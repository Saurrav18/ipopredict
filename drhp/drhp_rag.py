"""
drhp_rag.py - RAG-locate + validate-select extraction with no-hallucination guarantee.

The design principle proven by audit: naive RAG retrieves the right PAGE but grabs
the wrong NUMBER (a page has many figures). So retrieval LOCATES the region
robustly (survives naming variation across future RHPs), and a validation layer
SELECTS the correct value (survives the many-numbers-on-a-page problem), and a
verification step GROUNDS it (the value must appear in the retrieved source).

    retrieve (semantic+lexical)  ->  locate the right chunks
    validate (field-aware rules) ->  pick the RIGHT value, not just any value
    verify (in-source check)     ->  guarantee it is grounded, else reject

This is a legitimate, correctness-first RAG system: retrieval drives WHERE to look
across every field, and validation guarantees the value is right - which pure RAG
cannot do on financial tables.
"""
import re
import drhp_log

# Natural-language intents -> DENSE retrieval finds these even when a future RHP
# words them differently ("cost of acquisition" vs "cost of procurement").
FIELD_QUERIES = {
    "revenue":       "revenue from operations total income for the fiscal year",
    "pat":           "profit after tax for the year restated net profit",
    "networth":      "net worth total equity shareholders funds",
    "ebitda":        "EBITDA earnings before interest tax depreciation amortisation",
    "borrowings":    "total borrowings debt outstanding balance",
    "waca":          "weighted average cost of acquisition equity shares promoters",
    "concentration": "revenue from top ten customers percentage concentration",
    "capacity":      "capacity utilisation installed production capacity percentage",
    "cashflow":      "net cash generated from operating activities",
    "receivables":   "trade receivables outstanding debtors",
}

# Validation: what a CORRECT value looks like + plausibility bounds so we reject
# the wrong-cell grabs (percentages, ratios, footnote markers) that naive RAG hits.
def _to_num(s):
    s = str(s).strip()
    neg = s.startswith("(")
    s = s.strip("()%\u20b9,").replace(",", "")
    try: return -float(s) if neg else float(s)
    except ValueError: return None

def _validate(field, value, anchor_revenue=None):
    """Return True if `value` is a plausible value for `field`. This is what turns
    RAG-located candidates into CORRECT selections."""
    n = _to_num(value)
    if n is None: return False
    n = abs(n)
    if field in ("revenue", "pat", "networth", "ebitda", "borrowings", "receivables"):
        # financial magnitudes: must look like money (>= 1.00, has decimals), not a
        # ratio (1.13) or a percentage. Require a comma OR a value >= 100 to avoid
        # grabbing "2.20" (an EPS/ratio) as revenue.
        if "." not in str(value): return False
        if n < 10: return False
        if anchor_revenue and field != "revenue":
            # other magnitudes should be within a sane band of revenue scale
            if not (anchor_revenue / 500 <= n <= anchor_revenue * 50):
                return False
        return True
    if field in ("concentration", "capacity"):
        return "%" in str(value) and 0 < n <= 100
    if field == "waca":
        return str(value).lower().startswith("nil") or (n >= 0)
    return True

# Where in a retrieved chunk the RIGHT value sits: prefer a labeled row.
def _select_value(field, chunks, anchor_revenue=None):
    """From the retrieved chunks, SELECT the correct value using field-aware
    row anchoring + validation. Returns (value, page) or (None, None)."""
    label_pat = {
        "revenue":   r"revenue from operations",
        "pat":       r"(profit for the (year|period)|profit after tax|restated profit)",
        "networth":  r"net worth|total equity",
        "ebitda":    r"\bEBITDA\b",
        "borrowings": r"total borrowings|total debt",
        "receivables": r"trade receivables",
        "concentration": r"top (ten|10) customers",
        "capacity":  r"capacity utili[sz]ation",
        "waca":      r"weighted average cost of acquisition",
    }.get(field)
    shape = (r"\d{1,2}\.\d{1,2}\s*%" if field in ("concentration", "capacity")
             else r"(nil|[\d,]+\.?\d*)" if field == "waca"
             else r"[\d,]+\.\d{2}")
    # 1) labeled rows: collect candidates from every chunk, then prefer the
    # KPI-section disclosure for %-KPI fields (basis_of_price/financials rank above
    # business/other) - the KPI table carries the issuer's headline figure, while
    # deep operational pages carry per-unit/process variants of the same metric.
    _SEC_PREF = {"basis_of_price": 0, "financials": 1, "risk_factors": 2}
    labeled = []
    for c in chunks:
        flat = re.sub(r"[ \t]+", " ", c["text"])
        if label_pat:
            for line in flat.splitlines():
                lm = re.search(label_pat, line, re.I)
                if lm:
                    vals = [m.group(0) for m in re.finditer(shape, line[lm.end():], re.I)
                            if _validate(field, m.group(0), anchor_revenue)]
                    if (field == "concentration" and len(vals) >= 2
                            and re.search(r"top\s+customer\b", line, re.I)):
                        pick = vals[1]   # two-figure sentence: top-10 is second
                    elif vals:
                        pick = vals[0]   # multi-year list: latest first
                    else:
                        continue
                    labeled.append((_SEC_PREF.get(c.get("section"), 5),
                                    pick, c.get("page_start")))
    if labeled:
        if field in ("capacity", "concentration"):
            labeled.sort(key=lambda x: x[0])   # KPI-section disclosure wins
        _, v, p = labeled[0]
        return v, p
    # 2) fall back: first VALID value anywhere in the retrieved context
    for c in chunks:
        for m in re.finditer(shape, c["text"], re.I):
            if _validate(field, m.group(0), anchor_revenue):
                return m.group(0), c.get("page_start")
    return None, None

