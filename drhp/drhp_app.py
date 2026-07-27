import drhp_log
"""
drhp_app.py - local test server for the DRHP Scanner (Phase 1).

Run:   py -3.12 drhp_app.py     (or double-click run_drhp.bat)
Open:  http://127.0.0.1:8010

Endpoints:
  GET  /            -> drhp.html (the themed scanner UI)
  POST /scan        -> multipart file upload (.pdf/.txt) OR form field `url`
                       downloads/ingests, builds the index, returns the digest
  POST /ask         -> {"question": "..."} answered against the last-scanned doc

Laptop-only tool for now; not part of the deployed site. State is one document
at a time (module-level), which is exactly right for a single-user scanner.
"""
import os, io, json, tempfile, urllib.request
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

def _load_env():
    """Load .env robustly: BOM-safe, quote-stripping, works even if the .bat
    parsing failed or the file was created after launch attempts."""
    # Search own dir, then parent dirs (mounted as ipopredict/drhp/ -> the host
    # app's .env sits one level up), then the working directory. On Render none
    # of these exist and real environment variables are used instead - which is
    # why nothing here overwrites an already-set variable.
    _here = Path(__file__).resolve().parent
    _cands = [_here / ".env", _here.parent / ".env",
              _here.parent.parent / ".env", Path.cwd() / ".env"]
    p = next((c for c in _cands if c.exists()), _cands[0])
    loaded = []
    if p.exists():
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and not os.environ.get(k):   # real env vars win (Render)
                os.environ[k] = v; loaded.append(k)
    _cm = os.environ.get("COMPLIANCE_MODE", "0")
    if _cm == "1": print("  >>> COMPLIANCE_MODE ON - pure extractive relay, no LLM in output")
    for k in loaded:
        v = os.environ[k]
        print(f"  .env loaded: {k} = {v[:6]}...{v[-4:] if len(v) > 12 else ''}")
    if not loaded:
        print("  .env: not found or empty (LLM layers off; document layers fine)")
_load_env()

import drhp_ingest, drhp_digest, drhp_research, drhp_tables, drhp_corpus, drhp_analyst

app = FastAPI(title="DRHP Scanner")


