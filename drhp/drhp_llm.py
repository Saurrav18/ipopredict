"""
drhp_llm.py - one interface, many free LLM providers.

complete(prompt, json_mode=False) tries providers in order of configured keys:
  gemini     - Google AI Studio free tier (only one with built-in web grounding)
  groq       - Groq free tier (Llama 70B-class, extremely fast, OpenAI-style API)
  openrouter - OpenRouter ':free' models (one key, many models - ideal for comparison)
  ollama     - local models, no key, no rate limit, fully private (needs Ollama app)

Set LLM_PROVIDER=gemini|groq|openrouter|ollama to force one; otherwise the first
provider with a key (or a running Ollama) wins. Models overridable via env:
GEMINI_MODEL, GROQ_MODEL, OPENROUTER_MODEL, OLLAMA_MODEL.
"""
import os, json, urllib.request

def _post(url, body, headers, timeout=20):
    # A real User-Agent is required: some providers sit behind Cloudflare, which
    # blocks the default "Python-urllib" UA with error 1010. Presenting a normal
    # browser UA avoids that false block.
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": ua, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

_LLM_DISABLED = [False]      # trip after repeated timeouts so a scan never stalls
_TIMEOUT_STREAK = [0]
_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash",
                  "gemini-flash-latest"]

def _gemini(prompt, json_mode):
    """Tries current free-tier model names in order; a retired model returns
    429/404 even with a valid key, so model fallback is part of auth.

    Circuit breaker: if the network keeps timing out (slow/blocked API), stop
    attempting LLM calls for the rest of this process instead of hanging ~20s on
    every call. The scanner then runs as pure extraction - fully functional, just
    without the AI-phrased boxes - so a scan stays fast even if the LLM is down."""
    if os.environ.get("COMPLIANCE_MODE", "0") == "1":
        raise RuntimeError("COMPLIANCE_MODE: LLM calls disabled (pure extractive relay)")
    if _LLM_DISABLED[0]:
        raise RuntimeError("LLM disabled for this session after repeated timeouts")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    forced = os.environ.get("GEMINI_MODEL", "").strip()
    models = [forced] + [m for m in _GEMINI_MODELS if m != forced] if forced else _GEMINI_MODELS
    cfg = {"temperature": 0.1}
    if json_mode: cfg["response_mime_type"] = "application/json"
    last = None
    for model in models:
        try:
            d = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
                      {"x-goog-api-key": key})
            os.environ["GEMINI_MODEL_ACTIVE"] = model
            _TIMEOUT_STREAK[0] = 0
            return d["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            code = getattr(e, "code", None)
            last = e
            msg = str(e).lower()
            if "timed out" in msg or "timeout" in msg or isinstance(e, TimeoutError):
                _TIMEOUT_STREAK[0] += 1
                if _TIMEOUT_STREAK[0] >= 2:
                    _LLM_DISABLED[0] = True
                    print("  LLM endpoint unresponsive - switching to extraction-only "
                          "for this session (scan continues, no AI-phrased text)")
                break
            if code == 429:
                # rate-limited on Gemini. Only trip the global breaker if Gemini is
                # the ONLY provider; otherwise raise so complete() falls through to
                # the next provider (groq/ollama).
                others = [p for p in PROVIDERS if p[0] != "gemini" and p[2]()]
                if not others:
                    _LLM_DISABLED[0] = True
                    print("  Gemini quota exhausted (429), no other provider - "
                          "extraction-only this session")
                raise
            if code == 404:
                continue
            raise
    raise last

def _openai_style(url, key, model, prompt, json_mode, extra_headers=None):
    body = {"model": model, "temperature": 0.1,
            "messages": [{"role": "user", "content": prompt}]}
    if json_mode: body["response_format"] = {"type": "json_object"}
    d = _post(url, body, {"Authorization": f"Bearer {key}", **(extra_headers or {})})
    return d["choices"][0]["message"]["content"]

def _groq(prompt, json_mode):
    return _openai_style("https://api.groq.com/openai/v1/chat/completions",
                         os.environ.get("GROQ_API_KEY", "").strip(),
                         os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
                         prompt, json_mode)

def _openrouter(prompt, json_mode):
    return _openai_style("https://openrouter.ai/api/v1/chat/completions",
                         os.environ.get("OPENROUTER_API_KEY", "").strip(),
                         os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
                         prompt, json_mode)

def _cerebras(prompt, json_mode):
    """Cerebras: the most generous free tier as of 2026 (~1M tokens/day, no card)
    - 10x Groq's daily token budget. Free catalogs churn HARD (Cerebras dropped
    every Llama model; as of July 2026 the live list is gpt-oss-120b, gemma-4-31b,
    zai-glm-4.7), so the model name is env-overridable via CEREBRAS_MODEL and a
    404 simply fails over to the next provider instead of breaking the run."""
    return _openai_style("https://api.cerebras.ai/v1/chat/completions",
                         os.environ.get("CEREBRAS_API_KEY", "").strip(),
                         os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
                         prompt, json_mode)

def _github(prompt, json_mode):
    """GitHub Models: free frontier-model access tied to a GitHub account."""
    return _openai_style("https://models.inference.ai.azure.com/chat/completions",
                         os.environ.get("GITHUB_TOKEN", "").strip(),
                         os.environ.get("GITHUB_MODEL", "gpt-4o-mini"),
                         prompt, json_mode)

def _ollama(prompt, json_mode):
    body = {"model": os.environ.get("OLLAMA_MODEL", "llama3.2"),
            "prompt": prompt, "stream": False}
    if json_mode: body["format"] = "json"
    d = _post("http://127.0.0.1:11434/api/generate", body, {},
              timeout=int(os.environ.get("OLLAMA_TIMEOUT", "45")))
    return d["response"]


def _load_env_once():
    """Load .env if the process hasn't already. drhp_app does this at startup,
    but tests/scripts import drhp_llm directly - without this, available() would
    report no providers purely because of import order (a confusing false alarm)."""
    import os as _os
    from pathlib import Path as _Path
    if _os.environ.get("_DRHP_ENV_LOADED") == "1":
        return
    _here = _Path(__file__).resolve().parent
    _cands = [_here / ".env", _here.parent / ".env",
              _here.parent.parent / ".env", _Path.cwd() / ".env"]
    p = next((c for c in _cands if c.exists()), _cands[0])
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and not _os.environ.get(k):
                _os.environ[k] = v
    _os.environ["_DRHP_ENV_LOADED"] = "1"


_load_env_once()


# Provider chain: each free tier has INDEPENDENT limits, so stacking multiplies
# free capacity (and survives any one provider's quota dying mid-demo). Ordered
# by daily budget: cerebras (~1M tok/day) > gemini (1.5K req/day) > groq (100K
# tok/day) > openrouter (free models) > github > ollama (local, unlimited).
PROVIDERS = [("cerebras", _cerebras, lambda: os.environ.get("CEREBRAS_API_KEY", "").strip()),
             ("gemini", _gemini, lambda: os.environ.get("GEMINI_API_KEY", "").strip()),
             ("groq", _groq, lambda: os.environ.get("GROQ_API_KEY", "").strip()),
             ("openrouter", _openrouter, lambda: os.environ.get("OPENROUTER_API_KEY", "").strip()),
             ("github", _github, lambda: os.environ.get("GITHUB_TOKEN", "").strip()),
             ("ollama", _ollama, lambda: os.environ.get("OLLAMA", "").strip() or None)]

def available():
    forced = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if forced:
        return [p for p in PROVIDERS if p[0] == forced]
    return [p for p in PROVIDERS if p[2]()]

_LAST_CALL = [0.0]
_LAST_SERVED = [None]

# Each free tier caps REQUESTS PER MINUTE at a different level, so one global
# pace is wrong: it either wastes the fast providers or hammers the slow ones
# into a 429 (Cerebras allows ~5 RPM -> a 2.5s pace = 24 RPM = instant 429).
# Interval = 60 / RPM, plus a small margin. Env-overridable.
_MIN_INTERVAL = {
    "cerebras":   float(os.environ.get("CEREBRAS_MIN_INTERVAL", "13")),   # ~5 RPM
    "gemini":     float(os.environ.get("GEMINI_MIN_INTERVAL", "4.5")),    # ~15 RPM
    "groq":       float(os.environ.get("GROQ_MIN_INTERVAL", "2.5")),      # ~30 RPM
    "openrouter": 3.5,
    "github":     6.0,
    "ollama":     0.0,                                                    # local
}
_LAST_BY_PROVIDER = {}

def complete(prompt, json_mode=False):
    """First working provider wins. Free tiers rate-limit bursts, so calls are
    paced (min 2.5s apart) and a 429 gets one backoff retry before moving on.
    Once the breaker is tripped (quota exhausted / compliance mode), returns
    immediately with no network call or sleep - so a scan never stalls."""
    import time
    if _LLM_DISABLED[0] or os.environ.get("COMPLIANCE_MODE", "0") == "1":
        raise RuntimeError("LLM disabled (compliance mode or exhausted quota); "
                           "extractive output shown instead")
    errs = []
    for name, fn, _ in available():
        # respect THIS provider's RPM cap before calling it
        gap = _MIN_INTERVAL.get(name, 2.5)
        wait = gap - (time.time() - _LAST_BY_PROVIDER.get(name, 0.0))
        if wait > 0:
            time.sleep(wait)
        for attempt in (1, 2, 3):
            try:
                out = fn(prompt, json_mode)
                _LAST_CALL[0] = _LAST_BY_PROVIDER[name] = time.time()
                _LAST_SERVED[0] = name
                return out
            except Exception as e:
                code = getattr(e, "code", None)
                _LAST_BY_PROVIDER[name] = time.time()
                if _LLM_DISABLED[0]:
                    errs.append(f"{name}:breaker"); break
                if code == 429 and attempt < 3:
                    # honour retry-after when the provider sends it; otherwise
                    # exponential backoff. Waiting out a per-MINUTE cap is far
                    # better than burning the whole chain and landing on a dead
                    # daily quota - a 429 is often "too fast", not "out of quota".
                    ra = None
                    try:
                        ra = float(e.headers.get("retry-after"))   # type: ignore
                    except Exception:
                        pass
                    delay = min(ra if ra else (gap * attempt) + 2, 30)
                    if os.environ.get("LLM_DEBUG", "") == "1":
                        print(f"    [llm] {name} 429 - backing off {delay:.0f}s "
                              f"(attempt {attempt}/3)", flush=True)
                    time.sleep(delay)
                    continue
                errs.append(f"{name}:{code or type(e).__name__}")
                if os.environ.get("LLM_DEBUG", "") == "1":
                    print(f"    [llm] {name} failed ({code or type(e).__name__}: "
                          f"{str(e)[:60]}) -> next provider", flush=True)
                break
    _LAST_CALL[0] = time.time()
    raise RuntimeError("all providers rate-limited or failed (" +
                       (", ".join(errs) or "no keys set") +
                       "); the extractive fallback below is shown instead")

def provider_name():
    """The provider that ACTUALLY served the last call - not merely the first one
    configured. Reporting the configured provider lies whenever the chain fails
    over (e.g. cerebras 404 -> gemini 429 -> groq serves)."""
    if _LAST_SERVED[0]:
        return _LAST_SERVED[0]
    a = available()
    return a[0][0] if a else None

def health():
    """Human-readable status of LLM config, incl. common key mistakes."""
    gk = os.environ.get("GEMINI_API_KEY", "").strip()
    # AI Studio issues AIza... and newer AQ.... keys; both are valid.
    if gk and gk.startswith(("ya29.", "Bearer ")):
        return ("misconfigured", "GEMINI_API_KEY looks like an OAuth token, not an API key. "
                "Use the key from aistudio.google.com/apikey")
    a = available()
    if not a:
        return ("off", "no LLM key configured; document layers fully functional")
    return ("ok", f"provider: {a[0][0]}")


def selftest():
    """One tiny live call per configured provider; returns exact errors."""
    import time
    out = []
    for name, fn, keyfn in PROVIDERS:
        if not keyfn(): continue
        t0 = time.time()
        try:
            r = fn("Reply with exactly: OK", False)
            out.append({"provider": name, "ok": "OK" in (r or "").upper(),
                        "latency_s": round(time.time() - t0, 1)})
        except Exception as e:
            detail = str(e)[:200]
            try:
                body = e.read().decode()[:200]   # HTTPError body has Google's real message
                detail += " | " + body
            except Exception:
                pass
            out.append({"provider": name, "ok": False, "error": detail})
    return out or [{"ok": False, "error": "no provider keys configured"}]