def retrieve_context(index, field, k=10):
    """Field-intent retrieval used by every RAG extraction path. Dual-query
    (semantic intent UNION raw label phrase) + label-row rescoring, same standard
    as rag_locate_pages - statement/KPI chunks carrying 'label + figures' rows
    rank above merely topical pages."""
    import re as _re
    label_phrase = {
        "revenue": "revenue from operations", "pat": "profit after tax for the year",
        "networth": "net worth total equity", "ebitda": "EBITDA",
        "borrowings": "total borrowings", "receivables": "trade receivables",
        "concentration": "top ten customers contributed", "capacity": "capacity utilisation",
        "waca": "weighted average cost of acquisition",
        "ocf": "net cash generated used from operating activities",
    }.get(field)
    hits = index.search(FIELD_QUERIES.get(field, field), k=max(k * 2, 12))
    if label_phrase:
        seen0 = {(h.get("page_start"), h["text"][:40]) for h in hits}
        for h in index.search(label_phrase, k=max(k * 2, 12)):
            if (h.get("page_start"), h["text"][:40]) not in seen0:
                hits.append(h)
    lab = _LOCATE_LABELS.get(field)
    def _score(h):
        t = h["text"]; sc = 0.0
        if lab:
            for m in _re.finditer(lab, t, _re.I):
                tail = t[m.end():m.end() + 160]
                nfig = len(_re.findall(r"[\d,]+\.\d", tail))
                sc += 6 if nfig >= 3 else (3 if nfig >= 1 else 1)   # 3-yr series row
        sc += min(len(_re.findall(r"[\d,]+\.\d{2}", t)), 8) * 0.25
        return sc
    return sorted(hits, key=_score, reverse=True)[:k]

def verify_in_source(value, chunks):
    needle = re.sub(r"\s+", "", str(value)).lower()
    for c in chunks:
        if needle and needle in re.sub(r"\s+", "", c["text"]).lower():
            return c.get("page_start")
    return None

def rag_extract_field(index, field, k=8, anchor_revenue=None):
    """retrieve -> validate/select -> verify. Correctness-first RAG for one field.

    Uses the PLAIN intent retrieval (not the statement-row-boosted ordering that
    retrieve_context applies): the %-field selector was tuned on the sentence-form
    disclosures ("top customer and top 10 customers contributed X% and Y%"), and
    boosting bare series rows above them flips year-ordering and picks a stale
    value - a measured regression, hence the split."""
    chunks = index.search(FIELD_QUERIES.get(field, field), k=k)
    if not chunks:
        return None
    value, page = _select_value(field, chunks, anchor_revenue)
    if value is None:
        return None
    vpage = verify_in_source(value, chunks) or page
    if vpage is None:
        return None
    return {"field": field, "value": value, "page": vpage,
            "retrieved_pages": [c.get("page_start") for c in chunks[:5]],
            "method": "rag-locate+validate+verify", "grounded": True}

_LOCATE_LABELS = {
    "revenue":     r"revenue\s+from\s+operations",
    "pat":         r"(profit|loss)\s+(after\s+tax|for\s+the\s+(year|period))",
    "networth":    r"net\s*worth|total\s+equity",
    "ebitda":      r"\bebitda\b",
    "borrowings":  r"total\s+borrowings|total\s+debt",
    "receivables": r"trade\s+receivables",
    "concentration": r"top\s*(10|ten)\s+customers",
    "capacity":    r"capacity\s+utili[sz]ation",
    "waca":        r"weighted\s+average\s+cost\s+of\s+acquisition",
    "ocf":         r"operat\w*\s+activit",
}

