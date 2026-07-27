import drhp_log
"""
drhp_semantic.py - semantic search ACROSS every prospectus ever scanned.

WHY THIS EXISTS
---------------
Every other retrieval path in this project searches ONE document, and does it
lexically, because inside a single prospectus the target text is keyword-exact
("Revenue from operations" is literally the row label) and the SEBI-mandated
chapter structure already narrows the search before ranking begins.

Across documents that argument collapses. The same risk is worded differently by
every issuer:

    Kusumgar : "counterparty credit risk ... payment delays or defaults by customers"
    ICEL     : "loss or significant reduction of a major client"
    Alpine   : "revenue depends on a handful of customers with no long-term contracts"

Those three sentences share almost no content words. Lexical search cannot
connect them at any k. This is the one retrieval problem in the project that
sparse methods structurally cannot solve, which is why embeddings are justified
HERE and were not justified for line-item extraction.

WHAT IT ENABLES
    "which issuers disclose customer-concentration risk?"
    "find precedents for debt-funded capacity expansion"
    "who else had an auditor emphasis-of-matter on related-party loans?"

DESIGN NOTES
    One SHARED ChromaDB collection for the whole library, not one per document -
    the entire point is querying across issuers, and per-document collections
    would force a fan-out query and manual result merging.

    Chunks are keyed <doc_id>:<n> and carry issuer/page/section metadata, so a
    hit is always attributable to a specific page of a specific prospectus. A
    cross-document answer that cannot be traced back to a page is worthless in
    this domain.

    If no semantic backend is available the feature reports itself UNAVAILABLE
    and returns nothing. It deliberately does NOT fall back to a bag-of-words
    vector: that would return confident cross-document matches that are merely
    lexical, i.e. exactly the results this module exists to improve on. A
    feature that silently degrades into the thing it replaces is worse than one
    that says it is off.
"""

import os

_COLLECTION = "drhp_library"


