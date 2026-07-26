"""
drhp_research.py - EXTERNAL research layer for the DRHP scanner.

Everything this module returns is clearly marked kind="web": it comes from the
internet, NOT from the prospectus, and every item carries its source URL so the
user can always verify. Two engines, tried in order:

  1. Gemini with Google-Search grounding (GEMINI_API_KEY set): the model
     searches the web and returns text WITH grounding sources.
  2. DuckDuckGo HTML (no key needed): plain search-result titles + snippets.

If neither works (offline / blocked), returns a graceful "unavailable" so the
document layers keep working untouched.
"""
import os, re, json, html, urllib.request, urllib.parse

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def company_identity(index):
    """Guess the company name from the cover pages (front_matter chunks)."""
    chunks = index.chunks
    chunks = list(chunks.values()) if isinstance(chunks, dict) else list(chunks)
    names = {}
    for c in chunks:
        pg = c.get("page_start", 99)
        if pg > 4:
            continue
        for m in re.finditer(r"\b([A-Z][A-Z&.'\- ]{3,60}?(?:LIMITED|LTD\.?))\b", c["text"]):
            nm = re.sub(r"\s{2,}", " ", m.group(1).title()).strip()
            nm = re.sub(r"^(?:[A-Z]\s+)+", "", nm)          # stray leading initials
            if (len(nm.split()) > 8 or "Herring" in nm or "Stock Exchange" in nm
                    or "Private" in nm):                       # issuers are public Ltd
                continue
            # weight: cover page mentions count 3x - the issuer dominates page 1
            names[nm] = names.get(nm, 0) + (3 if pg == 1 else 1)
    return max(names, key=names.get) if names else None


def promoter_names(digest):
    """Pull promoter person-names out of the digest's promoter items."""
    out = []
    for f in digest.get("fields", []):
        if f["key"] != "promoters":
            continue
        for it in f["items"]:
            for m in re.finditer(r"(?:Mr|Mrs|Ms|Dr)\.?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})", it["text"]):
                if m.group(1) not in out:
                    out.append(m.group(1))
    return out[:4]


# ---------------------------------------------------------------- engines
def _gemini_grounded(query):
    """Gemini with google_search grounding: text + real web sources."""
    model = os.environ.get("GEMINI_MODEL_ACTIVE") or os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={GEMINI_KEY}")
    body = json.dumps({
        "contents": [{"parts": [{"text":
            f"Search the web and answer factually in 3-5 short bullets: {query}. "
            f"Only state things supported by the search results. If nothing "
            f"relevant is found, say so."}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2},
    }).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        d = json.loads(r.read().decode())
    cand = d["candidates"][0]
    text = " ".join(p.get("text", "") for p in cand["content"]["parts"]).strip()
    sources = []
    for ch in (cand.get("groundingMetadata", {}) or {}).get("groundingChunks", []):
        w = ch.get("web") or {}
        if w.get("uri"):
            sources.append({"title": w.get("title") or w["uri"], "url": w["uri"]})
    items = [{"text": ln.strip().lstrip("-*\u2022 "), "sources": sources[:3]}
             for ln in text.splitlines() if len(ln.strip()) > 30][:5]
    return items


def _ddg(query, n=3):
    """Keyless fallback: DuckDuckGo (lite + html endpoints)."""
    q = urllib.parse.quote(query)
    page = ""
    for u in (f"https://lite.duckduckgo.com/lite/?q={q}",
              f"https://html.duckduckgo.com/html/?q={q}"):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                page = r.read().decode("utf-8", "ignore")
            if "result" in page or "uddg" in page: break
        except Exception:
            continue
    out = []
    blocks = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)>', page, re.S)
    if not blocks:   # lite endpoint markup
        blocks = [(h, t, "") for h, t in re.findall(
            r'<a[^>]+href="([^"]*uddg=[^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(.*?)</a>', page, re.S)]
    if not blocks:
        blocks = [(h, t, "") for h, t in re.findall(
            r'<a[^>]+rel="nofollow"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)[:6]]
    for href, title, snip in blocks[:n]:
        m = re.search(r"uddg=([^&]+)", href)
        url = urllib.parse.unquote(m.group(1)) if m else href
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        out.append({"text": clean(snip)[:320],
                    "sources": [{"title": clean(title)[:90], "url": url}]})
    return out


def _search(query):
    if GEMINI_KEY:
        try:
            items = _gemini_grounded(query)
            if items:
                return items, "gemini+google"
        except Exception as e:
            print(f"  gemini research failed: {str(e)[:160]}")
    try:
        return _ddg(query), "duckduckgo"
    except Exception as e:
        print(f"  ddg failed: {type(e).__name__}")
        return [], "unavailable"


# ---------------------------------------------------------------- main entry
def external_research(company, promoters=None, custom=None):
    """Run the standard research plan (or one custom query). Everything is
    kind='web' with sources, clearly separated from document facts."""
    if custom:
        items, engine = _search(custom)
        return {"kind": "web", "engine": engine,
                "topics": [{"topic": custom, "items": items}]}
    plan = [("Recent news and developments", f'"{company}" company news'),
            ("IPO commentary and reviews",   f'"{company}" IPO review analysis GMP'),
            ("Sector and industry context",  f'"{company}" industry sector outlook India')]
    for p in (promoters or [])[:2]:
        plan.append((f"Promoter background: {p}",
                     f'"{p}" "{company}" promoter background'))
    topics, engine = [], "unavailable"
    for topic, q in plan:
        items, engine = _search(q)
        topics.append({"topic": topic, "items": items})
    return {"kind": "web", "engine": engine, "topics": topics}