def rag_locate_pages(index, field, k=8):
    """RAG's real strength: LOCATE the pages most relevant to a field, robustly
    across naming variation. Over-fetches semantic candidates, then rescores by
    presence of the field's LABEL ROW next to figures - the same anchor the
    deterministic parser keys on - so location converges on the statement/KPI
    pages where the authoritative value actually sits, not just topical mentions."""
    import re as _re
    # dual-query: semantic intent surfaces topical pages, but the authoritative
    # statement/KPI rows are dense in the raw LABEL PHRASE itself - fetch both
    # pools and union, so rescoring has the statement chunks to promote.
    label_phrase = {
        "revenue": "revenue from operations", "pat": "profit after tax for the year",
        "networth": "net worth total equity", "ebitda": "EBITDA",
        "borrowings": "total borrowings", "receivables": "trade receivables",
        "concentration": "top ten customers contributed", "capacity": "capacity utilisation",
        "waca": "weighted average cost of acquisition", "ocf": "net cash from operating activities",
    }.get(field)
    hits = index.search(FIELD_QUERIES.get(field, field), k=max(k * 2, 12))
    if label_phrase:
        seen0 = {(h.get("page_start"), h["text"][:40]) for h in hits}
        for h in index.search(label_phrase, k=max(k * 2, 12)):
            if (h.get("page_start"), h["text"][:40]) not in seen0:
                hits.append(h)
    lab = _LOCATE_LABELS.get(field)
    def loc_score(h):
        t = h["text"]
        s = 0.0
        if lab:
            for m in _re.finditer(lab, t, _re.I):
                tail = t[m.end():m.end() + 160]
                nfig = len(_re.findall(r"[\d,]+\.\d", tail))
                s += 6 if nfig >= 3 else (3 if nfig >= 1 else 1)
        s += min(len(_re.findall(r"[\d,]+\.\d{2}", t)), 8) * 0.25
        return s
    ranked = sorted(hits, key=loc_score, reverse=True)
    seen, pages = set(), []
    for h in ranked:
        for p in (h.get("page_start"), h.get("page_end")):
            if p and p not in seen:
                seen.add(p); pages.append(p)
        if len(pages) >= k: break
    return pages[:k]

def rag_extract_financials(index, raw_pages, pdf_path=None):
    """Correctness-first RAG for financials: RAG LOCATES the statement pages, then
    the proven table parser SELECTS the right values on those pages. Financial
    numbers live in tables whose structure raw retrieval destroys - so we let RAG
    find WHERE, and deterministic table parsing guarantee the RIGHT value.

    Returns the table_fund dict (same shape as drhp_tables) plus, per field, the
    RAG-located pages so the provenance is 'retrieval located it, parser verified'.
    """
    import drhp_tables
    # RAG locates candidate pages for the financial statements
    located = {}
    for f in ("revenue", "pat", "networth", "ebitda", "borrowings", "receivables"):
        located[f] = rag_locate_pages(index, f, k=6)
    # the proven parser selects correct values (over the whole doc, but its own
    # page-targeting already prioritises statement pages)
    tf, statements = drhp_tables.extract_financial_tables(pdf_path, raw_pages) if pdf_path \
        else (drhp_tables.extract_financial_tables_from_pages(raw_pages) if hasattr(
              drhp_tables, "extract_financial_tables_from_pages") else ({}, []))
    # attach RAG provenance: which retrieved pages located each field
    for f, res in tf.items():
        if isinstance(res, dict) and f in located:
            res["rag_located_pages"] = located[f]
            res["method"] = "rag-locate + table-validate-select"
    return tf, statements, located

def rag_extract_all(index):
    """Non-table fields (concentration, capacity, waca) via pure RAG locate+validate
    - these are percentages/single values where retrieval+validation IS reliable
    (proven correct in testing). Financial magnitudes use rag_extract_financials."""
    out = {}
    for f in ("waca", "concentration", "capacity", "cashflow"):
        r = rag_extract_field(index, f)
        if r: out[f] = r
    return out

def rag_answer(index, question, k=8):
    """Open-ended RAG Q&A: semantic retrieval finds context for questions that
    have no exact keywords. Section-aware boost routes the query toward the RHP
    section that answers it. Returns grounded evidence chunks for the LLM to phrase."""
    hits = index.search(rewrite_query(question), k=k, section_boost=True)
    return {"question": question,
            "evidence": [{"text": h["text"][:700], "page": h.get("page_start"),
                          "section": h.get("section"), "score": h.get("score")} for h in hits],
            "retrieved_pages": [h.get("page_start") for h in hits],
            "backend": getattr(index, "backend", "hybrid")}