# --------------------------------------------------------------- embeddings
class _OllamaEmbed:
    """Local embeddings through Ollama. Free, offline, no API key, no quota.

    Chosen as the preferred local backend because the alternative -
    sentence-transformers - pulls a multi-hundred-MB torch stack and downloads
    model weights on first use, neither of which is acceptable on a 512MB
    hosting tier or a laptop about to be demoed on conference wifi."""
    @staticmethod
    def name():
        return "ollama"

    def __init__(self, model=None):
        self._model = model or os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        self._url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
        self._probe()

    def _probe(self):
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            f"{self._url}/api/embeddings",
            data=_json.dumps({"model": self._model, "prompt": "ping"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            _json.loads(r.read())

    def __call__(self, input):
        import json as _json
        import urllib.request
        out = []
        for t in input:
            req = urllib.request.Request(
                f"{self._url}/api/embeddings",
                data=_json.dumps({"model": self._model, "prompt": t[:8000]}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                out.append(_json.loads(r.read())["embedding"])
        return out

    # ChromaDB 1.x embeds DOCUMENTS via __call__ but QUERIES via embed_query.
    # Implementing only __call__ indexes fine and then fails on every search -
    # a split that is invisible until something is actually queried.
    def embed_query(self, input):
        return self(input)

    def embed_documents(self, input):
        return self(input)



class _GeminiEmbed:
    """Hosted embeddings. Fast and needs no local model, but consumes the same
    free-tier quota the analysis layers depend on, so it ranks below Ollama when
    both are present - the local one costs nothing we care about."""
    @staticmethod
    def name():
        return "gemini"

    def __init__(self):
        self._key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not self._key:
            raise RuntimeError("no GEMINI_API_KEY")
        self._model = os.environ.get("GEMINI_EMBED_MODEL", "models/text-embedding-004")

    def __call__(self, input):
        import json as _json
        import urllib.request
        out = []
        for t in input:
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/{self._model}:embedContent",
                data=_json.dumps({"model": self._model,
                                  "content": {"parts": [{"text": t[:8000]}]}}).encode(),
                headers={"Content-Type": "application/json", "x-goog-api-key": self._key})
            with urllib.request.urlopen(req, timeout=30) as r:
                out.append(_json.loads(r.read())["embedding"]["values"])
        return out

    # ChromaDB 1.x embeds DOCUMENTS via __call__ but QUERIES via embed_query.
    # Implementing only __call__ indexes fine and then fails on every search -
    # a split that is invisible until something is actually queried.
    def embed_query(self, input):
        return self(input)

    def embed_documents(self, input):
        return self(input)



def backend(_override=None):
    """Resolve an embedding backend, or None.

    Order is deliberate: a free local model outranks a hosted one that spends
    the quota our analysis layers need, and both outrank a large local download.
    Returning None (rather than something weaker) is a supported outcome."""
    if _override is not None:
        return _override
    for make in (_OllamaEmbed, _GeminiEmbed):
        try:
            return make()
        except Exception as _sw:
            drhp_log.swallowed("drhp_semantic", _sw)
            continue
    try:
        from chromadb.utils import embedding_functions
        model = os.environ.get("DRHP_EMBED_MODEL")
        if model:
            return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model)
    except Exception as _sw:
        drhp_log.swallowed("drhp_semantic", _sw)
        pass
    return None


# --------------------------------------------------------------- collection
def _collection(persist_dir=None, embed=None):
    import chromadb
    path = persist_dir or os.environ.get(
        "DRHP_VECTOR_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectors"))
    client = chromadb.PersistentClient(path=path)
    # get_or_create is the correct call here: a try/get + except/create race
    # loses to itself, because a failed get for ANY reason (not just absence)
    # sends us into create, which then fails with "already exists" and takes the
    # whole feature down. cosine space because document length varies hugely
    # between issuers and we care about direction (topic), not magnitude.
    return client.get_or_create_collection(
        _COLLECTION, embedding_function=embed,
        metadata={"hnsw:space": "cosine"})


def index_document(doc_id, issuer, chunks, embed=None, persist_dir=None, limit=400):
    """Add one prospectus to the shared library collection.

    `limit` caps how many chunks are embedded per document. Cross-document
    questions are about disclosures - risks, litigation, related-party dealings -
    so narrative chunks are worth far more here than the hundreds of pages of
    statutory boilerplate every RHP carries. Embedding all ~1,200 chunks would
    multiply cost for material that will never be a useful cross-issuer answer.

    Returns {"status": ..., "added": n} and never raises: indexing is an
    enhancement, and a scan must not fail because a vector store is unavailable.
    """
    emb = backend(embed)
    if emb is None:
        return {"status": "UNAVAILABLE", "added": 0,
                "note": "no embedding backend (try: ollama pull nomic-embed-text)"}
    try:
        col = _collection(persist_dir, emb)
        existing = col.get(where={"doc_id": doc_id}, limit=1)
        if existing and existing.get("ids"):
            return {"status": "already_indexed", "added": 0}

        WANTED = {"risk_factors", "litigation", "related_party", "business",
                  "objects", "promoters", "mdna"}
        picked = [c for c in chunks if c.get("section") in WANTED] or list(chunks)
        picked = picked[:limit]

        col.add(
            ids=[f"{doc_id}:{i}" for i in range(len(picked))],
            documents=[c["text"][:2000] for c in picked],
            metadatas=[{"doc_id": doc_id, "issuer": issuer,
                        "section": c.get("section", ""),
                        "page": c.get("page_start", 0)} for c in picked])
        return {"status": "indexed", "added": len(picked)}
    except Exception as e:
        return {"status": "UNAVAILABLE", "added": 0,
                "note": f"{type(e).__name__}: {str(e)[:120]}"}


def search_library(query, k=8, exclude_doc=None, embed=None, persist_dir=None):
    """Semantic search across every indexed prospectus.

    `exclude_doc` supports the precedent use case: when asking "who else
    disclosed this?", the document you are reading is not a precedent for
    itself, and leaving it in would fill the results with near-duplicates of
    the passage you started from.

    Returns {"status", "hits":[{issuer, page, section, text, score}]}. Chroma
    returns cosine DISTANCE; it is converted to a similarity so a bigger number
    means a better match, which is what a caller and a UI expect.
    """
    emb = backend(embed)
    if emb is None:
        return {"status": "UNAVAILABLE", "hits": [],
                "note": "no embedding backend available"}
    try:
        col = _collection(persist_dir, emb)
        where = {"doc_id": {"$ne": exclude_doc}} if exclude_doc else None
        r = col.query(query_texts=[str(query)[:2000]],
                      n_results=max(1, k), where=where)
        hits = []
        for i, doc in enumerate(r.get("documents", [[]])[0]):
            md = r.get("metadatas", [[]])[0][i] or {}
            dist = (r.get("distances") or [[None]])[0][i]
            hits.append({"issuer": md.get("issuer", "?"),
                         "page": md.get("page"),
                         "section": md.get("section", ""),
                         "text": doc[:400],
                         "score": round(1.0 - dist, 3) if dist is not None else None})
        return {"status": "ok" if hits else "none_found", "hits": hits}
    except Exception as e:
        return {"status": "UNAVAILABLE", "hits": [],
                "note": f"{type(e).__name__}: {str(e)[:120]}"}


def find_precedents(tension, doc_id=None, k=4, embed=None, persist_dir=None):
    """Given a tension this document raised, find how OTHER issuers worded the
    same pattern.

    This is the payoff of a shared collection. A tension on its own is an
    assertion about one company; the same pattern found in three other
    prospectuses is a base rate. The query is built from the tension's own
    title and claim text, so no new vocabulary has to be invented.
    """
    q = " ".join(str(tension.get(x, "")) for x in ("title", "a", "b", "question"))
    out = search_library(q, k=k, exclude_doc=doc_id, embed=embed, persist_dir=persist_dir)
    out["for_tension"] = tension.get("title")
    return out
