import drhp_log
"""
drhp_ingest.py - turn a DRHP/RHP prospectus (PDF or TXT) into a searchable index.

Pipeline: extract text per page -> detect SEBI-mandated sections -> chunk with
overlap + metadata {section, page} -> filter boilerplate -> build a retrieval
index. Retrieval backend is pluggable:

  1. ChromaDB (real vector DB, semantic embeddings) if `chromadb` is installed
     -> py -3.12 -m pip install chromadb
  2. TF-IDF cosine (scikit-learn, already installed) as the zero-install fallback

Both expose the same .search(query, k, section=None) so drhp_digest.py and
drhp_app.py do not care which one is active.
"""
import os, re, json, hashlib
from pathlib import Path

# SEBI mandates these top-level sections in every DRHP/RHP, in roughly this
# order. Matching is uppercase + fuzzy so ToC variations still hit.
SECTION_PATTERNS = [
    ("risk_factors",        r"\bRISK\s+FACTORS?\b"),
    ("industry",            r"\bINDUSTRY\s+OVERVIEW\b"),
    ("business",            r"\bOUR\s+BUSINESS\b"),
    ("promoters",           r"\bOUR\s+PROMOTERS?\b|\bPROMOTERS?\s+AND\s+PROMOTER\s+GROUP\b"),
    ("management",          r"\bOUR\s+MANAGEMENT\b|\bBOARD\s+OF\s+DIRECTORS\b"),
    ("capital_structure",   r"\bCAPITAL\s+STRUCTURE\b"),
    ("objects",             r"\bOBJECTS?\s+OF\s+THE\s+(ISSUE|OFFER)\b"),
    ("basis_of_price",      r"\bBASIS\s+FOR\s+(THE\s+)?(ISSUE|OFFER)\s+PRICE\b"),
    ("dividend",            r"\bDIVIDEND\s+POLICY\b"),
    ("financials",          r"\bFINANCIAL\s+(STATEMENTS|INFORMATION)\b|\bRESTATED\s+.{0,30}FINANCIAL\b"),
    ("mdna",                r"\bMANAGEMENT'?S?\s+DISCUSSION\s+AND\s+ANALYSIS\b"),
    ("related_party",       r"\bRELATED\s+PARTY\s+TRANSACTIONS?\b"),
    ("litigation",          r"\bOUTSTANDING\s+LITIGATIONS?\b|\bLEGAL\s+PROCEEDINGS\b|\bLITIGATIONS?\s+AND\s+(OTHER\s+)?MATERIAL\s+DEVELOPMENTS\b"),
    ("government_approvals",r"\bGOVERNMENT\s+AND\s+OTHER\s+APPROVALS\b"),
    ("offer_structure",     r"\bTERMS\s+OF\s+THE\s+(ISSUE|OFFER)\b|\b(ISSUE|OFFER)\s+STRUCTURE\b"),
]

# lines that are pure boilerplate noise in every prospectus
_NOISE = re.compile(
    r"^(page\s+\d+|draft red herring prospectus|red herring prospectus|"
    r"please read section 32|dated\s|for private circulation|"
    r"\d+\s*$|table of contents)", re.I)

CHUNK_WORDS   = 220   # ~ 800ish tokens of prospectus prose is too big; 220 words retrieves tighter
CHUNK_OVERLAP = 40


def _fix_shredded_digits(text):
    """Repair PDF digit-shredding WITHOUT fusing adjacent columns.

    pdfplumber/PyMuPDF sometimes emit '8 ,559.67' or '0 .12' - a space inside a
    single number. Only two SAFE merges exist:
      digit + ' ,' + 3 digits   ->  '8 ,559.67'  => '8,559.67'
      digit + ' .' + digits     ->  '0 .12'      => '0.12'
    Anything looser (e.g. plain 'digit space digit') would fuse neighbouring
    table COLUMNS ('14 9,770.90' is note-14 next to a value) and corrupt data,
    so it is deliberately NOT attempted."""
    text = re.sub(r"(\d) ,(\d{3})", r"\1,\2", text)
    text = re.sub(r"(\d) \.(\d)", r"\1.\2", text)
    return text