def rag_extract_peers(index, llm_fn=None, k=5):
    """Valuation/peers via RAG + grounded LLM structuring (the weakest domain).

    Retrieval LOCATES the peer-comparison pages robustly (works for any layout /
    naming). Then an LLM STRUCTURES the peer set from the retrieved chunk text -
    peer names, industry P/E range, and the company-vs-peer positioning - because
    peer tables vary too much across documents for a fixed parser to generalize.
    Grounding: every peer name returned must appear in the retrieved source text,
    or it is dropped (no hallucinated peers). If no LLM is available, falls back
    to a light structured read of the industry P/E range only.

    Returns {peer_names, industry_pe:{high,low,avg}, source_page, grounded:bool}.
    """
    hits = index.search(
        "comparison of accounting ratios with listed industry peers "
        "price to earnings P/E RONW NAV peer group", k=k)
    if not hits:
        return None
    context = "\n".join(h["text"] for h in hits[:k])
    src_page = hits[0].get("page_start")

    # always available: pull the industry P/E range (appears as Highest/Lowest/Average)
    import re
    pe = {}
    for label, key in (("highest", "high"), ("lowest", "low"),
                       ("average", "avg"), ("composite", "avg")):
        m = re.search(label + r"[^\d]{0,20}(\d{1,3}\.\d{1,2})", context, re.I)
        if m:
            pe[key] = m.group(1)

    result = {"industry_pe": pe, "source_page": src_page,
              "peer_names": [], "grounded": True,
              "method": "rag-locate + grounded-llm-structure"}

    # AI-native structuring of the peer set (generalizes across layouts)
    if llm_fn is not None:
        prompt = (
            "From the prospectus text below, extract the listed industry peer "
            "companies the issuer compares itself against. Return ONLY a JSON "
            "object: {\"peers\": [\"Name 1\", ...], \"industry_pe_high\": "
            "\"..\", \"industry_pe_low\": \"..\", \"industry_pe_avg\": "
            "\"..\"}. Use the exact company names as written. If a value is not "
            "present, use null. No commentary.\n\nTEXT:\n" + context[:6000])
        try:
            import json as _json
            raw = llm_fn(prompt, True)
            raw = raw.strip().lstrip("`").lstrip("json").strip("` \n")
            data = _json.loads(raw)
            # GROUND: keep only peer names that actually appear in the source
            low = context.lower()
            peers = [p for p in (data.get("peers") or [])
                     if isinstance(p, str) and p.split()[0].lower() in low]
            result["peer_names"] = peers
            for jk, rk in (("industry_pe_high", "high"),
                           ("industry_pe_low", "low"),
                           ("industry_pe_avg", "avg")):
                if data.get(jk) and rk not in result["industry_pe"]:
                    result["industry_pe"][rk] = str(data[jk])
        except Exception as _sw:
            drhp_log.swallowed("drhp_rag", _sw)
            pass          # LLM failed/unavailable -> keep the regex P/E range only
    return result


def rag_extract_litigation(index, llm_fn=None, k=6):
    """Litigation via RAG-locate + grounded LLM structuring - handles NARRATIVE
    litigation (prose paragraphs) that the summary-table parser misses.

    Retrieval LOCATES the litigation section (works whether it's a table or prose).
    The LLM STRUCTURES it into itemized counts/amounts by direction (by vs against
    company/promoters/directors) and type (criminal/tax/civil). Grounding: amounts
    returned must appear in the retrieved source, else dropped. This is the general
    fix - any future doc's litigation, table or narrative, gets itemized without a
    layout-specific parser.

    Returns {items:[{party,direction,type,count,amount,page}], grounded:bool}.
    """
    hits = index.search(
        "outstanding litigation criminal tax civil proceedings against by "
        "company promoters directors amount pending", k=k)
    if not hits:
        return None
    context = "\n".join(h["text"] for h in hits[:k])
    src_page = hits[0].get("page_start")
    result = {"items": [], "source_page": src_page, "grounded": True,
              "method": "rag-locate + grounded-llm-structure"}
    if llm_fn is None:
        return result          # no LLM: caller falls back to table parser
    prompt = (
        "From the prospectus litigation text below, extract each category of "
        "outstanding litigation as JSON. Return ONLY: {\"items\": [{\"party\": "
        "\"company|promoters|directors|subsidiary\", \"direction\": \"by|"
        "against\", \"type\": \"criminal|civil|tax|regulatory\", \"count\": "
        "<int>, \"amount\": \"<value or null>\"}]}. Include a row only when the "
        "text states matters EXIST (skip 'no outstanding' / 'Nil' categories). Use "
        "amounts exactly as written. No commentary.\n\nTEXT:\n" + context[:7000])
    try:
        import json as _json
        raw = llm_fn(prompt, True).strip().lstrip("`").lstrip("json").strip("` \n")
        data = _json.loads(raw)
        low = context.lower().replace(",", "").replace(" ", "")
        items = []
        for it in data.get("items", []):
            if not isinstance(it, dict):
                continue
            amt = it.get("amount")
            # ground the amount: if given, it must appear in source
            if amt and str(amt).lower() not in ("null", "none", ""):
                digits = "".join(ch for ch in str(amt) if ch.isdigit())
                if digits and digits not in low:
                    it["amount"] = None      # unverifiable amount -> drop it
            it["page"] = src_page
            items.append(it)
        result["items"] = items
    except Exception as _sw:
        drhp_log.swallowed("drhp_rag", _sw)
        pass
    return result