# ------------------------------------------------------------- hardening
def _assert_public_url(url):
    """SSRF guard for URL scans. The audit's live finding: /scan fetched ANY
    user-supplied URL, so a hosted instance could be told to fetch cloud
    metadata (169.254.169.254) or internal services. Policy: http(s) only,
    every resolved address must be public, and redirects are not followed
    (a public URL 302-ing to localhost is the classic bypass)."""
    import socket, ipaddress
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise HTTPException(400, "Only http(s) URLs can be scanned.")
    host = u.hostname or ""
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror:
        raise HTTPException(400, "Host could not be resolved.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise HTTPException(400, "URL resolves to a non-public address.")


class _NoRedirect(__import__("urllib.request", fromlist=["x"]).HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise HTTPException(400, "Redirects are not followed for URL scans.")


_BUCKETS = {}
def _rate_limit(request, kind, per_hour):
    """Per-client inbound rate limit (audit: outbound pacing existed, inbound
    did not - one caller could monopolise the single worker). In-memory token
    bucket, which matches the single-instance deployment; the multi-instance
    upgrade is the same interface backed by Redis."""
    import time as _t
    ip = getattr(getattr(request, "client", None), "host", None) or "anon"
    key = (kind, ip)
    now = _t.time()
    tokens, last = _BUCKETS.get(key, (per_hour, now))
    tokens = min(per_hour, tokens + (now - last) * per_hour / 3600.0)
    if tokens < 1:
        drhp_log.warn("rate_limited", kind=kind, ip=ip)
        raise HTTPException(429, f"Rate limit: {per_hour} {kind}/hour. Try later.")
    _BUCKETS[key] = (tokens - 1, now)


@app.middleware("http")
async def _observe(request, call_next):
    """Request ID + timing on every call - the trace the audit said was missing."""
    import time as _t
    rid = drhp_log.new_request_id()
    request.state.rid = rid
    t0 = _t.time()
    resp = await call_next(request)
    drhp_log.info("request", rid=rid, path=request.url.path,
                  status=resp.status_code, ms=round((_t.time()-t0)*1000))
    resp.headers["x-request-id"] = rid
    return resp

CACHE_VERSION = "29"   # bump whenever digest/extraction output changes
CACHE_DIR = Path(__file__).resolve().parent / "cache"

STATE = {"index": None, "meta": None, "name": None, "digest": None,
         "last_response": None, "doc": None}

def _resolve_doc(doc):
    """Return (index, digest) for the requested document hash.

    Module STATE holds only the LAST scan - fine for the single-user laptop
    case, but on a shared deployment two users scanning different PDFs would
    overwrite each other and cross-answer. The fix reuses the content-addressed
    cache as the per-document store: if the caller names a doc hash that isn't
    the one in STATE, rehydrate index+digest from cache/<hash>.json (version-
    checked). No Redis or sessions needed - the cache IS the keyed store."""
    if not doc or doc == STATE.get("doc"):
        return STATE["index"], STATE["digest"]
    p = CACHE_DIR / f"{doc}.json"
    if not p.exists():
        return STATE["index"], STATE["digest"]
    try:
        hit = json.loads(p.read_text())
        if hit.get("version") != CACHE_VERSION:
            return STATE["index"], STATE["digest"]
        idx, _ = _rebuild_index(hit["pages"])
        return idx, hit["digest"]
    except Exception:
        return STATE["index"], STATE["digest"]

HERE = Path(__file__).resolve().parent


@app.get("/", response_class=HTMLResponse)
def home():
    return (HERE / "drhp.html").read_text(encoding="utf-8")


def _rebuild_index(pages):
    """Rebuild a searchable index from cached page text - no PDF re-parse, no
    LLM. Lets a cache hit serve the full experience (Ask tab included) instantly."""
    pgs = [(p, t) for p, t in pages]
    chunks = drhp_ingest.chunk_document(pgs)
    idx = drhp_ingest.TfidfIndex(chunks, doc_id="cached")
    try: idx.raw_pages = pgs
    except Exception: pass
    return idx, {"pages": len(pgs), "chunks": len(chunks)}


@app.post("/scan")
async def scan(request: Request, file: UploadFile = File(None), url: str = Form(None)):
    _rate_limit(request, "scans", int(os.environ.get("DRHP_SCANS_PER_HOUR", "12")))
    if file is None and not url:
        raise HTTPException(400, "Upload a PDF/TXT or provide a URL.")
    suffix = ".pdf"
    if file is not None:
        name = file.filename or "upload.pdf"
        suffix = ".txt" if name.lower().endswith(".txt") else ".pdf"
        data = await file.read()
    else:
        name = url.rsplit("/", 1)[-1] or "drhp.pdf"
        _assert_public_url(url)          # SSRF guard (audit finding, live)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        _opener = urllib.request.build_opener(_NoRedirect)
        with _opener.open(req, timeout=120) as r:
            data = r.read()
    if len(data) > 80 * 1024 * 1024:
        raise HTTPException(413, "File too large (80MB cap).")
    # ---- shared digest cache (keyed by document hash) -----------------------
    # IPOs are a SHARED corpus: many users scan the same handful of live issues.
    # Caching by content hash means LLM/parse cost scales with the number of
    # DOCUMENTS, not the number of USERS - 500 users x 5 hot IPOs = 5 scans, not
    # 2,500. This is the single biggest quota multiplier in the system.
    import hashlib
    _dochash = hashlib.sha256(data).hexdigest()[:24]
    _cachedir = CACHE_DIR
    _cachedir.mkdir(exist_ok=True)
    _cpath = _cachedir / f"{_dochash}.json"
    if _cpath.exists():
        try:
            _hit = json.loads(_cpath.read_text())
            if _hit.get("version") != CACHE_VERSION:
                raise ValueError("stale cache version - rescan")
            _idx, _meta2 = _rebuild_index(_hit["pages"])
            STATE["doc"] = _dochash
            STATE.update(index=_idx, meta=_hit["meta"], name=_hit["name"],
                         digest=_hit["digest"])
            _hit["resp"]["cached"] = True
            _hit["resp"]["doc"] = _dochash
            STATE["last_response"] = _hit["resp"]
            return JSONResponse(_hit["resp"])
        except Exception as _sw:
            drhp_log.swallowed("drhp_app", _sw)
            pass   # corrupt/legacy cache entry - fall through to a fresh scan
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(data); tmp = tf.name
    try:
        index, meta, pages = drhp_ingest.build_index(tmp)
        try: index.raw_pages = pages
        except Exception: pass
        table_fund, statements = ({}, [])
        if suffix == ".pdf":
            try:
                table_fund, statements = drhp_tables.extract_financial_tables(tmp, pages)
            except Exception as e:
                print(f"  table extraction skipped: {type(e).__name__}")
            try:
                segments = drhp_tables.extract_segments(tmp, pages)
            except Exception:
                segments = []
            try:
                peers = drhp_tables.extract_peers(tmp, pages)
            except Exception:
                peers = []
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        try: os.unlink(tmp)
        except OSError: pass
    offer, units, segments, peers = {}, None, [], []
    try:
        offer = drhp_tables.offer_snapshot(pages)
        units = drhp_tables.detect_units(pages)
    except Exception as _sw:
        drhp_log.swallowed("drhp_app", _sw)
        pass
    digest = drhp_digest.build_digest(index, table_fund=table_fund,
                                      statements=statements, offer=offer, segments=segments)
    digest["peers"] = peers
    digest["units"] = units
    # cross-document library: index this scan for semantic search (iteration 38).
    # Fire-and-forget thread: embedding must never delay or fail a scan; without
    # a backend it reports UNAVAILABLE and the scan proceeds untouched.
    def _sem_index(_h=_dochash, _n=name, _ix=index):
        try:
            import drhp_semantic
            _c = _ix.chunks
            _ch = list(_c.values()) if isinstance(_c, dict) else list(_c)
            r = drhp_semantic.index_document(_h, _n, _ch)
            drhp_log.info("semantic_index", doc=_h,
                          status=r.get("status"), added=r.get("added"))
        except Exception as _e:
            drhp_log.swallowed("drhp_app._sem_index", _e)
    import threading as _th
    _th.Thread(target=_sem_index, daemon=True).start()
    company = drhp_research.company_identity(index) or name
    try:
        drhp_corpus.update(company, digest)
        digest["corpus_context"] = drhp_corpus.context(company)
    except Exception:
        digest["corpus_context"] = []
    STATE["doc"] = _dochash
    STATE.update(index=index, meta=meta, name=name, digest=digest)
    # iteration 21: every scan joins the comparison library - add as many IPOs as
    # you want; /compare works over any subset, fully dynamic (no fixed doc count)
    try:
        import drhp_compare
        drhp_compare.save_to_library(name.rsplit(".", 1)[0], digest)
    except Exception as _sw:
        drhp_log.swallowed("drhp_app", _sw)
        pass
    import drhp_llm
    status, hint = drhp_llm.health()
    resp = {"name": name, "meta": meta, "digest": digest, "doc": _dochash,
            "llm_available": status == "ok", "llm_hint": hint, "cached": False}
    STATE["last_response"] = resp
    try:   # persist for the shared cache - next user of this PDF pays nothing
        _cpath.write_text(json.dumps(
            {"version": CACHE_VERSION, "name": name, "meta": meta,
             "digest": digest, "pages": pages, "resp": resp}))
    except Exception as _sw:
        drhp_log.swallowed("drhp_app", _sw)
        pass
    return JSONResponse(resp)


@app.get("/last")
async def last():
    """Restore the report after a page refresh without re-scanning."""
    if not STATE.get("last_response"):
        raise HTTPException(404, "nothing scanned yet")
    return JSONResponse(STATE["last_response"])


@app.get("/library")
async def library():
    import drhp_compare
    return JSONResponse(drhp_compare.list_library())


@app.post("/compare")
async def compare(body: dict = None):
    """Compare ANY set of scanned documents (or all). Dynamic: the matrix is the
    union of whatever metrics each digest carries; add an IPO by scanning it."""
    import drhp_compare
    sel = (body or {}).get("docs") or []
    return JSONResponse(drhp_compare.compare(selection=sel))



@app.post("/semantic")
async def semantic(request: Request, body: dict):
    """Search ACROSS every scanned prospectus - the one retrieval problem
    lexical search structurally cannot solve (same risk, different wording per
    issuer). Honest degradation: no embedding backend -> status UNAVAILABLE."""
    _rate_limit(request, "questions", int(os.environ.get("DRHP_ASKS_PER_HOUR", "120")))
    q = (body or {}).get("query", "").strip()
    if not q:
        raise HTTPException(400, "Empty query.")
    import drhp_semantic
    out = drhp_semantic.search_library(q, k=int((body or {}).get("k", 8)),
                                       exclude_doc=(body or {}).get("exclude_doc"))
    return JSONResponse(out)

@app.post("/ask")
async def ask(request: Request, body: dict):
    _rate_limit(request, "questions", int(os.environ.get("DRHP_ASKS_PER_HOUR", "120")))
    if STATE["index"] is None:
        raise HTTPException(409, "Scan a document first.")
    q = (body.get("question") or "").strip()
    if not q:
        raise HTTPException(400, "Empty question.")
    # Answer immediately with the fast lexical hybrid. Kick off dense-embedding
    # build in the BACKGROUND so it is ready for later questions without ever
    # making the user wait ~60s on the first ask. (Set USE_DENSE=1 to pre-build
    # at scan time instead.)
    idx, _digest = _resolve_doc((body or {}).get("doc"))
    if idx is None:
        idx = STATE["index"]
    if hasattr(idx, "ensure_dense") and idx._dense is None \
            and not STATE.get("_dense_building"):
        STATE["_dense_building"] = True
        import threading
        def _bg():
            try: idx.ensure_dense()
            finally: STATE["_dense_building"] = False
        threading.Thread(target=_bg, daemon=True).start()
    # iteration 5: rerank retrieved context so open-ended answers use the most
    # relevant chunks (over-fetch then rerank by term-overlap). Stored for the
    # answerer to use; degrades to plain retrieval if anything fails.
    try:
        import drhp_rag
        idx._reranked = drhp_rag.rag_answer_reranked(idx, q)
    except Exception as _sw:
        drhp_log.swallowed("drhp_app", _sw)
        pass
    # iteration 17: tool-calling agent. When an LLM is live, it DECIDES which
    # proven tool answers the question (validated financials / litigation / peers
    # / flags / semantic search), answers only from tool results, and a numeric
    # verifier rejects any number not present in them. agent_answer returns None
    # on any LLM failure (429/timeout/compliance), so the extractive path below
    # remains the always-working fallback - the agent is an enhancement, never a
    # dependency (selftest stays green with no key).
    try:
        import os as _os, drhp_agent, drhp_llm
        if _os.environ.get("COMPLIANCE_MODE", "0") != "1" and not drhp_llm._LLM_DISABLED[0]:
            _ag = drhp_agent.agent_answer(idx, _digest or STATE["digest"], q, drhp_llm.complete)
            if _ag and _ag.get("answer"):
                base = drhp_digest.answer_question(idx, q, digest=_digest or STATE["digest"])
                base["agent"] = {"answer": _ag["answer"],
                                 "tools_used": _ag.get("tools_used", []),
                                 "method": _ag.get("method")}
                # normalize to the items shape the UI maps over - the agent
                # returns a STRING; leaving it raw crashes (d.answer||[]).map
                import re as _re
                _pg = _re.search(r"\(p\.?\s*(\d+)\)", str(_ag["answer"]))
                base["answer"] = [{"text": str(_ag["answer"]),
                                   "page": int(_pg.group(1)) if _pg else None,
                                   "section": "agent"}]
                base["note"] = "answered by the tool-calling agent (tools: " + \
                               ", ".join(_ag.get("tools_used", [])) + ")"
                return JSONResponse(base)
    except Exception as _sw:
        drhp_log.swallowed("drhp_app", _sw)
        pass
    return JSONResponse(drhp_digest.answer_question(idx, q, digest=_digest or STATE["digest"]))


@app.post("/analyze")
async def analyze():
    if STATE["digest"] is None:
        raise HTTPException(409, "Scan a document first.")
    a = drhp_digest.build_analysis(STATE["digest"], STATE["index"])
    if a is None:
        raise HTTPException(424, "No GEMINI_API_KEY configured. Add it to .env to enable AI explanations.")
    return JSONResponse(a)


@app.get("/llmtest")
async def llmtest():
    import drhp_llm
    return JSONResponse({"results": drhp_llm.selftest()})


@app.post("/analyst")
async def analyst():
    if STATE["digest"] is None:
        raise HTTPException(409, "Scan a document first.")
    memo = drhp_analyst.analyze(STATE["digest"], STATE["index"])
    memo["narrative"] = drhp_analyst.narrative(memo)
    return JSONResponse(memo)


@app.post("/research")
async def research(body: dict = None):
    if STATE["index"] is None:
        raise HTTPException(409, "Scan a document first.")
    custom = (body or {}).get("query")
    company = drhp_research.company_identity(STATE["index"]) or STATE["name"]
    promoters = drhp_research.promoter_names(STATE["digest"] or {})
    r = drhp_research.external_research(company, promoters, custom=custom)
    r["company"] = company
    return JSONResponse(r)


if __name__ == "__main__":
    print("DRHP Scanner -> http://127.0.0.1:8010")
    uvicorn.run(app, host="127.0.0.1", port=8010)