def extract_pages(path):
    """Return [(page_no, text), ...] from a PDF or TXT file.

    PDF text is read with PyMuPDF (fitz) when available - it is 10-20x faster
    than pdfplumber's layout engine and gives equivalent text for prose-heavy
    prospectuses, which is what dominates a 500-page RHP. pdfplumber is kept as
    an automatic fallback (and is still used elsewhere for precise TABLE work,
    where its cell detection matters). This makes a full scan seconds, not
    minutes, without losing extraction quality."""
    p = str(path)
    if p.lower().endswith(".txt"):
        lines = Path(p).read_text(encoding="utf-8", errors="ignore").splitlines()
        # simulate pages every ~55 lines, PRESERVING line structure so
        # section-heading detection still works on standalone lines
        pages = []
        for i in range(0, len(lines), 55):
            pages.append((i // 55 + 1, "\n".join(lines[i:i+55])))
        return pages
    # fast path: PyMuPDF (disable with USE_FAST_PDF=0 to force pdfplumber)
    if os.environ.get("USE_FAST_PDF", "1") == "1":
     try:
        try:
            import pymupdf as fitz   # PyMuPDF >= 1.24 preferred name
        except ImportError:
            import fitz              # older name
        pages = []
        with fitz.open(p) as doc:
            for i, page in enumerate(doc, start=1):
                try:
                    pages.append((i, _fix_shredded_digits(page.get_text("text") or "")))
                except Exception:
                    pages.append((i, ""))
        if sum(len(t) for _, t in pages) > 500:
            if not os.environ.get("_PDF_ENGINE_SHOWN"):
                print("  [PDF engine] PyMuPDF (fast) - text extracted in seconds")
                os.environ["_PDF_ENGINE_SHOWN"] = "1"
            return pages
     except Exception as _sw:
        drhp_log.swallowed("drhp_ingest", _sw)
        pass
    # fallback: pdfplumber (slower but robust)
    if not os.environ.get("_PDF_ENGINE_SHOWN"):
        print("  [PDF engine] pdfplumber (slow fallback) - install PyMuPDF for 10x speedup: pip install PyMuPDF")
        os.environ["_PDF_ENGINE_SHOWN"] = "1"
    import pdfplumber
    pages = []
    with pdfplumber.open(p) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            try:
                pages.append((i, _fix_shredded_digits(page.extract_text() or "")))
            except Exception:
                pages.append((i, ""))
            finally:
                try: page.flush_cache()
                except Exception: pass
    return pages


def detect_section(line, current):
    """If this line is a TRUE section heading, return its key; else keep current.
    DRHP headings are short, standalone, ALL-CAPS lines. Requiring that stops
    cross-references inside body text ("see Our Business on page 213") from
    flipping the section mid-document."""
    st = line.strip()
    if len(st) > 90 or len(st) < 4:
        return current
    alpha = [c for c in st if c.isalpha()]
    if not alpha or sum(c.isupper() for c in alpha) / len(alpha) < 0.7:
        return current                    # body text, not a heading
    up = st.upper()
    for key, pat in SECTION_PATTERNS:
        if re.search(pat, up):
            return key
    return current


def chunk_document(pages):
    """Section-aware chunking. Returns list of dicts:
    {id, text, section, page_start, page_end}"""
    chunks, buf, buf_pages, section = [], [], [], "front_matter"

    def flush():
        if not buf: return
        words = " ".join(buf).split()
        i = 0
        while i < len(words):
            piece = words[i:i + CHUNK_WORDS]
            if len(piece) < 25 and chunks:      # tail too small, merge into previous
                chunks[-1]["text"] += " " + " ".join(piece)
                break
            chunks.append({
                "id": f"c{len(chunks):04d}",
                "text": " ".join(piece),
                "section": section,
                "page_start": buf_pages[0],
                "page_end": buf_pages[-1],
            })
            i += CHUNK_WORDS - CHUNK_OVERLAP

    for page_no, text in pages:
        for line in (text or "").splitlines():
            if page_no <= 2:
                if line.strip(): buf.append(line.strip())
                if not buf_pages or buf_pages[-1] != page_no: buf_pages.append(page_no)
                continue
            st = re.sub(r"\b\d{1,4}\s*\|\s*P\s*a\s*g\s*e\b", " ", line).strip()
            st = re.sub(r"\s{2,}", " ", st)
            # noise filter targets short header/footer lines only; a long
            # content line that merely CONTAINS such a phrase must survive
            if len(st) < 80 and _NOISE.match(st):
                continue
            new_sec = detect_section(st, section)
            if new_sec != section:              # heading found: close old section
                flush(); buf, buf_pages, section = [], [], new_sec
            if st:
                buf.append(st)
                if not buf_pages or buf_pages[-1] != page_no:
                    buf_pages.append(page_no)
    flush()
    return [c for c in chunks if len(c["text"].split()) >= 25]


# retrieval backends -----------------------------------------------------------

# query-word -> target section, for section-aware retrieval boosting. Maps the
# vocabulary a user (or a field intent) uses to the RHP section that answers it.
# Soft signal only: used to boost, never to hard-filter, so a wrong guess is safe.
_SECTION_CUES = {
    "risk_factors":   ("risk", "risks", "concern", "threat", "adverse", "exposure"),
    "objects":        ("use of proceeds", "objects of the", "utilise", "utilisation",
                       "use the money", "deploy the funds", "proceeds of the"),
    "promoters":      ("promoter", "promoters", "founder", "controlling"),
    "management":     ("director", "board", "management", "kmp", "key managerial"),
    "related_party":  ("related party", "related-party", "related parties"),
    "dividend":       ("dividend", "payout", "distribution policy"),
    "litigation":     ("litigation", "legal proceeding", "case", "criminal", "lawsuit",
                       "outstanding case", "pending case"),
    "financials":     ("revenue", "profit", "ebitda", "cash flow", "balance sheet",
                       "net worth", "borrowings", "margin"),
    "capital_structure": ("shareholding", "capital structure", "equity shares",
                          "pre-issue", "post-issue"),
    "industry":       ("industry", "market size", "sector overview", "competitive landscape"),
    "business":       ("business model", "our business", "products", "operations",
                       "manufacturing", "what we do", "competitive strength"),
    "basis_of_price": ("valuation", "price band", "p/e", "peer", "basis for"),
    "offer_structure":("offer for sale", "fresh issue", "offer size", "issue size"),
}

def _infer_section(query):
    """Return the section a query most likely targets, or None. Longest-cue match
    wins so multi-word cues ('use of proceeds') beat generic single words."""
    q = query.lower()
    best, best_len = None, 0
    for sec, cues in _SECTION_CUES.items():
        for cue in cues:
            if cue in q and len(cue) > best_len:
                best, best_len = sec, len(cue)
    return best


class TfidfIndex:
    """Three-way hybrid retrieval: TF-IDF cosine + BM25 + dense embeddings
    (ChromaDB), fused by reciprocal-rank fusion. TF-IDF favors phrase/ngram
    overlap; BM25 handles short keyword queries and length-normalizes long
    chunks; dense embeddings capture semantic similarity when the query and the
    text share meaning but not vocabulary. Fusing all three beats any one alone.

    The dense leg is optional and degrades gracefully: if ChromaDB or its model
    is unavailable (offline, or a minimal deploy), the index runs as the proven
    BM25+TF-IDF hybrid with identical output shape - so nothing downstream breaks
    and performance never regresses below the lexical baseline."""
    def __init__(self, chunks, doc_id=None, use_dense=None, persist_dir=None):
        import math
        from collections import Counter
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        self._cos = cosine_similarity
        self.chunks = chunks
        self.raw_pages = None
        self.vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2),
                                   sublinear_tf=True, max_features=60000)
        self.mat = self.vec.fit_transform([c["text"] for c in chunks])
        # bm25 structures (pure python, no deps)
        self._tok = lambda t: re.findall(r"[a-z]{3,}", t.lower())
        self._docs = [Counter(self._tok(c["text"])) for c in chunks]
        self._dl = [sum(d.values()) for d in self._docs]
        self._avdl = (sum(self._dl) / len(self._dl)) if self._dl else 1
        self._df = Counter()
        for d in self._docs: self._df.update(d.keys())
        self._N = len(chunks); self._math = math
        self._id2pos = {c["id"]: i for i, c in enumerate(chunks) if "id" in c}
        self._doc_id = doc_id
        # optional dense leg via ChromaDB (persistent = embed once, reuse)
        self._dense = None
        # Scans default to lexical (fast): the deterministic extraction that powers
        # fundamentals/flags/contradictions does not use embeddings, so paying ~90s
        # to embed every chunk on a scan is wasted. The dense leg is built lazily
        # for semantic Q&A (the Ask tab) via ensure_dense(). Set USE_DENSE=1 to
        # force embeddings at scan time.
        want_dense = os.environ.get("USE_DENSE", "0") == "1" if use_dense is None else use_dense
        if want_dense and os.environ.get("OFFLINE", "0") != "1":
            try:
                self._dense = _DenseLeg(chunks, doc_id or "adhoc",
                                        persist_dir or os.environ.get("DRHP_STORE", "drhp_store"))
            except Exception as e:
                self._dense = None
                print(f"  dense retrieval off ({type(e).__name__}) - "
                      f"running proven BM25+TF-IDF hybrid (no quality loss vs baseline)")
        self.backend = ("hybrid(tfidf+bm25+dense)" if self._dense
                        else "hybrid(tfidf+bm25)")

    def ensure_dense(self):
        """Build the dense-embedding leg on demand (used by semantic Q&A). Cheap
        to call repeatedly - only embeds the first time, then reuses the
        persistent ChromaDB collection. Returns True if dense is available."""
        if self._dense is not None:
            return True
        if os.environ.get("COMPLIANCE_MODE", "0") == "1":
            return False
        try:
            doc_id = getattr(self, "_doc_id", None) or "adhoc"
            self._dense = _DenseLeg(self.chunks, doc_id,
                                    os.environ.get("DRHP_STORE", "drhp_store"))
            self.backend = "hybrid(tfidf+bm25+dense)"
            return True
        except Exception:
            return False

    def _bm25(self, query):
        k1, b, m = 1.5, 0.75, self._math
        scores = [0.0] * self._N
        for term in set(self._tok(query)):
            df = self._df.get(term)
            if not df: continue
            idf = m.log(1 + (self._N - df + 0.5) / (df + 0.5))
            for i, d in enumerate(self._docs):
                tf = d.get(term)
                if tf:
                    scores[i] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * self._dl[i] / self._avdl))
        return scores

    def search(self, query, k=6, section=None, section_boost=False):
        import numpy as np
        t = self._cos(self.vec.transform([query]), self.mat)[0]
        bm = self._bm25(query)
        r1 = {i: r for r, i in enumerate(np.argsort(-t))}
        r2 = {i: r for r, i in enumerate(np.argsort(-np.array(bm)))}
        # dense ranks (position-indexed), if the leg is live
        r3 = {}
        if self._dense is not None:
            try:
                for rank, cid in enumerate(self._dense.rank(query, top=min(self._N, 50))):
                    pos = self._id2pos.get(cid)
                    if pos is not None: r3[pos] = rank
            except Exception:
                r3 = {}
        # section-aware boost (opt-in via section_boost=True). Applied to open-ended
        # Q&A where it raises precision, but NOT to the internal field/flag retrieval
        # that is already tuned - blanket-boosting there shifts which evidence chunks
        # surface and can drop a flag, so it stays off by default (no regression).
        boost_sec = section if section else (_infer_section(query) if section_boost else None)
        C = 60
        def rrf(i):
            s = 1/(C + r1[i]) + 1/(C + r2[i])
            if r3: s += 1/(C + r3.get(i, self._N))
            if boost_sec and self.chunks[i].get("section") == boost_sec:
                s *= 1.5                      # soft boost, not a hard filter
            return s
        fused = sorted(range(self._N), key=rrf, reverse=True)
        out = []
        for idx in fused:
            c = self.chunks[idx]
            if section and c["section"] != section: continue   # explicit hard filter
            out.append({**c, "score": round(rrf(idx), 4)})
            if len(out) >= k: break
        return out

    def sweep(self, section, pattern, limit=40):
        """COMPLETENESS mode: every chunk of a section, regex-matched sentences.
        Top-k retrieval trades recall for relevance; litigation and objects need
        the opposite - you cannot summarize 'all cases' from 6 chunks."""
        out = []
        for c in self.chunks:
            if c["section"] != section: continue
            for m in re.finditer(pattern, c["text"], re.I):
                s0 = max(0, m.start() - 100)
                out.append({"text": c["text"][s0:m.end() + 160].strip(),
                            "section": section, "page_start": c["page_start"],
                            "score": 1.0})
                if len(out) >= limit: return out
        return out