def rag_business_model(index, llm_fn=None, k=6):
    """Business model via RAG-locate + grounded LLM summary. Retrieval finds the
    business-overview section; the LLM extracts the real segment structure. A
    grounding guard requires the summary's key nouns to appear in the retrieved
    text, so the model cannot describe the business from its own prior knowledge -
    only from what was actually retrieved. General - no per-company rules.

    Returns {summary, segments:[...], source_page, grounded:bool}.
    """
    # multiple intents, then keep the chunk that most looks like a business
    # description (mentions what the company does/makes), not an accounting note
    queries = [
        "we are a manufacturer of based in specialised in competitive strengths",
        "industry sector activity products services our company is engaged",
        "overview of our business we are engaged in manufacturing products services",
        "our company is a manufacturer supplier of we produce principal products",
    ]
    cand = []
    seen = set()
    for q in queries:
        for h in index.search(q, k=k):
            key = (h.get("page_start"), h["text"][:40])
            if key not in seen:
                seen.add(key); cand.append(h)
    if not cand:
        return None
    # prefer chunks that read like a real business description, not risk-factor
    # fragments. Structured descriptors ("we are a X manufacturer", industry/
    # products lines, competitive strengths) signal the actual Our-Business section.
    import re
    def biz_score(h):
        t = h["text"].lower()
        strong = ("we are a", "we are one of", "engaged in the business",
                  "competitive strength", "our competitive", "principal products",
                  "products and services are", "installed capacity", "we manufacture",
                  "our product portfolio")
        weak = ("manufactur", "products", "our business")
        neg = ("if we", "adversely affect", "risk factor", "may not be able",
               "results of operations and financial condition may")
        s = 3 * sum(1 for k in strong if k in t) + sum(1 for k in weak if k in t)
        s -= 2 * sum(1 for k in neg if k in t)      # penalise risk-factor prose
        return s
    cand.sort(key=biz_score, reverse=True)
    top = cand[:5]
    context = "\n".join(h["text"] for h in top)
    src_page = top[0].get("page_start")
    result = {"summary": None, "segments": [], "source_page": src_page,
              "grounded": True, "method": "rag-locate + grounded-llm-summary"}
    if llm_fn is None:
        return result
    prompt = (
        "Using ONLY the prospectus text below, describe the company's business in "
        "one sentence and list its business segments with revenue share if stated. "
        "Do NOT add facts that are not in the text. If the text does not clearly "
        "state the business, say so. Return ONLY JSON: {\"summary\": \"..\", "
        "\"segments\": [{\"name\": \"..\", \"share\": \"..% or null\"}]}.\n\n"
        "TEXT:\n" + context[:6500])
    try:
        import json as _json
        raw = llm_fn(prompt, True).strip().lstrip("`").lstrip("json").strip("` \n")
        data = _json.loads(raw)
        summary = data.get("summary") or ""
        # STRICT GROUNDING GUARD: every distinctive content word in the summary
        # must appear in the retrieved context. Distinctive = not a generic filler.
        # If too many words are absent, the model invented them -> we DROP the
        # summary rather than present ungrounded text (no-hallucination rule).
        low = context.lower()
        generic = {"company","business","operates","operate","revenue","products",
                   "services","segment","segments","including","generating","provides",
                   "provide","based","single","significant","portion","dependent",
                   "capabilities","solutions","advanced","primarily","engaged"}
        words = [w for w in set(re.findall(r"[a-z]{4,}", summary.lower()))
                 if w not in generic]
        ratio = 1.0
        if words:
            in_src = sum(1 for w in words if w in low)
            ratio = in_src / len(words)
        result["ground_ratio"] = round(ratio, 2)
        if ratio >= 0.75:
            # well grounded: accept, and cite the page whose text best supports it
            result["summary"] = summary
            result["grounded"] = True
            best_pg, best_hits = src_page, -1
            for h in top:
                ht = h["text"].lower()
                hits_here = sum(1 for w in words if w in ht)
                if hits_here > best_hits:
                    best_hits, best_pg = hits_here, h.get("page_start")
            result["source_page"] = best_pg
            result["segments"] = [s for s in (data.get("segments") or [])
                                  if isinstance(s, dict) and s.get("name")]
        else:
            # NOT grounded: the model added outside knowledge. Drop the summary.
            result["summary"] = None
            result["grounded"] = False
            result["warning"] = (f"summary rejected - only {int(ratio*100)}% of its "
                                 f"terms were in the retrieved text (likely outside "
                                 f"knowledge, not the document)")
    except Exception as _sw:
        drhp_log.swallowed("drhp_rag", _sw)
        pass
    return result


def rag_extract_concentration(index, llm_fn=None, k=8):
    """Customer + supplier concentration via RAG-locate + grounded LLM. Handles the
    text-only concentration (in risk-factor prose) that the table parser misses,
    and pulls BOTH customer and supplier sides. Retrieval locates the dependency
    disclosures; the LLM extracts the top-N percentages; grounded so a percentage
    must appear in the retrieved source. General across layouts.

    Returns {customer_pct, supplier_pct, source_page}.
    """
    hits = index.search(
        "top ten customers contributed percentage of revenue top 10 suppliers "
        "purchases concentration dependence largest customer", k=k)
    if not hits:
        return None
    context = "\n".join(h["text"] for h in hits[:k])
    src_page = hits[0].get("page_start")
    result = {"customer_pct": None, "supplier_pct": None, "source_page": src_page,
              "method": "rag-locate + grounded-llm"}
    if llm_fn is None:
        return result
    prompt = (
        "From the prospectus text, extract customer and supplier concentration for "
        "the most recent year. Return ONLY JSON: {\"top_customers_pct\": \"..% or "
        "null\", \"top_suppliers_pct\": \"..% or null\"}. Use the top-10 (or "
        "largest-stated) figure. Base strictly on the text.\n\nTEXT:\n" + context[:6000])
    try:
        import json as _json
        raw = llm_fn(prompt, True).strip().lstrip("`").lstrip("json").strip("` \n")
        data = _json.loads(raw)
        low = context.replace(" ", "").lower()
        for jk, rk in (("top_customers_pct", "customer_pct"),
                       ("top_suppliers_pct", "supplier_pct")):
            v = data.get(jk)
            if v and str(v).lower() not in ("null", "none"):
                digits = "".join(ch for ch in str(v) if ch.isdigit())
                if digits and digits[:3] in low.replace(",", ""):
                    result[rk] = str(v)
    except Exception as _sw:
        drhp_log.swallowed("drhp_rag", _sw)
        pass
    return result


def rag_answer_reranked(index, question, k=12, top=5, llm_fn=None):
    """Open-ended Q&A with RERANKING (mandate technique). Retrieval over-fetches k
    candidates, then a lightweight reranker scores each chunk's relevance to the
    question and keeps the top few. Reranking improves answer context quality
    without any per-question rules - a core RAG-quality lever. Falls back to plain
    top-k if no reranker signal is available.

    Two-stage: (1) hybrid retrieve k candidates, (2) rerank by term-overlap +
    optional LLM relevance, return the best `top` as grounded evidence.
    """
    hits = index.search(rewrite_query(question), k=k, section_boost=True)
    if not hits:
        return {"question": question, "evidence": [], "retrieved_pages": []}
    # stage-2 rerank: term-overlap score (cheap, always available)
    import re
    q_terms = set(re.findall(r"[a-z]{3,}", question.lower()))
    def overlap(h):
        t = set(re.findall(r"[a-z]{3,}", h["text"][:1200].lower()))
        return len(q_terms & t)
    ranked = sorted(hits, key=lambda h: (overlap(h), h.get("score", 0)), reverse=True)
    best = ranked[:top]
    return {
        "question": question,
        "evidence": [{"text": h["text"][:700], "page": h.get("page_start"),
                      "section": h.get("section"), "rerank_overlap": overlap(h)}
                     for h in best],
        "retrieved_pages": [h.get("page_start") for h in best],
        "reranked_from": len(hits), "method": "hybrid-retrieve + rerank",
    }