class _GeminiEmbed:
    """ChromaDB-compatible embedding function using Google's embedding API.
    Fast (network call, not local CPU) and high quality. Falls back handled by
    caller. Batches to respect API limits."""
    def __init__(self):
        self._key = os.environ.get("GEMINI_API_KEY", "").strip()
        self._model = os.environ.get("GEMINI_EMBED_MODEL", "models/text-embedding-004")
        if not self._key:
            raise RuntimeError("no GEMINI_API_KEY for embeddings")
    def name(self):
        return "gemini-text-embedding-004"
    def __call__(self, input):
        import urllib.request, json as _json
        vecs = []
        for text in input:
            body = {"model": self._model,
                    "content": {"parts": [{"text": text[:8000]}]}}
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/{self._model}:embedContent",
                data=_json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "x-goog-api-key": self._key})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = _json.loads(r.read())
            vecs.append(d["embedding"]["values"])
        return vecs


class _DenseLeg:
    """Dense-embedding retriever backed by a PERSISTENT ChromaDB collection.
    Persistence is the performance win: a document is embedded once and the
    vectors are reused on every later query/re-scan instead of re-embedding.

    Embedding backend priority: Gemini API (fast, if GEMINI_API_KEY set) ->
    a sentence-transformers model (DRHP_EMBED_MODEL) -> Chroma's built-in local
    MiniLM. The Gemini path makes scan-time embeddings viable (~2s, not ~60s
    on CPU), so dense retrieval can drive extraction, not just the Ask tab."""
    def __init__(self, chunks, doc_id, persist_dir):
        import chromadb
        self._client = chromadb.PersistentClient(path=persist_dir)
        name = f"drhp_{doc_id}"
        embed_fn = None
        model = os.environ.get("DRHP_EMBED_MODEL")
        if os.environ.get("USE_GEMINI_EMBED", "1") == "1" and os.environ.get("GEMINI_API_KEY"):
            try:
                embed_fn = _GeminiEmbed()
            except Exception:
                embed_fn = None
        if embed_fn is None and model:
            from chromadb.utils import embedding_functions
            embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model)
        # reuse an existing collection if it already holds this doc's chunks
        try:
            col = self._client.get_collection(name, embedding_function=embed_fn)
            if col.count() >= len(chunks):
                self.col = col
                return
            self._client.delete_collection(name)
        except Exception as _sw:
            drhp_log.swallowed("drhp_ingest", _sw)
            pass
        self.col = self._client.create_collection(
            name, embedding_function=embed_fn, metadata={"hnsw:space": "cosine"})
        B = 100
        cl = list(chunks)
        for i in range(0, len(cl), B):
            batch = cl[i:i+B]
            self.col.add(
                ids=[c["id"] for c in batch],
                documents=[c["text"] for c in batch],
                metadatas=[{"section": c["section"], "page_start": c["page_start"]}
                           for c in batch])

    def rank(self, query, top=50):
        r = self.col.query(query_texts=[query], n_results=top)
        return r["ids"][0] if r and r.get("ids") else []


def build_index(path, prefer_chroma=True):
    """Full ingest: file -> chunks -> three-way hybrid index. Returns (index, meta, pages)."""
    pages = extract_pages(path)
    chunks = chunk_document(pages)
    if not chunks:
        raise ValueError("No readable text found. Scanned-image PDFs need OCR; "
                         "try the text-based DRHP from NSE/SEBI.")
    doc_id = hashlib.md5(Path(path).name.encode()).hexdigest()[:10]
    index = TfidfIndex(chunks, doc_id=doc_id)
    index.raw_pages = pages
    sections = sorted({c["section"] for c in chunks})
    meta = {"pages": len(pages), "chunks": len(chunks),
            "sections_found": sections, "backend": index.backend}
    return index, meta, pages