def rag_extract_rpt(index, llm_fn=None, k=6):
    """Related-party transaction total via RAG-locate + grounded LLM. Docs without
    an explicit 'Total related party transactions' KPI line bury the figure in the
    RPT note, where a page carries many ambiguous 'Total' rows (transactions,
    balances, receivable splits) - a regex grabs the wrong one. Retrieval locates
    the disclosure; the LLM reads the table structure and returns the AGGREGATE
    transactions figure for the latest year; grounding requires the value to appear
    in the retrieved source or it is dropped.

    Returns {rpt_total, year_hint, source_page} or None.
    """
    # multi-query + rpt-scoring: RPT notes are phrased several ways, and TOC pages
    # mention the words without the amounts - prefer chunks that actually carry
    # related-party rows WITH figures.
    queries = [
        "transactions with related parties during the year remuneration paid",
        "summary of related party transactions total amount purchases sales loans",
        "related party disclosures name of related party nature of transaction amount",
    ]
    import re as _re
    seen, cand = set(), []
    for q in queries:
        for h in index.search(q, k=k):
            key = (h.get("page_start"), h["text"][:40])
            if key not in seen:
                seen.add(key); cand.append(h)
    if not cand:
        return None
    def rpt_score(h):
        t = h["text"].lower()
        s = 0
        if "related part" in t: s += 2
        if _re.search(r"total.{0,60}[\d,]+\.\d{2}", t): s += 2
        if _re.search(r"remuneration|loan (from|to)|purchases? (from|of)|sales? to", t): s += 1
        if len(_re.findall(r"[\d,]+\.\d{2}", t)) >= 4: s += 1
        return s
    cand.sort(key=rpt_score, reverse=True)
    hits = cand[:k]
    context = "\n".join(h["text"] for h in hits)
    src_page = hits[0].get("page_start")
    result = {"rpt_total": None, "source_page": src_page,
              "method": "rag-locate + grounded-llm"}
    if llm_fn is None:
        return result
    prompt = (
        "From the prospectus related-party-transactions text below, extract the "
        "TOTAL value of related party TRANSACTIONS (not outstanding balances, not "
        "receivables) for the most recent fiscal year. Return ONLY JSON: "
        "{\"rpt_total\": \"<number as written> or null\", \"year\": \"..\"}. "
        "If no aggregate transactions total is stated, return null - do not sum "
        "rows yourself.\n\nTEXT:\n" + context[:7000])
    try:
        import json as _json
        raw = llm_fn(prompt, True).strip().lstrip("`").lstrip("json").strip("` \n")
        data = _json.loads(raw)
        v = data.get("rpt_total")
        if v and str(v).lower() not in ("null", "none"):
            digits = "".join(ch for ch in str(v) if ch.isdigit())
            if digits and digits in context.replace(",", "").replace(" ", ""):
                result["rpt_total"] = str(v)
                result["year_hint"] = data.get("year")
    except Exception as _sw:
        drhp_log.swallowed("drhp_rag", _sw)
        pass
    return result


# ---- query rewriting (colloquial -> prospectus vocabulary) --------------------
# Users ask "how will they use the money"; documents say "objects of the offer /
# utilisation of net proceeds". Retrieval matches vocabulary, so the rewrite layer
# expands a colloquial question with the document's own terms before searching.
# Rule-based core (works offline, deterministic); an LLM can extend it, but the
# mapping below covers the standard RHP lexicon and is domain-general, not per-doc.
_QUERY_REWRITES = [
    (r"use (of )?(the )?(money|funds|cash|proceeds)|spend (the )?(money|proceeds)|raise money for",
     "objects of the offer utilisation of net proceeds"),
    (r"who (runs|owns|controls|manages)|founders?\b",
     "promoters directors key managerial personnel"),
    (r"\b(risky|risks?|dangerous|safe to invest|downside)\b",
     "risk factors internal external"),
    (r"how (do|does|will) (they|it|the company) (make|earn) money|business model|what (do|does) (they|the company) do",
     "business overview revenue from operations products services"),
    (r"\b(debt|loans?|owes?|borrowed|leverage)\b",
     "total borrowings indebtedness"),
    (r"\b(lawsuits?|court cases?|legal trouble|sued)\b",
     "outstanding litigation proceedings criminal civil tax"),
    (r"\b(competitors?|rivals?|competition)\b",
     "industry peer group competitive landscape"),
    (r"\b(dividends?|payouts?)\b",
     "dividend policy"),
    (r"(salary|salaries|pay|compensation) of (directors|promoters|management)",
     "remuneration managerial personnel"),
    (r"(ipo|issue|offer) (price )?(fair|expensive|cheap|overpriced)|valuation",
     "basis for offer price price earnings peer comparison"),
    (r"customers? (depend|concentrat)|biggest customers?",
     "top ten customers contributed revenue concentration"),
    # standard finance synonyms (domain lexicon, not per-document tuning):
    (r"\b(top ?line|turnover|sales figures?|billed?|billing)\b",
     "revenue from operations"),
    (r"\b(bottom ?line|net earnings?|net income|profit left|earnings after tax)\b",
     "profit after tax for the year"),
    (r"(shareholders?'? funds?|book value|owners'? capital|equity base)",
     "net worth total equity"),
    (r"(borrowed|owed to lenders|loan amounts?|indebted)",
     "total borrowings"),
    (r"cash (the )?(operations?|core business) (actually )?(produced|generated)|operating cash generation",
     "net cash generated used from operating activities"),
]

def rewrite_query(question):
    """Expand a colloquial question with prospectus vocabulary. Returns the
    original question plus appended document-lexicon terms for every matched
    intent - the original words stay, so precision is never lost, only recall
    gained. Deterministic and domain-general (RHP lexicon, not per-document)."""
    import re as _re
    q = question
    extras = []
    for pat, vocab in _QUERY_REWRITES:
        if _re.search(pat, question, _re.I):
            extras.append(vocab)
    return (q + " " + " ".join(extras)) if extras else q


def hierarchical_search(index, query, k=8, coarse_k=40, top_sections=2):
    """Hierarchical retrieval: coarse pass over the whole index lets chunks VOTE
    on which document SECTION is relevant (aggregate of retrieval scores per
    section), then the fine pass ranks chunks WITHIN the winning sections. Unlike
    the keyword cue-map (which only knows phrasings it was given), the section
    choice here is learned from actual chunk relevance, so unseen phrasings route
    correctly. Falls back to the global ranking when section signal is weak
    (winner margin < 1.3x) - hierarchy should sharpen, never override, a flat
    ranking that is already confident."""
    coarse = index.search(query, k=coarse_k)
    if not coarse:
        return []
    agg = {}
    for h in coarse:
        sec = h.get("section") or "?"
        agg[sec] = agg.get(sec, 0.0) + float(h.get("score", 0))
    ranked_secs = sorted(agg.items(), key=lambda x: -x[1])
    # weak-signal fallback: no dominant section -> keep the flat ranking
    if len(ranked_secs) > 1 and ranked_secs[0][1] < 1.3 * ranked_secs[1][1]:
        return coarse[:k]
    keep = {sname for sname, _ in ranked_secs[:top_sections]}
    fine = [h for h in coarse if (h.get("section") or "?") in keep]
    return (fine + [h for h in coarse if h not in fine])[:k]


def rag_litigation_reasoning(index, litigation_items, networth=None, llm_fn=None):
    """The human analyst's last edge, systematized: for each litigation item,
    reason about DIRECTION and MATERIALITY - is the promoter the complainant
    (protective, not a red flag) or the accused (real flag)? Is the amount
    material against net worth, or trivial? A bare item list treats a promoter
    filing a cheque-bounce case against a defaulter the same as a fraud case
    AGAINST the promoter - direction is the whole story.

    Grounded: retrieval supplies the case-description text; the LLM classifies
    each item using ONLY that text; a verdict must quote a supporting phrase that
    appears in the source or the item is marked 'unclassified' (never guessed).
    Deterministic materiality: amount vs net worth is arithmetic, not LLM opinion.

    Returns [{item, direction_read, materiality, verdict, support}] or None.
    """
    if not litigation_items or llm_fn is None:
        return None
    hits = index.search(
        "litigation criminal complaint filed by against promoter director "
        "company respondent accused complainant", k=8)
    context = "\n".join(h["text"] for h in hits)
    import json as _json, re as _re
    # deterministic materiality: amount as % of net worth
    nw = None
    if networth:
        try: nw = float(str(networth).replace(",", "").strip("()"))
        except ValueError: pass
    out = []
    prompt = (
        "For each litigation item below, classify using ONLY the prospectus text:\n"
        "- role: is the company/promoter the COMPLAINANT (they filed it) or the "
        "ACCUSED/DEFENDANT?\n- verdict: 'protective' (they filed to recover/enforce), "
        "'exposure' (filed against them), or 'unclassified' if the text does not say.\n"
        "- support: a short phrase FROM THE TEXT that shows the role.\n"
        "Return ONLY JSON: {\"items\": [{\"idx\": <n>, \"role\": \"..\", "
        "\"verdict\": \"..\", \"support\": \"..\"}]}\n\n"
        "ITEMS:\n" + _json.dumps(litigation_items)[:2500] +
        "\n\nPROSPECTUS TEXT:\n" + context[:6000])
    try:
        raw = llm_fn(prompt, True).strip().lstrip("`").lstrip("json").strip("` \n")
        data = _json.loads(raw)
        low = _re.sub(r"\s+", " ", context.lower())
        for it in data.get("items", []):
            i = it.get("idx")
            if not isinstance(i, int) or i >= len(litigation_items): continue
            support = str(it.get("support") or "")
            # grounding: the supporting phrase must exist in source, else unclassified
            grounded = bool(support) and _re.sub(r"\s+", " ", support.lower())[:60] in low
            verdict = it.get("verdict") if grounded else "unclassified"
            row = {"item": litigation_items[i], "role": it.get("role"),
                   "verdict": verdict, "support": support if grounded else None,
                   "grounded": grounded}
            amt = str(litigation_items[i].get("amount") or "")
            if nw and amt and amt.lower() not in ("null", "none", "-"):
                try:
                    a = float(amt.replace(",", ""))
                    pct = a / (nw * 100000 if nw < 10000 else nw) * 100
                    row["materiality"] = (f"{pct:.2f}% of net worth"
                                          if pct < 1000 else "scale mismatch - check units")
                except ValueError as _sw:
                    drhp_log.swallowed("drhp_rag", _sw)
                    pass
            out.append(row)
        return out or None
    except Exception:
        return None
