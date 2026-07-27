import drhp_log
import drhp_config
"""
drhp_digest.py - build the structured Digest from an ingested DRHP index.

Output structure (v2):
  summary      - positives / negatives / neutral, classified and cleaned
  fundamentals - key metrics (revenue, PAT, EPS, ROE, ROCE, OCF...) parsed from
                 the Basis-for-Issue-Price KPI table and financial sections,
                 up to 3 fiscal years each, with page citations
  red_flags    - deterministic rule scans with evidence quotes
  fields       - the deep-dive sections (risks, litigation, objects...)

Modes: extractive (default, cannot hallucinate) or Gemini-synthesized when
GEMINI_API_KEY is set, with a numeric groundedness check.
"""
import os, re, json, urllib.request

def _any_llm():
    try:
        import drhp_llm
        return bool(drhp_llm.available())
    except Exception:
        return bool(os.environ.get("GEMINI_API_KEY", "").strip())
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "").strip() or (
    "multi" if _any_llm() else "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

# ------------------------------------------------------------------ sentence QA
# generic boilerplate that appears in EVERY prospectus - zero information
_BOILER = re.compile(
    r"(left blank intentionally|sebi.{0,40}guarantee the accuracy|"
    r"no formal market for (our|the) equity|high degree of risk\.?$|"
    r"not in a position to quantify|value of investment.{0,40}decline|"
    r"please (refer|see)|beginning (on|from) page|on page (no|number)|"
    r"pages? \d+ of this (draft )?red herring|in terms of the (draft )?red herring|"
    r"whether physical or electronic|will be considered as the application|"
    r"companies act|icdr regulations?\b|securities contracts \(regulation\)|"
    r"prospective investors should|you should carefully consider|"
    r"incorporated under the laws of india|materiality policy|"
    r"the risk factors have been determined|risks and uncertainties summarized|"
    r"record date|grievances? relating|registrar (to|with)|"
    r"commission (and|or) brokerage|face value of the equity|bid lot|"
    r"emerge platform|proposed to be listed|lodr|regulation \d+|"
    r"chapter (titled|ix)|section titled|as amended from time|"
    r"forward.looking statements?|anticipated in these|"
    r"complaints?|redress|other confirmations|net tangible assets|"
    r"should have a track record of at least|eligibility)", re.I)

def _digit_ratio(s):
    d = sum(c.isdigit() for c in s)
    return d / max(1, len(s))

def _good_sentence(s, max_digit=0.22):
    """Reject fragments, table dumps, cross-references, legal boilerplate."""
    s = s.strip()
    if not (60 <= len(s) <= 420):           return False
    if not (s[0].isupper() or s[0].isdigit()): return False   # mid-sentence fragment
    if _digit_ratio(s) > max_digit:          return False     # table dump
    if _BOILER.search(s):                    return False
    if "\u25cf" in s or "[\u2022]" in s:     return False     # placeholder-price sentences
    if re.search(r"\(\d\)\s*\(\d\)", s):  return False     # footnote chains (1) (1)
    if re.search(r"\.{4,}", s):              return False     # table-of-contents dot leaders
    if re.search(r"[:;]\s*[A-Z]\.?$", s):    return False     # trails into a table header
    if re.search(r"\bSr\.?$", s):             return False     # trails into "Sr. No." column
    if re.search(r"(set out in the following|in the manner set out|table below)[:\s]*\(?", s, re.I):
        return False                                             # table-intro sentences
    if re.search(r"\s\d{1,2}\.$", s):        return False     # trails into an enumeration
    if re.match(r"Note\s*[\u2013\u2014-]", s): return False  # financial-note index lines
    alpha = [c for c in s if c.isalpha()]
    if alpha and sum(c.isupper() for c in alpha) / len(alpha) > 0.55:
        return False                                            # ALL-CAPS heading dumps
    return True

_ABBR = re.compile(r"\b(Mr|Mrs|Ms|Dr|Rs|No|Nos|M/s|Ltd|Pvt|St|vs|Co|Inc|vol|approx|i\.e|e\.g)\.\s", re.I)
def _split_sentences(text):
    """Split on sentence ends, but never after common abbreviations (Mr., Rs., ...)."""
    protected = _ABBR.sub(lambda m: m.group(0).replace(". ", ".\x00"), text)
    return [p.replace("\x00", " ") for p in re.split(r"(?<=[.;])\s+", protected)]

def _top_sentences(passages, limit=6, require=None, reject=None,
                   per_chunk=1, max_digit=0.22):
    """Extractive: best clean sentences per chunk, deduped, optionally filtered."""
    seen, out = set(), []
    for p in passages:
        took = 0
        for s in _split_sentences(p["text"]):
            s = s.strip()
            # a section heading often glues onto the first sentence of a chunk
            s = re.sub(r"^[A-Z][A-Z\s\d&,\-/():.]{6,}\s(?=[A-Z][a-z])", "", s).strip()
            # heading glue (any case style): a mid-sentence capitalized sentence-starter
            # with no punctuation before it means a period-less heading was prepended
            m = re.match(r"^[^.;:!?]{25,90}?\s(?=(?:We|Our|The Company|This|It)\s+[a-z])", s)
            if m and not re.search(r"\b(and|or|that|which|because)\s*$", m.group(0)):
                s = s[m.end():].strip()
            if not _good_sentence(s, max_digit):         continue
            if require and not re.search(require, s, re.I): continue
            if reject  and re.search(reject,  s, re.I):     continue
            key = s[:60].lower()
            if key in seen:                              continue
            seen.add(key)
            out.append({"text": s if len(s) < 420 else s[:417] + "...",
                        "page": p["page_start"], "section": p["section"]})
            took += 1
            if took >= per_chunk: break
        if len(out) >= limit: break
    return out[:limit]

def _all_chunks(index):
    c = index.chunks
    return list(c.values()) if isinstance(c, dict) else list(c)

# ------------------------------------------------------------- fundamentals
# label regex -> pretty name. Values: up to 3 fiscal years, newest first,
# tolerant of footnote markers like "ROCE (%)(6)" and negatives "(1,109.55)".
_NUM = r"\(?-?\d[\d,]*\.?\d*\)?%?"
FUNDAMENTAL_PATTERNS = [
    ("revenue",  r"Revenue\s+from\s+operations?",              "Revenue from operations"),
    ("ebitda_margin", r"EBITDA\s+Margin",                      "EBITDA margin"),
    ("pat",      r"(?:\bPAT\b|(?:Restated\s+)?[Pp]rofit\s+(?:after\s+tax|for\s+the\s+(?:year|period))|Net\s+[Pp]rofit)", "Profit after tax (PAT)"),
    ("eps",      r"(?:\bEPS\b|(?:Basic\s+)?Earnings\s+per\s+share)", "EPS"),
    ("roe",      r"(?:ROE\s*/?\s*RoNW|\bRoNW\b|Return\s+on\s+(?:Net\s*Worth|Equity)|\bROE\b)", "ROE / RoNW"),
    ("roce",     r"\bROCE\b",                                  "ROCE"),
    ("ocf",      r"(?:Operating\s+Cash\s*flows?|(?:Net\s+)?[Cc]ash\s+(?:flows?\s+from|generated\s+from|used\s+in)\s+operating\s+activities)", "Operating cash flow"),
    ("cratio",   r"Current\s+Ratio",                           "Current ratio"),
    ("borrowings", r"(?:Total\s+borrowings|Total\s+debt(?!\s*/?\s*equity)|Borrowings(?!\s*/?\s*equity))", "Total borrowings"),
    ("pe",       r"(?:P\s*/\s*E\s*(?:ratio)?|Price\s*/?\s*Earnings)", "P/E"),
]

def _clean_values(raw_vals):
    """Drop footnote markers like (9), bare years, and date fragments."""
    vals = []
    for v in raw_vals:
        v = v.strip().rstrip(",")
        if not v or not any(ch.isdigit() for ch in v):        continue
        if re.fullmatch(r"\(?(19|20)\d{2}\)?", v):            continue   # a year
        vals.append(v)
    # leading tiny int with no decimal/% = footnote marker (e.g. PAT(9) -> "(9)")
    while vals and re.fullmatch(r"\(?\d{1,2}\)?", vals[0]) and len(vals) > 1:
        vals.pop(0)
    # a match is only real if at least one value looks like a metric
    if not any(re.search(r"[\d],|\.\d|%|\d{3,}", v) for v in vals):
        return []
    return vals[:3]

# value-type expectations per metric: magnitudes must not be percentages
# (stops "Revenue grew 16.09%" from being read as revenue), ratios must not
# carry % either, and percentage metrics must actually be percentages.
_MAG_KEYS = {"revenue", "pat", "ocf", "debt"}      # rupee magnitudes
_PCT_KEYS = {"ebitda_margin", "roe", "roce"}            # percentages
_RATIO_KEYS = {"cratio", "pe", "eps"}              # plain numbers
_MAGNITUDE_KEYS = {"revenue","pat","networth","ebitda","borrowings","total_income",
                   "total_assets","ocf","finance_cost","inventory","receivables"}

def _typed_ok(key, vals):
    if key in _MAG_KEYS:
        vals = [v for v in vals if not v.endswith("%")]
        # a rupee magnitude should have a comma or 4+ digits somewhere
        return vals if any(re.search(r",|\d{4,}", v) for v in vals) else []
    if key in _PCT_KEYS:
        vals = [v for v in vals if v.endswith("%")]
        return vals
    if key in _RATIO_KEYS:
        return [v for v in vals if not v.endswith("%")]
    return vals

def extract_fundamentals(index):
    """Parse KPI metrics. Collect ALL candidate matches per metric, validate the
    value TYPE (magnitude vs percentage vs ratio), and keep the best-scoring
    match: more fiscal years beats fewer, basis_of_price beats other sections."""
    chunks = [c for c in _all_chunks(index)
              if c["section"] in ("basis_of_price", "financials", "mdna")]
    out = []
    for key, pat, label in FUNDAMENTAL_PATTERNS:
        rx = re.compile(pat + r"[^0-9\-(]{0,24}((?:" + _NUM + r"[\s]+){0,3}" + _NUM + r")", re.I)
        best, best_score = None, -1
        for c in chunks:
            for m in rx.finditer(c["text"]):
                gap = c["text"][max(0, m.start()):m.start(1)]
                if re.search(r"(as at|growth|grew|increase|decrease|january|february|march|april|may|june|"
                             r"july|august|september|october|november|december)", gap, re.I):
                    continue                     # date rows and growth rows are not the KPI
                # A risk-factor table listing each SUBSIDIARY's revenue/net worth uses
                # the same row labels as the consolidated P&L. On one document that
                # table sat ~230 pages before the real statement and won the tie, so
                # the company's revenue was reported as a subsidiary's (2,539 vs
                # 36,529) and its net worth likewise - which then produced an
                # impossible 5.2x debt/equity. The giveaway is the "Subsidiary /
                # Particulars" header, or a "Private Limited" name used as the row
                # label. Consolidated statements say "and its Subsidiaries" in prose
                # and never carry that header, so this does not catch them.
                _pre = c["text"][max(0, m.start() - 220):m.start()]
                if re.search(r"Subsidiar(?:y|ies)\s+Particulars", c["text"], re.I) or \
                   re.search(r"(Private Limited|Pvt\.?\s*Ltd)\s*$", _pre.strip()[-80:], re.I) or \
                   re.search(r"(Private Limited|Pvt\.?\s*Ltd)[^.]{0,40}$", _pre[-120:], re.I):
                    continue
                if key == "pe":
                    # company P/E is [placeholder] pre-pricing; a concrete P/E in the
                    # basis-for-price section belongs to a PEER, never the company
                    _win = c["text"][max(0, m.start()-300):m.end()+300]
                    if "\u25cf" in _win or c["section"] == "basis_of_price" or \
                       re.search(r"peer|industry|listed compan", _win, re.I):
                        continue
                vals = _typed_ok(key, _clean_values(re.findall(_NUM, m.group(1))))
                if not vals: continue
                if key in _MAGNITUDE_KEYS:
                    _mv = []
                    for _v in vals:
                        try: _mv.append(abs(float(str(_v).replace(",",""))))
                        except: pass
                    # a magnitude metric with all values < 50 is a mislabeled ratio row
                    if _mv and max(_mv) < 50: continue
                # Ties used to be broken by document order, which is arbitrary -
                # the first chunk containing the label won, wherever it lived.
                # Prefer the audited statements, which is where these figures are
                # authoritative, over a mention elsewhere in the prospectus.
                _stmt = bool(re.search(r"Restated (?:Consolidated|Standalone) Statement of "
                                       r"(?:Profit and Loss|Assets and Liabilities|Cash Flow)|"
                                       r"Balance Sheet|Statement of Profit and Loss",
                                       c["text"][:400], re.I))
                score = (len(vals) * 2
                         + (3 if _stmt else 0)
                         + (1 if c["section"] == "basis_of_price" else 0))
                if score > best_score:
                    best, best_score = {"key": key, "label": label, "values": vals,
                                        "page": c["page_start"], "section": c["section"]}, score
        if best: out.append(best)
    return out

# ------------------------------------------------------------- summary (pos/neg/neu)
_GENERIC_RISK = (r"(trading price|value of (the )?investment|equity shares involves|"
                 r"loss of all or part|material adverse effect[s]?( on)?$)")

def build_summary(index, fundamentals, red_flags):
    """Classified positives / negatives / neutral, cleaned of boilerplate."""
    neg_p = index.search("we depend rely limited customers suppliers concentration "
                         "competition regulatory delay adverse pledge negative cash",
                         k=14, section="risk_factors")
    negatives = _top_sentences(
        neg_p, limit=5, per_chunk=2, reject=_GENERIC_RISK,
        require=_RISK_SHAPE + r".*(depend|rely|adverse|could|may|loss|failure|inabilit|delay|concentrat|subject to|competit|pledg)")

    pos_p = index.search("our competitive strengths experienced promoters research "
                         "development long standing customer relationships track record "
                         "accreditation capacity", k=10, section="business")
    cand = _top_sentences(
        pos_p, limit=12, per_chunk=2,
        require=r"\b(we|our)\b.{0,200}(strength|believe|long[- ]standing|experienc|dedicated|"
                r"accredit|track record|strategic|relationship|capacit|expertise|decades|"
                r"certif|patent|award|market share|installed)",
        reject=r"(experienced management team consisting|led by an experienced|"
               r"qualified and experienced (management|team)$)")
    def _specificity(t):
        sc = 0
        if re.search(r"\d", t): sc += 3                      # numbers = substance
        if re.search(r"(decade|years of|ISO|MTPA|certif|accredit|patent|award)", t, re.I): sc += 2
        if re.search(r"[A-Z][a-z]+ [A-Z][a-z]+", t[10:]): sc += 1   # named entities
        return sc
    positives = sorted(cand, key=lambda it: -_specificity(it["text"]))[:5]

    neutral = []
    obj = index.search("fresh issue offer for sale net proceeds utilised repayment "
                       "capital expenditure", k=6, section="objects")
    neutral += _top_sentences(obj, limit=2,
                              require=r"(fresh issue|offer for sale|net proceeds|utilis)")
    div = index.search("dividend declared paid last three fiscals", k=3, section="dividend")
    neutral += _top_sentences(div, limit=1,
                              require=r"dividend.*(declared|paid|policy)|not declared",
                              reject=r"(record date|ceases to be|entitled)")

    triggered = [f["label"] for f in red_flags if f["triggered"]]
    return {"positives": positives, "negatives": negatives, "neutral": neutral,
            "headline": {
                "flags_triggered": len(triggered), "flags_total": len(red_flags),
                "fundamentals_found": len(fundamentals),
                "top_flags": triggered[:4],
            }}

def _clean_llm_text(t):
    """Remove stray markdown emphasis/handles that leak from some providers."""
    t = re.sub(r"\*{1,}", "", t or "")
    t = re.sub(r"^#+\s*", "", t, flags=re.M)
    return t.strip()


def _verify_llm(text, facts_blob):
    """Gate for ALL generated prose. Strips markdown, then rejects the whole
    output if any number in it is absent from the facts (fiscal years exempt).
    Returns (clean_text, unverified_list). Empty text => do not display."""
    if not text: return "", ["empty"]
    text = _clean_llm_text(text)
    off = _grounded_numbers(text, facts_blob)
    return (("", off) if off else (text, []))


def _thesis(index, digest):
    """SEPARATE bull / bear / one-line-verdict compression. Grounded: built only
    from the computed facts + detected contradictions; verified before display."""
    facts = _facts_blob(digest)
    try:
        import drhp_contradictions
        cons = drhp_contradictions.detect(digest, index=index)
    except Exception:
        cons = []
    con_txt = "\n".join(f"- {c['title']}: {c['a']}; {c['b']}" for c in cons)
    prompt = ("You are an equity analyst writing the CLOSING box of an IPO note. "
              "Using ONLY the facts and tensions below, output exactly three labelled lines:\n"
              "BULL: <one sentence, the strongest fair case>\n"
              "BEAR: <one sentence, the strongest fair risk>\n"
              "VERDICT: <one sentence naming the single question that decides it - not advice>\n"
              "Every number must appear in the facts. No preamble. No markdown. "
              "READABILITY RULE: at most THREE figures per line - pick the ones that "
              "carry the argument, do not inventory every metric; a closing box is a "
              "judgement, not a data dump. Every percentage keeps the exact base the "
              "facts state.\n\n"
              f"FACTS:\n{facts[:8000]}\n\nTENSIONS:\n{con_txt[:2000]}")
    try:
        raw = _gemini(prompt)
    except Exception as e:
        return {"error": str(e)[:160]}
    clean, off = _verify_llm(raw, facts + "\n" + con_txt)
    if not clean:
        return {"error": f"unverified numbers: {', '.join(off[:5])}"}
    parsed = {}
    for line in clean.splitlines():
        m = re.match(r"\s*(BULL|BEAR|VERDICT)\s*[:\-]\s*(.+)", line, re.I)
        if m: parsed[m.group(1).lower()] = m.group(2).strip()
    return parsed if parsed else {"error": "no parseable thesis"}


def _weave_contradictions(index, digest):
    """Fold the ALREADY-DETECTED tensions into one prose paragraph. The facts
    are deterministic; the LLM only supplies connective phrasing, and the whole
    paragraph is number-verified before display."""
    try:
        import drhp_contradictions
        cons = drhp_contradictions.detect(digest, index=index)
    except Exception:
        cons = []
    if len(cons) < 2: return None
    facts = "\n".join(f"- {c['title']}: {c['a']}; {c['b']} (page-cited already)" for c in cons)
    prompt = ("Combine these already-verified tensions from an IPO prospectus into ONE tight "
              "paragraph (4-6 sentences) a fund manager would read. Use ONLY these facts; keep "
              "every number; do not invent causes; no markdown, no preamble.\n\n" + facts)
    try:
        raw = _gemini(prompt)
    except Exception:
        return None
    clean, off = _verify_llm(raw, facts)
    return clean or None


def _polish_summary(index, digest):
    """Rewrite the three summary columns with the SAME grounded LLM used for
    deep-dive: candidates in, crisp cited bullets out, numbers verified.
    Falls back silently to the extractive lists on any error."""
    S = digest["summary"]
    jobs = [
        ("positives", "company-specific competitive strengths. Prefer assets, capacity, "
         "market position, certifications, client base. AT MOST ONE bullet about "
         "management experience. NEVER corporate history, name changes, or incorporation dates",
         index.search("competitive strengths capacity market position certifications "
                      "client relationships installed", k=12, section="business")),
        ("negatives", "the most material company-specific risks. Prefer quantified ones "
         "(concentration %, dependence, litigation). Skip generic legal boilerplate",
         index.search("we depend rely limited customers suppliers concentration "
                      "competition litigation adverse pledge negative cash", k=12,
                      section="risk_factors")),
        ("neutral", "factual offer mechanics: fresh issue vs offer-for-sale split, "
         "use of proceeds amounts, dividend history. Facts only, no judgement",
         index.search("fresh issue offer for sale net proceeds utilised dividend "
                      "declared", k=10, section="objects")),
    ]
    try:
        facts = _facts_blob(digest)[:9000]
        raw = _gemini("Explain this IPO company to a first-time investor in 5 plain sentences. "
                      "Use ONLY these extracted facts; no advice; mention what the company does, "
                      "how big it is, how the money flows in this offer, and the one biggest "
                      "thing to be careful about.\n" + facts)
        _wide = facts + " " + json.dumps([f["values"] for f in digest.get("fundamentals", [])])
        clean, off = _verify_llm(raw, _wide)
        if clean:
            digest["plain_english"] = clean
    except Exception as _sw:
        drhp_log.swallowed("drhp_digest", _sw)
        pass
    for key, ask, passages in jobs:
        if not passages: continue
        items, note = _synthesize(f"{ask}. Write 3-5 bullets.", passages)
        if items:
            S[key] = items[:5]
            if note: S[f"{key}_note"] = note

# ------------------------------------------------------------- deep-dive fields
# (key, label, query, section, k, require_regex, reject_regex)
# require = a sentence must look like ACTUAL content for this topic, not meta/procedure
_RISK_SHAPE = r"^(We|Our|If|Any|There|The loss|Failure|Inability|Delay|A significant|Changes)\b"
# order follows how a person reads a company: what it is -> the deal -> who runs it
# -> what can go wrong -> legal -> money-with-insiders. Each carries a plain intro.
FIELD_INTRO = {
 "business": "What the company actually does, in its own words.",
 "objects": "Where the IPO money goes (or, for an OFS, who is cashing out).",
 "promoters": "Who owns and runs it, and their track record.",
 "risks": "The company's own stated risks - what could go wrong.",
 "litigation": "Outstanding legal matters, by direction and amount.",
 "related_party": "Money flowing between the company and promoter-linked entities.",
}
_FIELD_ORDER = ["business", "objects", "promoters", "risks", "litigation", "related_party"]

DIGEST_FIELDS = [
    ("risks",        "Key risk factors",
     "we depend rely customers suppliers concentration competition regulatory approvals delay adverse",
     "risk_factors", 12,
     _RISK_SHAPE + r".*(depend|rely|adverse|could|may|loss|failure|inabilit|delay|concentrat|subject to|competit|pledg|litigat)",
     None),
    ("litigation",   "Litigation and legal proceedings",
     "criminal civil tax proceedings pending against our company promoters directors aggregate amount involved lakhs million",
     "litigation", 12,
     r"(proceeding|litigation|criminal|civil|tax|show cause|dispute)s?\b.*\d|"
     r"there (are|is) no.{0,40}(criminal|litigation|proceeding)",
     r"(grievance|registrar|materiality|audit committee|half yearly|d&b|industry report|policy for identification|\d{8,})"),
    ("objects",      "Where the money goes (objects of the issue)",
     "net proceeds utilised repayment prepayment borrowings capital expenditure working capital general corporate",
     "objects", 10,
     r"(net proceeds|fresh issue|offer for sale|utilis|repayment|working capital|capital expenditure|general corporate)",
     r"(unsubscribed|clauses \(|letter of credit|bank guarantee|dependent on a number of factors|^No\.\b|Proceeds\*)"),
    ("related_party","Related party transactions",
     "related party transactions aggregate amount purchase sale rent loan remuneration promoter group entities",
     "related_party", 8,
     r"related party transactions?.{0,200}(rs\.?|\u20b9|lakh|million|crore|aggregate|amounted|%)|"
     r"transactions? (entered into )?with (our |certain )?(promoter|related|group)",
     r"(nature of transactions|note\b|\d{2}-\d{2}-\d{4}|shareholding|equity shares held|eliminated while preparing)"),
    ("promoters",    "Promoters",
     "our promoter is Mr years of experience holds degree director managing founded",
     None, 10,
     r"(Mr|Mrs|Ms|Dr)\.?\s+[A-Z][a-z]+.{0,120}(experience|holds|director|promoter|found|graduate|degree|qualif)|"
     r"^Our Promoters?\b.{0,80}(is|are)\b|"
     r"^[A-Z][a-z]+ [A-Z][a-z]+.{0,60}\b(is|are|has been)\b.{0,60}(Chairman|Managing Director|Whole.?Time|Executive Director|Promoter|Director)",
     r"(shareholding pattern|regulation|shareholders on|contribution|table below|confirmation|nil\b|body corporate|no relationship between|except as described)"),
    ("business",     "Business in brief",
     "we are engaged in the business manufacture products services customers installed capacity facilities",
     "business", 8,
     r"^(We|Our)\b.*(engaged|manufactur|provid|serv|capacit|facilit|product|customer|operat)",
     None),
]

_FLAG_SEV = {"criminal":"high","neg_cashflow":"high","auditor":"high","pledge":"medium",
             "ofs_heavy":"medium","losses":"medium","related_dep":"medium","contingent":"info"}
FLAG_WHY = {
 "criminal": "cases against promoters/directors can hit both reputation and, in bad outcomes, control",
 "neg_cashflow": "profits you cannot collect are not profits; check where the cash went",
 "auditor": "the auditor is paid to be boring - when they highlight something, read it",
 "pledge": "pledged shares can be sold by lenders in a downturn, exactly when the stock is weakest",
 "ofs_heavy": "the company gets none of this money; existing owners are cashing out",
 "losses": "a loss history means the business model is not yet proven at current scale",
 "related_dep": "revenue or costs routed through promoter entities can move value out of your company",
 "contingent": "off-balance-sheet obligations that can become real; compare their size to net worth",
 "ocf_neg_trend": "multi-year negative operating cash flow while reporting profit is the classic pre-listing red flag",
 "receivable_days": "sales are being booked faster than cash is collected - working capital risk",
 "pat_declining": "shrinking profits into an IPO means you are paying for the past, not the future",
 "revenue_declining": "topline shrinking - understand whether it is one-off or structural before paying growth multiples",
 "high_leverage": "debt above 1.5x net worth leaves little cushion in a bad year",
 "ebitda_declining": "the operating engine is weakening even if the bottom line looks fine",
 "margin_compression": "pricing power or cost control is slipping",
 "eps_declining": "per-share earnings falling - dilution or profit decline, either way you get less",
}
RED_FLAG_RULES = [
    ("criminal",  "Criminal proceedings involving promoters/directors",
     "criminal proceedings pending against promoters directors", "litigation",
     r"(?<!no )(?<!other )criminal (proceeding|case|complaint)s?\b(?![^.\n]{0,80}\b(nil|none|no criminal|no pending|not pending))"),
    ("neg_cashflow", "Negative cash flow from operations reported",
     "negative cash flow from operating activities", None,
     r"(negative cash flows? (from|used in) operat|operating cash flow.{0,40}\(\d)"),
    ("losses",    "Company has reported losses",
     "we have incurred losses net loss during fiscal", None,
     r"(incurred|reported) (net )?loss(es)?\b"),
    ("pledge",    "Promoter shares pledged",
     "pledge of equity shares by promoters", None,
     r"(promoter|equity share)s?[^.\n]{0,60}\b(is|are|has been|have been|stand)\s+pledged|pledge of \d{1,2}(\.\d+)?\s*%|pledged \d{1,2}(\.\d+)?\s*%"),
    ("ofs_heavy", "Offer is fully/heavily Offer-for-Sale",
     "offer for sale selling shareholders will not receive any proceeds", "objects",
     r"(will not receive any proceeds|entirely.{0,30}offer for sale)"),
    ("related_dep","Material dependence on related parties",
     "significant portion of revenue purchases from related parties promoter group", None,
     r"(significant|substantial|material).{0,90}(related part|promoter group)"),
    ("auditor",   "Auditor qualifications / emphasis of matter",
     "auditor qualified opinion emphasis of matter", None,
     r"(qualified opinion|emphasis of matter|adverse remark)"),
    ("contingent","Large contingent liabilities",
     "contingent liabilities not provided for aggregate", None,
     r"contingent liabilit(y|ies).{0,80}\d"),
]

# ------------------------------------------------------------- LLM (optional)
def _gemini(prompt):
    # historical name; now routes through the multi-provider layer (drhp_llm)
    import drhp_llm
    return drhp_llm.complete(prompt)

def _grounded_numbers(text, evidence):
    ev = re.sub(r"[,\u20b9]", "", evidence.lower())
    out = []
    for m in re.finditer(r"\d[\d,]*\.?\d*", text):
        n = m.group(0)
        bare = re.sub(r",", "", n)
        if re.fullmatch(r"(19|20)\d{2}", bare):        # fiscal years = context
            continue
        tail = text[m.end():m.end()+1]
        # descriptive small integers ("100% OFS", "top 5", "3 units", "6 plants",
        # "over 1,000 SKUs") are prose, not financial claims to verify
        if "." not in bare and float(bare) <= 1000 and (tail == "%" or float(bare) <= 100):
            continue
        if bare not in ev:
            out.append(n)
    return out

def _synthesize(label, passages):
    evidence = "\n\n".join(
        f"[p.{p['page_start']} {p['section']}] {p['text']}" for p in passages)
    prompt = ("You are writing one section of a professional IPO note from prospectus "
              f"excerpts.\nTopic: {label}.\n"
              "STRICT FORMAT: output ONLY bullet lines starting with '- '. No preamble, "
              "no heading, no intro sentence, no 'Here are'. YEAR ORDER: value series in "
              "prospectus tables run LATEST-FIRST (FY26 | FY25 | FY24); attribute each figure "
              "to its correct fiscal year and never call an older figure 'latest'. "
              "3-5 bullets, each a complete "
              "sentence a fund manager would read, each ending with its page citation "
              "like (p.47). Numbers only from the excerpts. Every percentage MUST keep the exact base the source states ('49% of raw materials' must never become '49% of revenue'). If not covered, reply "
              "exactly: NOT COVERED.\n\nEXCERPTS:\n" + evidence[:14000])
    try:
        out = _gemini(prompt).strip()
    except Exception as e:
        return None, f"llm error: {str(e)[:220]}"
    if out.upper().startswith("NOT COVERED"):
        return [], None
    bullets = []
    for line in out.splitlines():
        line = re.sub(r"\*{2,}", "", line).strip().lstrip("-*\u2022 ").strip()
        if re.match(r"^(here (are|is)|the following|below (are|is)|these are)\b", line, re.I):
            continue                                  # LLM preamble, not content
        if line.endswith(":") and len(line) < 80:
            continue                                  # heading line
        if len(line) > 25:
            m = re.search(r"\(p\.?\s*(\d+)\)", line)
            page = int(m.group(1)) if m else passages[0]["page_start"]
            line = re.sub(r"\s*\(p\.?\s*\d+(?:\s*,\s*p\.?\s*\d+)*\)\s*\.?\s*$", "", line).strip()
            bullets.append({"text": line, "page": page,
                            "section": passages[0]["section"]})
    _nil = r"there (?:are|is) no (?:outstanding )?litigat|no (?:suits|proceedings|actions)[^.]{0,40}against"
    _case = r"case\s*(?:no\.?|number)|\b[A-Z]{2,4}[-/ ]?\d+[-/ ]?(?:of\s*)?20\d\d|proceeding (?:is )?pending|demand order"
    if any(re.search(_case, b["text"], re.I) for b in bullets):
        _before = len(bullets)
        bullets = [b for b in bullets if not re.search(_nil, b["text"], re.I)]
        # (dropping the nil boilerplate only when concrete cases are also listed -
        # a genuinely litigation-free document keeps its nil disclosure)
    off = _grounded_numbers(out, evidence)
    note = (f"groundedness: {len(off)} unverified number(s): {', '.join(off[:5])}") if off else None
    return bullets, note

# ------------------------------------------------------------- main entry
def _fnum(v):
    v = str(v).strip()
    neg = v.startswith("(")
    v = v.strip("()%").replace(",", "")
    try: return -float(v) if neg else float(v)
    except ValueError: return None

def computed_rows(fund_list):
    """Analyst ratios the prospectus never states directly - derived from the
    extracted statements, per year, labeled (computed)."""
    F = {f["key"]: f for f in fund_list}
    def series(k):
        out = []
        for v in (F[k]["values"] if k in F else []):
            out.append(_fnum(v))
        return out
    def pgs(*ks):
        return next((F[k]["page"] for k in ks if k in F), None)
    rows = []
    def emit(key, label, vals, page, pct=False, x=False):
        vv = [(f"{v:.0f}" if not pct and not x else f"{v:.1f}{'%' if pct else 'x'}")
              for v in vals if v is not None]
        if vv:
            rows.append({"key": key, "label": label + " (computed)", "values": vv,
                         "page": page, "section": "derived"})
    rec, inv, pay, rv = series("receivables"), series("inventory"), series("payables"), series("revenue")
    n = min(len(rec), len(inv), len(pay), len(rv))
    if n >= 2 and all(rv[:n]):
        emit("wc_days", "Working-capital days",
             [ (rec[i] + inv[i] - pay[i]) / rv[i] * 365 for i in range(n) ], pgs("receivables"))
    bor, cash, eb = series("borrowings"), series("cash"), series("ebitda")
    n = min(len(bor), len(cash), len(eb))
    if n >= 1 and all(e and e > 0 for e in eb[:n]):
        emit("nd_ebitda", "Net debt / EBITDA",
             [ (bor[i] - cash[i]) / eb[i] for i in range(n) ], pgs("borrowings"), x=True)
    ppe = series("ppe")
    n = min(len(rv), len(ppe))
    if n >= 1 and all(p and p > 0 for p in ppe[:n]):
        emit("fat", "Fixed-asset turnover", [ rv[i] / ppe[i] for i in range(n) ], pgs("ppe"), x=True)
    emp = series("employee_cost")
    n = min(len(rv), len(emp))
    if n >= 1 and all(rv[:n]):
        emit("workforce_pct", "Workforce cost / revenue",
             [ emp[i] / rv[i] * 100 for i in range(n) ], pgs("employee_cost"), pct=True)
    # iteration 6: high-value solvency/liquidity ratios an analyst expects, all
    # computed deterministically from already-extracted magnitudes (no new rules,
    # just arithmetic on retrieved numbers). Each only emits when its inputs exist.
    ca, cl = series("current_assets"), series("current_liabilities")
    n = min(len(ca), len(cl))
    if n >= 1 and all(c and c > 0 for c in cl[:n]):
        emit("current_ratio", "Current ratio",
             [ ca[i] / cl[i] for i in range(n) ], pgs("current_assets"), x=True)
    eb2, fin = series("ebitda"), series("finance_cost")
    n = min(len(eb2), len(fin))
    if n >= 1 and all(f and f > 0 for f in fin[:n]):
        emit("interest_cov", "Interest coverage (EBITDA/finance cost)",
             [ eb2[i] / fin[i] for i in range(n) ], pgs("ebitda"), x=True)
    nw2 = series("networth")
    n = min(len(bor), len(nw2))
    if n >= 1 and all(w and w > 0 for w in nw2[:n]):
        emit("debt_equity", "Debt / equity",
             [ bor[i] / nw2[i] for i in range(n) ], pgs("borrowings"), x=True)
    return rows


def extract_litigation_summary(index):
    """Parse the litigation SUMMARY TABLE into structured rows by direction and
    party, with the aggregate amount. Universal across the standard RHP table
    layout (Category | Criminal | Tax | Statutory | Civil | Aggregate ₹). Pure
    relay, page-cited - this is what lets the tool itemize like an analyst."""
    raw = getattr(index, "raw_pages", None) or []
    def _cell(x):
        x = x.strip()
        return 0 if re.fullmatch(r"nil|n\.?a\.?", x, re.I) else x
    for p, t in raw:
        if not t: continue
        flat = re.sub(r"\s+", " ", t)
        # must be the summary table: has the column header + by/against rows
        if not (re.search(r"category\s+criminal\s+tax", flat, re.I) or
                (re.search(r"\bcriminal\b.*\btax\b.*\b(statutory|civil)\b", flat, re.I)
                 and re.search(r"(by|against) (our|the) (compan|promoter|director)", flat, re.I))):
            continue
        rows = []
        # 4 count columns (criminal/tax/statutory/civil) then aggregate-amount.
        # some tables insert a 5th "material civil count" before the ₹ amount.
        _cellpat = (r"(nil|n\.?a\.?|\d+)\s+(nil|n\.?a\.?|\d+)\s+(nil|n\.?a\.?|\d+)\s+"
                    r"(nil|n\.?a\.?|\d+)(?:\s+(?:nil|n\.?a\.?|\d+))?\s+(nil|[\d,]+\.\d{1,2}|nil)")
        # Layout A: "By our/Against our <Party> <5 cells>"
        for mrow in re.finditer(
            r"(by (?:our|the)|against (?:our|the))\s+(compan\w+|promoter\w?|director\w?|subsidiar\w+)\s+"
            + _cellpat, flat, re.I):
            direction = "by" if mrow.group(1).lower().startswith("by") else "against"
            party = re.sub(r"s$", "", mrow.group(2).lower())
            crim, tax, stat, civil = (_cell(mrow.group(i)) for i in (3,4,5,6))
            amt = 0 if re.fullmatch(r"nil", mrow.group(7), re.I) else mrow.group(7)
            if any(v not in (0, "0") for v in (crim, tax, stat, civil)) or amt not in (0, "0"):
                rows.append({"direction": direction, "party": party, "criminal": crim,
                             "tax": tax, "statutory": stat, "civil": civil,
                             "amount": amt, "page": p})
        # Layout B (party-first): "<Party> By our/Against <5 cells>"
        if not rows:
            for mrow in re.finditer(
                r"(compan\w+|promoter\w?|director\w?|subsidiar\w+)\s+"
                r"(by (?:our|the)|against(?:\s+(?:our|the))?)\s+" + _cellpat, flat, re.I):
                party = re.sub(r"s$", "", mrow.group(1).lower())
                direction = "by" if mrow.group(2).lower().startswith("by") else "against"
                crim, tax, stat, civil = (_cell(mrow.group(i)) for i in (3,4,5,6))
                amt = 0 if re.fullmatch(r"nil", mrow.group(7), re.I) else mrow.group(7)
                if any(v not in (0, "0") for v in (crim, tax, stat, civil)) or amt not in (0, "0"):
                    rows.append({"direction": direction, "party": party, "criminal": crim,
                                 "tax": tax, "statutory": stat, "civil": civil,
                                 "amount": amt, "page": p})
        if rows:
            return rows[:10]
    return []


def numeric_flags(fund_list):
    """Professional-grade thresholded flags computed from the extracted
    financials themselves. Deterministic, each cites the source page."""
    F = {f["key"]: f for f in fund_list}
    def vals(k):
        return [x for x in (map(_fnum, F[k]["values"]) if k in F else []) if x is not None]
    def pg(k): return F[k]["page"] if k in F else None
    out = []
    def add(fid, label, sev, cond, key, detail):
        if key not in F: return
        out.append({"id": fid, "label": label, "severity": sev, "why": FLAG_WHY.get(fid, ""),
                    "triggered": bool(cond),
                    "evidence": ({"quote": detail, "page": pg(key), "section": "financial tables"}
                                 if cond else None)})
    ocf = vals("ocf")
    add("ocf_neg_trend", "Operating cash flow negative (multi-year)", "high",
        len(ocf) >= 2 and all(v < 0 for v in ocf), "ocf",
        f"OCF stated as {' | '.join(F.get('ocf',{}).get('values',[]))} - negative across reported years")
    pat = vals("pat")
    add("pat_declining", "Profit declining year on year", "medium",
        len(pat) >= 3 and pat[0] < pat[1] < pat[2], "pat",
        f"PAT trend {' | '.join(F.get('pat',{}).get('values',[]))} (latest first)")
    rev = vals("revenue")
    add("revenue_declining", "Revenue declining year on year", "medium",
        len(rev) >= 3 and rev[0] < rev[1] < rev[2], "revenue",
        f"Revenue trend {' | '.join(F.get('revenue',{}).get('values',[]))} (latest first)")
    bor, nw = vals("borrowings"), vals("networth")
    de = (bor[0] / nw[0]) if (bor and nw and nw[0]) else None
    add("high_leverage", "High leverage (debt exceeds 1.5x net worth)", "high",
        de is not None and de > 1.5, "borrowings",
        f"Borrowings {F.get('borrowings',{}).get('values',[''])[0]} vs net worth "
        f"{F.get('networth',{}).get('values',[''])[0]} (D/E ~ {de:.2f})" if de else "")
    marg = vals("ebitda_margin")
    add("margin_compression", "EBITDA margin compressing", "info",
        len(marg) >= 3 and marg[0] < marg[1] < marg[2], "ebitda_margin",
        f"Margin trend {' | '.join(F.get('ebitda_margin',{}).get('values',[]))} (latest first)")
    rec, rv = vals("receivables"), vals("revenue")
    if len(rec) >= 2 and len(rv) >= 2 and rv[0] and rv[1]:
        d_now, d_prev = rec[0] / rv[0] * 365, rec[1] / rv[1] * 365
        add("receivable_days", "Trade receivables ballooning vs revenue", "high",
            d_prev > 0 and d_now > 60 and d_now > 1.7 * d_prev, "receivables",
            f"Receivable days ~{d_now:.0f} vs ~{d_prev:.0f} a year ago "
            f"(receivables {F.get('receivables',{}).get('values',[''])[0]} on falling/flat revenue) - "
            f"cash is stuck in debtors; check collection terms and the customers behind it")
    eb = vals("ebitda")
    add("ebitda_declining", "EBITDA declining year on year", "medium",
        len(eb) >= 3 and eb[0] < eb[1] < eb[2], "ebitda",
        f"EBITDA trend {' | '.join(F.get('ebitda',{}).get('values',[]))} (latest first)")
    eps = vals("eps")
    add("eps_declining", "EPS declining year on year", "info",
        len(eps) >= 3 and eps[0] < eps[1] < eps[2], "eps",
        f"EPS trend {' | '.join(F.get('eps',{}).get('values',[]))} (latest first)")
    # iteration 8: customer-concentration and thin-coverage flags (real risks that
    # were surfaced only as tensions before). Deterministic, threshold-based.
    tc = vals("top10_customers")
    add("customer_concentration", "High customer concentration", "high",
        bool(tc) and tc[0] >= 60, "top10_customers",
        f"Top-10 customers are {F.get('top10_customers',{}).get('values',[''])[0]} of revenue - "
        f"loss of a major account would materially dent revenue")
    ic = vals("interest_cov")
    add("thin_coverage", "Thin interest coverage", "high",
        bool(ic) and ic[0] < 2.5, "interest_cov",
        f"EBITDA covers interest only {F.get('interest_cov',{}).get('values',[''])[0]} - "
        f"limited cushion to service debt if earnings dip")
    return out


def _correct_contaminated_series(digest, raw_pages):
    """Final guarantee: override any core metric whose value trio was pulled from a
    'basis for issue price' page (which tabulates the company AGAINST peers, mixing
    columns). Re-reads the canonical statement text and replaces the row in place."""
    if not raw_pages: return
    import re as _re
    basis = {p for p, t in raw_pages if t and _re.search(
        r"basis for (the )?(issue|offer) price|comparison with.{0,40}peers", t, _re.I)}
    pats = {
        "revenue": r"revenue\s+from\s+operations\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})",
        "borrowings": r"total\s+borrowings\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})",
        "networth": r"(?:total equity|net\s*worth)\s*(?:\([^)]{0,20}\))?(?:\s*\[[a-z]\])?\s*(?:in\s+.{0,4}million)?\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})",
        "pat": r"(?:restated\s+)?profit\s+for\s+the\s+(?:year|period)\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})\s+([\d][\d,]*\.\d{2})",
    }
    # revenue scale anchor: the company's own metrics are the same order of magnitude
    _rev_anchor = None
    for f in digest.get("fundamentals", []):
        if f["key"] == "revenue":
            try: _rev_anchor = abs(float(f["values"][0].replace(",", "")))
            except Exception: pass
            break
    def _consistent(vals, key=None):
        nums = []
        for v in vals:
            try: nums.append(abs(float(v.replace(",", ""))))
            except ValueError: return False
        if len(nums) < 3 or min(nums) <= 0: return False
        if max(nums) / min(nums) > 8: return False        # fragment/column bleed
        # net worth must be within ~50x of revenue (not a subsidiary's tiny figure)
        if key in ("networth", "borrowings") and _rev_anchor:
            if not (_rev_anchor / 80 <= nums[0] <= _rev_anchor * 20):
                return False
        return True
    canon = {}
    for key, pat in pats.items():
        best = None
        for p, t in raw_pages:
            if not t or p in basis: continue
            for m in _re.finditer(pat, _re.sub(r"\s+", " ", t), _re.I):
                trio = [m.group(1), m.group(2), m.group(3)]
                if _consistent(trio, key):
                    best = (trio, p); break
            if best: break
        if best: canon[key] = best
    # EBITDA arithmetic identity: EBITDA must ≈ revenue × EBITDA-margin. If the
    # extracted EBITDA row disagrees (peer/gross-profit bleed), recompute it from
    # the disclosed margin, which is the reliable figure.
    _bk = {f["key"]: f for f in digest.get("fundamentals", [])}
    if "ebitda" in _bk and "ebitda_margin" in _bk and "revenue" in _bk:
        def _fnum2(v):
            try: return abs(float(str(v).strip("()%").replace(",", "")))
            except Exception: return None
        eb = _fnum2(_bk["ebitda"]["values"][0])
        mg = _fnum2(_bk["ebitda_margin"]["values"][0])
        rv = _fnum2(_bk["revenue"]["values"][0])
        if eb and mg and rv and mg < 80:
            implied = rv * mg / 100
            if implied > 0 and not (implied/1.4 <= eb <= implied*1.4):
                # recompute the whole EBITDA series from margin × revenue per year
                revs = _bk["revenue"]["values"]; mgs = _bk["ebitda_margin"]["values"]
                newv = []
                for i in range(min(len(revs), len(mgs))):
                    r_, m_ = _fnum2(revs[i]), _fnum2(mgs[i])
                    if r_ and m_ is not None: newv.append(f"{r_*m_/100:,.2f}")
                if newv:
                    _bk["ebitda"]["values"] = newv
                    _bk["ebitda"]["label"] = "EBITDA (computed = revenue × margin)"
                    _bk["ebitda"]["section"] = "derived"
    _byk = {f["key"]: f for f in digest.get("fundamentals", [])}
    for key, (vals, pg) in canon.items():
        if key in _byk:
            if _byk[key]["values"][:3] != vals:
                _byk[key]["values"] = vals; _byk[key]["page"] = pg
                _byk[key]["section"] = "financial statements"
        else:
            digest["fundamentals"].append({"key": key, "label":
                {"networth":"Net worth","revenue":"Revenue from operations",
                 "pat":"Profit after tax (PAT)"}.get(key, key.title()),
                "values": vals, "page": pg, "section": "financial statements"})


def build_digest(index, table_fund=None, statements=None, offer=None, segments=None):
    import os as _os
    _compliance = _os.environ.get("COMPLIANCE_MODE", "0") == "1"
    mode = "synthesized" if GEMINI_KEY else "extractive"
    digest = {"mode": mode, "backend": index.backend, "fields": [], "red_flags": []}

    _all = _all_chunks(index)
    _succ = {c["id"]: (_all[i+1]["text"] if i+1 < len(_all) else "")
             for i, c in enumerate(_all)}
    # AUTHORITATIVE criminal check: the litigation summary table states counts by
    # direction. "Against our Company/Promoter/Director: Criminal Nil" = clean,
    # regardless of boilerplate elsewhere that lists "all criminal proceedings".
    # Authoritative criminal check via the litigation SUMMARY TABLE, read from the
    # raw pages (chunk boundaries can split the table). Clear the flag ONLY when every
    # "Against our <party>: Criminal" cell is Nil across the summary.
    _crim_override = None
    _raw = getattr(index, "raw_pages", None) or []
    for p, t in _raw:
        if not t: continue
        flat = re.sub(r"\s+", " ", t)
        if not (re.search(r"against our promoter", flat, re.I) and
                re.search(r"by our promoter", flat, re.I) and "criminal" in flat.lower()):
            continue
        against_vals = re.findall(
            r"against our (?:company|promoter|director|subsidiary|key managerial|senior manage\w*)\s+"
            r"(nil|\d+)", flat, re.I)
        if against_vals and all(v.lower() == "nil" for v in against_vals):
            _crim_override = p
        # a nonzero anywhere in an 'against' criminal cell cancels the override
        if re.search(r"against our (?:company|promoter|director|subsidiary)\s+[1-9]", flat, re.I):
            _crim_override = None; break
    for fid, label, query, section, pattern in RED_FLAG_RULES:
        passages = index.search(query, k=6, section=section) or []
        cands = []
        for p in passages:
            # stitch the successor chunk so a phrase cut by a chunk boundary
            # ("Criminal proceedings | As on the date... there are no...") is seen
            stitched = p["text"] + " " + _succ.get(p.get("id",""), "")[:400]
            for m in re.finditer(pattern, stitched, re.I):
                s0 = max(0, m.start() - 120)
                q = stitched[s0:m.end() + 200].strip()
                wide = stitched[max(0, m.start() - 200):m.end() + 450]
                cands.append({"quote": q, "wide": wide,
                              "page": p["page_start"], "section": p["section"]})
        # existence flags require a POSITIVE assertion: a window that only says
        # "there are no pending criminal proceedings" is a nil-disclosure, not evidence
        _negrx = r"there (are|is) no|no (other )?(pending|outstanding)|not pending|\bnil\b"
        # direction matters: a case the COMPANY filed to recover money is not a
        # governance flag against its promoters/directors
        _WINDOW_REJECT = {"criminal": r"(proceedings?|litigation|criminal) by our (company|promoters?|directors?|subsidiary)|"
                                      r"(company|promoters?) (has|have) filed a (case|complaint|petition) against|"
                                      r"by our promoter[^.]{0,40}\b1\b|"      # summary-table 'By our Promoter Criminal 1'
                                      r"against our (company|promoter|director)[^.]{0,60}\bnil\b"}
        wrej = _WINDOW_REJECT.get(fid)
        clean = [c for c in cands
                 if not re.search(_negrx, c["wide"], re.I)
                 and not (wrej and re.search(wrej, c["wide"], re.I))]
        hit = clean[0] if clean else None
        if hit: hit = {k: v for k, v in hit.items() if k != "wide"}
        if fid == "criminal" and _crim_override is not None:
            hit = None      # summary table says Nil criminal-against: authoritative
        sev = _FLAG_SEV.get(fid, "info")
        if fid == "auditor" and hit:
            w = hit["quote"].lower()
            if re.search(r"going concern|qualified opinion|adverse opinion|disclaimer of opinion", w):
                sev = "high"
            elif re.search(r"special purpose|basis of (its )?accounting|record retention|"
                           r"do(es)? not require any corrective", w):
                sev = "info"; label = label + " (technical/basis-of-preparation)"
            else:
                sev = "medium"
        digest["red_flags"].append({"id": fid, "label": label,
                                    "severity": sev, "why": FLAG_WHY.get(fid, ""),
                                    "triggered": bool(hit), "evidence": hit})

    digest["fundamentals"] = extract_fundamentals(index)
    # RAG provenance: record which pages retrieval located for each field, so the
    # extraction is auditable as retrieval-driven (locate) + validated (select).
    try:
        import drhp_rag
        digest["rag_located"] = {f: drhp_rag.rag_locate_pages(index, f, k=5)
                                 for f in ("revenue","pat","networth","ebitda",
                                           "borrowings","concentration","capacity","waca")}
    except Exception:
        digest["rag_located"] = {}
    # valuation/peers via RAG-locate + grounded LLM structuring (weakest domain lift)
    try:
        import drhp_rag, drhp_llm
        _llm = drhp_llm.complete if not _compliance else None
        digest["peer_comparison"] = drhp_rag.rag_extract_peers(index, llm_fn=_llm)
        # iteration 3: business-model segment summary (grounded LLM)
        digest["business_model"] = drhp_rag.rag_business_model(index, llm_fn=_llm)
        # iteration 2: narrative litigation - only when table parser found nothing
        _lit = digest.get("litigation_itemized") or digest.get("litigation")
        _empty = (not _lit) or (isinstance(_lit, list) and len(_lit) == 0)
        if _empty and _llm is not None:
            _nl = drhp_rag.rag_extract_litigation(index, llm_fn=_llm)
            if _nl and _nl.get("items"):
                digest["litigation_itemized"] = _nl["items"]
                digest["litigation_source"] = "rag-narrative"
                digest["litigation_status"] = "extracted"
            elif _nl is not None:
                # we DID look and the document disclosed nothing
                digest["litigation_status"] = "none_found"
            else:
                # extraction could not run (LLM rate-limited/failed) - record it,
                # so downstream tools never mistake "no data" for "no cases"
                digest["litigation_status"] = "unavailable"
        elif _empty:
            digest["litigation_status"] = "unavailable"
        else:
            digest["litigation_status"] = "extracted"
        # iteration 20: litigation direction/materiality reasoning - the human
        # analyst's last edge (promoter-as-complainant vs accused; amount vs net
        # worth), grounded so an unsupported verdict degrades to 'unclassified'
        _li = digest.get("litigation_itemized")
        if _li and _llm is not None:
            _nw = next((x["values"][0] for x in digest.get("fundamentals", [])
                        if x.get("key") == "networth" and x.get("values")), None)
            _lr = drhp_rag.rag_litigation_reasoning(index, _li, networth=_nw, llm_fn=_llm)
            if _lr:
                digest["litigation_reasoning"] = _lr
        # iteration 11: related-party total - grounded LLM fallback when the doc has
        # no explicit KPI line (regex on RPT-note pages grabs wrong 'Total' rows)
        _F2 = {x.get("key") for x in digest.get("fundamentals", [])}
        if "rpt_total" not in _F2 and _llm is not None:
            _rp = drhp_rag.rag_extract_rpt(index, llm_fn=_llm)
            if _rp and _rp.get("rpt_total"):
                digest.setdefault("rag_rpt", {}).update(_rp)
        # iteration 4: concentration - fill supplier side if the table parser missed it
        _F = {x.get("key") for x in digest.get("fundamentals", [])}
        if "top10_suppliers" not in _F and _llm is not None:
            _cc = drhp_rag.rag_extract_concentration(index, llm_fn=_llm)
            if _cc and _cc.get("supplier_pct"):
                digest.setdefault("rag_concentration", {})["supplier_pct"] = _cc["supplier_pct"]
                digest["rag_concentration"]["source_page"] = _cc.get("source_page")
    except Exception:
        digest["peer_comparison"] = None
    if table_fund:
        import drhp_tables
        digest["fundamentals"] = drhp_tables.merge_into_fundamentals(
            digest["fundamentals"], table_fund)
    digest["fundamentals"] += computed_rows(digest["fundamentals"])
    _byk = {f["key"]: f for f in digest["fundamentals"]}
    if "ocf" in _byk and "pbt" in _byk and _byk["ocf"]["values"] == _byk["pbt"]["values"]:
        # cash-flow statements OPEN with PBT; identical rows mean the text pass
        # grabbed that line. If the TABLE layer holds a genuine different OCF
        # series, swap it in (narrow repair); only drop when no alternative exists.
        _tocf = (table_fund or {}).get("ocf") if isinstance(table_fund, dict) else None
        if _tocf and _tocf.get("values") and _tocf["values"] != _byk["pbt"]["values"]:
            for f in digest["fundamentals"]:
                if f["key"] == "ocf":
                    f["values"] = _tocf["values"]; f["page"] = _tocf.get("page", f.get("page"))
                    f["label"] = "Operating cash flow"; f["section"] = "financial tables"
        else:
            digest["fundamentals"] = [f for f in digest["fundamentals"] if f["key"] != "ocf"]
    digest["statements"] = statements or []
    try:
        _lit = extract_litigation_summary(index)
        digest["litigation_summary"] = _lit
        # build a human one-line-per-matter itemization
        lines = []
        for r in _lit:
            parts = []
            for typ in ("criminal","tax","statutory","civil"):
                v = r.get(typ)
                if v not in (0, "0", "nil", "Nil", None):
                    parts.append(f"{v} {typ}")
            amt = r.get("amount")
            amt_s = f" (₹{amt}M)" if amt not in (0,"0",None) else ""
            if parts:
                lines.append(f"{r['direction'].title()} {r['party']}: " +
                             ", ".join(parts) + amt_s + f" (p.{r['page']})")
        digest["litigation_itemized"] = lines
    except Exception:
        digest["litigation_summary"] = []; digest["litigation_itemized"] = []
    digest["offer"] = offer or {}
    digest["segments"] = segments or []
    try:
        _raw = getattr(index, "raw_pages", None)
        if _raw: _correct_contaminated_series(digest, _raw)
    except Exception: pass
    # ratio consistency: derived ratios MUST be recomputed after every correction
    # pass, or they silently disagree with the values shown beside them (Alpine:
    # borrowings corrected to 1,660.90 while debt/equity stayed at the stale
    # 992.82-based 1.9x - the true ratio is 3.2x, and the LLM even flagged the
    # clash). Drop all previously-derived rows and recompute from final values.
    _base_rows = [f for f in digest["fundamentals"] if f.get("section") != "derived"
                  or "(computed)" not in f.get("label", "")]
    _fresh = computed_rows(_base_rows)
    _fresh_keys = {r["key"] for r in _fresh}
    digest["fundamentals"] = [f for f in digest["fundamentals"]
                              if not (f["key"] in _fresh_keys
                                      and "(computed)" in f.get("label", ""))] + _fresh
    # duplicate-label dedupe: two extraction keys can carry the same display
    # label (tables' current_ratio vs text's cratio). Same label twice with
    # near-identical values looks broken; materially different values are a
    # mini-duality and get an explicit "(alt.)" tag instead of a silent twin.
    _seen_lbl = {}
    _kept = []
    for _f in digest["fundamentals"]:
        _l = _f.get("label", "")
        if _l in _seen_lbl:
            try:
                a = float(str(_f["values"][0]).replace(",", "").strip("()%x"))
                b = float(str(_seen_lbl[_l]["values"][0]).replace(",", "").strip("()%x"))
                if b and abs(a - b) / abs(b) <= 0.05:
                    continue                       # same figure, different route: drop twin
            except (ValueError, IndexError, ZeroDivisionError):
                continue
            _f["label"] = _l + " (alt. source)"     # materially different: disclose
        _seen_lbl[_f.get("label", "")] = _f
        _kept.append(_f)
    digest["fundamentals"] = _kept
    consistency_audit(digest)
    # SELF-CORRECTION: detection alone leaves a hole where a number should be.
    # If the auditor flagged anything, hand the digest to the repair agent, which
    # re-extracts each suspect metric from authoritative statement pages and
    # accepts a candidate ONLY if it passes the same cross-check that rejected
    # the original. Bounded, verifier-gated, and cannot make the output worse.
    if digest.get("suspect_metrics"):
        try:
            import drhp_autofix
            drhp_autofix.autofix(index, digest)
        except Exception as _e:
            digest.setdefault("data_warnings", []).append(
                f"self-repair unavailable ({type(_e).__name__})")

    # iteration 23: statement-level dualities - the document itself prints two
    # totals for some metrics (consolidated vs standalone); disclose both.
    try:
        digest["dualities"] = detect_statement_dualities(
            getattr(index, "raw_pages", None) or [], digest["fundamentals"])
    except Exception:
        digest["dualities"] = []
    # qualifier-carrying capacity label: when the disclosure reads "capacity
    # utilisation at processing, dyeing", the qualifier says WHICH capacity - docs
    # often report several (per process/unit). Appending it prevents a bare number
    # being mistaken for the aggregate. General: any "at <words>" qualifier on the
    # cited page is captured; docs without one keep the plain label.
    try:
        _raw2 = {p: t for p, t in (getattr(index, "raw_pages", None) or [])}
        for _f in digest["fundamentals"]:
            if _f.get("key") == "capacity_util" and _f.get("page") in _raw2:
                _m = re.search(r"capacity\s+utili[sz]ation\s+(at\s+[a-z][a-z ,&/-]{2,40}?)\s*[,%(]",
                                _raw2[_f["page"]], re.I)
                if _m and "(" not in _m.group(1):
                    _f["label"] = f"Capacity utilisation ({_m.group(1).strip().rstrip(',')})"
    except Exception as _sw:
        drhp_log.swallowed("drhp_digest", _sw)
        pass
    digest["red_flags"] += numeric_flags(digest["fundamentals"])
    try:
        import drhp_contradictions
        digest["contradictions"] = drhp_contradictions.detect(digest, index=index)
    except Exception:
        digest["contradictions"] = []
    # RAG analytical layer: retrieval-driven prose components (business, risks,
    # strategy, moat, industry, management...). Semantic, page-cited, no hallucination.
    try:
        import drhp_rag
        digest["rag_analysis"] = drhp_rag.rag_analyze(index)
    except Exception:
        digest["rag_analysis"] = {}
    digest["summary"] = build_summary(index, digest["fundamentals"], digest["red_flags"])
    if _compliance:
        mode = "extractive"       # pure relay: zero LLM anywhere in the output
        digest["compliance_mode"] = True
    if mode == "synthesized":
        _polish_summary(index, digest)
        th = _thesis(index, digest)
        digest["thesis"] = th
        wv = _weave_contradictions(index, digest)
        if wv: digest["contradiction_narrative"] = wv

    FIELD_OPTS = {"risks": {"per_chunk": 2}, "litigation": {"max_digit": 0.4}}
    for key, label, query, section, k, req, rej in DIGEST_FIELDS:
        if key == "litigation" and hasattr(index, "sweep"):
            # completeness mode: scan the WHOLE litigation section, not top-k -
            # "all cases" cannot be answered from six retrieved chunks
            passages = index.sweep("litigation",
                r"(criminal|civil|tax|regulatory)\s+(proceeding|case|matter|litigation)s?"
                r"|there (are|is) no outstanding") or \
                index.search(query, k=k, section=section)
        else:
            passages = index.search(query, k=k, section=section) or index.search(query, k=k)
        if not passages: continue
        items, note = (None, None)
        if mode == "synthesized":
            items, note = _synthesize(label, passages)
        if items is None:
            items = _top_sentences(passages, limit=6, require=req, reject=rej,
                                   **FIELD_OPTS.get(key, {}))
        digest["fields"].append({
            "key": key, "label": label, "items": items, "note": note,
            "evidence": [{"text": p["text"][:700], "page": p["page_start"],
                          "section": p["section"], "score": p["score"]}
                         for p in passages[:4]]})
    _fmap = {f["key"]: f for f in digest["fields"]}
    digest["fields"] = [_fmap[k] for k in _FIELD_ORDER if k in _fmap] + \
                       [f for f in digest["fields"] if f["key"] not in set(_FIELD_ORDER)]
    for f in digest["fields"]:
        f["intro"] = FIELD_INTRO.get(f["key"], "")
    digest["flags_triggered"] = sum(1 for f in digest["red_flags"] if f["triggered"])
    return digest

# ------------------------------------------------------------- ask
_QUERY_EXPAND = [
    (r"\bp\.?\s*/?\s*e\b|price.{0,6}earnings",
     "price earnings ratio P/E basis for issue price peer comparison", "basis_of_price"),
    (r"\beps\b",           "earnings per share EPS basic diluted", "basis_of_price"),
    (r"\broe\b|\bronw\b",  "return on net worth ROE RoNW", "basis_of_price"),
    (r"\brevenue\b|topline","revenue from operations fiscal", "financials"),
    (r"\bpat\b|profit",    "profit after tax PAT restated", "financials"),
    (r"\bdebt\b|borrowing","total borrowings indebtedness", "financials"),
    (r"promoter",          "our promoters shareholding experience pledge", "promoters"),
    (r"litigat|case|lawsuit|criminal", "outstanding litigation criminal proceedings amounts", "litigation"),
    (r"dividend",          "dividend policy declared paid", "dividend"),
    (r"risk",              "material risk factors specific to our company", "risk_factors"),
    (r"object|proceeds|money", "objects of the offer net proceeds utilisation", "objects"),
]
_METRIC_HINT = {"pe": r"\bp\.?\s*/?\s*e\b", "eps": r"\beps\b", "roe": r"\broe\b|\bronw\b",
                "revenue": r"revenue|topline|top line|turnover|sales",
                "pat": r"\bpat\b|profits?\b|earnings|bottom ?line|net profit|net income",
                "ocf": r"cash ?flow|operating cash", "roce": r"\broce\b",
                "cratio": r"current ratio", "networth": r"net ?worth|shareholders.? funds|book value",
                "ebitda": r"\bebitda\b", "ebitda_margin": r"margin",
                "borrowings": r"\bdebt\b|borrowing|loans?\b|owed"}

# canonical finance vocabulary for typo repair ("revenucue" -> "revenue"). Users
# type fast and misspell; retrieval on the raw string then matches junk. Words
# >=5 chars within edit distance 2 of a canonical term are corrected before any
# routing - deterministic, no LLM.
_FIN_VOCAB = ["revenue", "earnings", "profit", "ebitda", "margin", "borrowings",
              "networth", "cashflow", "receivables", "inventory", "dividend",
              "litigation", "promoters", "valuation", "capacity", "customers",
              "suppliers", "growth", "total", "income", "yearly"]

def _edit1or2(a, b):
    """cheap bounded edit distance (<=2) without an import"""
    if abs(len(a) - len(b)) > 2: return False
    # classic DP, small strings only
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > 2: return False
        prev = cur
    return prev[-1] <= 2

def _fix_typos(q):
    out = []
    for w in re.split(r"(\W+)", q):
        wl = w.lower()
        if wl.isalpha() and len(wl) >= 5 and wl not in _FIN_VOCAB:
            for v in _FIN_VOCAB:
                if _edit1or2(wl, v):
                    w = v; break
        out.append(w)
    return "".join(out)

def _num_f(v):
    try: 
        s2 = str(v).replace(",", "").strip()
        return -float(s2.strip("()")) if s2.startswith("(") else float(s2.rstrip("%x"))
    except ValueError: return None

def _metric_answer(q, fundamentals):
    """Direct answer for metric questions from the digest's merged, corrected
    fundamentals (the single source of truth) - handles MULTIPLE metrics in one
    question ("earnings vs revenue yoy") and computes YoY when asked. Returns
    None when no metric matches, so retrieval still serves narrative questions."""
    F = {f["key"]: f for f in fundamentals}
    matched = []
    for key, pat in _METRIC_HINT.items():
        if key in F and re.search(pat, q, re.I) and key not in [m[0] for m in matched]:
            matched.append((key, F[key]))
    if not matched:
        return None
    want_yoy = bool(re.search(r"\byoy\b|year.on.year|growth|grew|trend|vs\b|versus|compare|"
                              r"going (up|down)|rising|falling|increas|decreas|improv|declin", q, re.I))
    items = []
    for key, f in matched[:4]:
        vals = f.get("values", [])[:3]
        line = f"{f['label']}: {' | '.join(vals)} (latest first)"
        if want_yoy and len(vals) >= 2:
            nums = [_num_f(v) for v in vals]
            deltas = []
            for i in range(len(nums) - 1):
                if nums[i] is not None and nums[i+1]:
                    deltas.append(f"{(nums[i]/nums[i+1]-1)*100:+.1f}%")
            if deltas:
                line += f"  | YoY (computed): {', '.join(deltas)}"
        items.append({"text": line, "page": f.get("page"), "section": f.get("section")})
    return {"answer": items,
            "note": "answered from the parsed statements; YoY figures are computed, not printed",
            "evidence": []}

_GENERAL_Q = re.compile(r"(good ipo|should i (apply|invest|buy)|worth (it|applying|investing)|"
                        r"^summar|overview|thoughts|review|verdict|apply or not|is it safe)", re.I)

def answer_question(index, question, k=6, digest=None):
    q = question.strip()
    if digest and _GENERAL_Q.search(q):
        items = []
        trig = [f for f in digest["red_flags"] if f["triggered"]]
        hi = [f for f in trig if f["severity"] == "high"]
        items.append({"text": f"This tool reads the prospectus; it does not give investment advice. "
                              f"From this document: {len(trig)} red flags triggered "
                              f"({len(hi)} high severity).",
                      "page": trig[0]["evidence"]["page"] if trig else 1, "section": "summary"})
        for f in trig[:3]:
            items.append({"text": f"{f['label']} ({f['severity']})",
                          "page": f["evidence"]["page"], "section": "red flag"})
        for it in digest["summary"]["negatives"][:2] + digest["summary"]["positives"][:2]:
            items.append(it)
        return {"answer": items, "note": "decision aid built from the document; verify and decide yourself",
                "evidence": []}
    # typo repair before any routing ("revenucue" -> "revenue")
    q = _fix_typos(q)
    # fast path: metric questions answered from the DIGEST's merged, corrected
    # fundamentals (not a re-extraction) - supports multiple metrics + YoY
    _fnd = (digest or {}).get("fundamentals") or extract_fundamentals(index)
    _ma = _metric_answer(q, _fnd)
    if _ma:
        return _ma
    # honest P/E answer when the RHP still has a placeholder price
    if re.search(r"\bp\.?\s*/?\s*e\b|price.{0,6}earnings", q, re.I):
        hits = index.search("price to earnings P/E ratio in relation to issue price", k=3,
                            section="basis_of_price")
        if hits and "\u25cf" in hits[0]["text"]:
            return {"answer": [{"text": "The company's own P/E is not stated yet: the issue price is "
                                        "still a placeholder in this RHP (it is fixed after book-building). "
                                        "See the Basis for Issue Price section for peer P/E comparisons.",
                                "page": hits[0]["page_start"], "section": "basis_of_price"}],
                    "note": None,
                    "evidence": [{"text": hits[0]["text"][:700], "page": hits[0]["page_start"],
                                  "section": "basis_of_price", "score": hits[0]["score"]}]}
    # query expansion for short/slang questions
    section = None
    for pat, expansion, sec in _QUERY_EXPAND:
        if re.search(pat, q, re.I):
            q, section = q + " " + expansion, sec
            break
    passages = index.search(q, k=k, section=section) or index.search(q, k=k)
    if not passages:
        return {"answer": [], "evidence": [], "note": "nothing relevant found"}
    # reuse the digest's per-field quality filters when the ask routes to a section
    _by_sec = {f[3]: (f[5], f[6]) for f in DIGEST_FIELDS if f[3]}
    req, rej = _by_sec.get(section, (None, None))
    if GEMINI_KEY:
        items, note = _synthesize(question, passages)
        if items:
            return {"answer": items, "note": note,
                    "evidence": [{"text": p["text"][:700], "page": p["page_start"],
                                  "section": p["section"], "score": p["score"]}
                                 for p in passages[:4]]}
    ans = _top_sentences(passages, limit=5, require=req, reject=rej)
    if not ans:
        ans = _top_sentences(passages, limit=5)
    # relevance gate: an extractive answer must actually engage the question.
    # Sentences sharing zero content words with the (typo-repaired) question are
    # retrieval noise - table headers score high on tf-idf but answer nothing.
    # Showing them as an "answer" is worse than admitting no passage matches.
    _stop = {"what", "how", "when", "where", "does", "the", "and", "are", "is",
             "of", "for", "with", "this", "that", "have", "has", "bro", "much",
             "many", "any", "there", "their", "they", "about", "tell", "show"}
    _qtok = {w for w in re.findall(r"[a-z]{4,}", q.lower()) if w not in _stop}
    if _qtok:
        _kept = []
        for a in ans:
            at = a["text"].lower()
            if any(t in at for t in _qtok):
                _kept.append(a)
        if _kept:
            ans = _kept
        else:
            return {"answer": [{"text": "No clear passage in the prospectus directly "
                                        "answers this. Try naming the metric or topic "
                                        "(e.g. 'revenue growth', 'litigation against "
                                        "promoters', 'use of proceeds').",
                                "page": None, "section": None}],
                    "note": "no relevant extractive match - not shown rather than shown wrong",
                    "evidence": [{"text": p["text"][:300], "page": p.get("page_start"),
                                  "section": p.get("section"), "score": p.get("score")}
                                 for p in passages[:2]]}
    return {"answer": ans, "note": None,
            "evidence": [{"text": p["text"][:700], "page": p["page_start"],
                          "section": p["section"], "score": p["score"]}
                         for p in passages[:4]]}


# ---------------------------------------------------------------- AI analyst layer
def _fy_labelled(f):
    """Values with explicit fiscal-year labels so an LLM can never misread
    latest-first ordering as a decline (the direction-hallucination bug)."""
    yrs = f.get("years") or []
    if len(yrs) >= len(f["values"]):
        return "; ".join(f"FY{y}: {v}" for y, v in zip(yrs, f["values"]))
    return "; ".join(f"{lbl}: {v}" for lbl, v in
                     zip(["latest year", "prior year", "two years ago"], f["values"]))

def _facts_blob(digest):
    """Serialize the digest's extracted facts (with pages) as the ONLY input
    the analyst LLM is allowed to reason from."""
    L = []
    for f in digest.get("fundamentals", []):
        L.append(f"[p.{f['page']}] {f['label']}: {_fy_labelled(f)}")
    for grp in ("positives", "negatives", "neutral"):
        for it in digest["summary"].get(grp, []):
            L.append(f"[p.{it['page']}] ({grp}) {it['text']}")
    for fl in digest.get("red_flags", []):
        if fl["triggered"]:
            L.append(f"[p.{fl['evidence']['page']}] (red flag) {fl['label']}: "
                     f"{fl['evidence']['quote'][:220]}")
    for f in digest.get("fields", []):
        for it in f["items"][:3]:
            L.append(f"[p.{it['page']}] ({f['key']}) {it['text']}")
    return "\n".join(L)


def build_analysis(digest, index=None):
    """Grounded analyst explanation. Uses ONLY the digest's extracted facts;
    every claim must carry its page citation; numbers are verified against the
    facts and flagged if unverifiable. Returns None when no LLM is configured,
    so the scanner stays fully functional without a key."""
    if not GEMINI_KEY:
        return None
    facts = _facts_blob(digest)
    prompt = (
        "You are an equity analyst explaining an Indian IPO prospectus to a busy "
        "professional. Below are verified FACTS extracted from the prospectus, each "
        "tagged with its page. Rules: use ONLY these facts, no outside knowledge; "
        "every sentence must end with its page citation like (p.47); plain English; "
        "be direct about weaknesses.\n\n"
        "Write exactly these four titled parts, 2-4 sentences each:\n"
        "WHAT THIS COMPANY IS:\nFINANCIAL QUALITY:\nMAIN CONCERNS:\nWHAT TO VERIFY BEFORE APPLYING:\n\n"
        "FACTS:\n" + facts[:15000])
    try:
        out = _gemini(prompt).strip()
    except Exception as e:
        return {"kind": "ai", "error": f"llm error: {type(e).__name__}"}
    off = _grounded_numbers(out, facts)
    parts = []
    for m in re.finditer(r"(WHAT THIS COMPANY IS|FINANCIAL QUALITY|MAIN CONCERNS|"
                         r"WHAT TO VERIFY BEFORE APPLYING)\s*:\s*(.+?)(?=(?:WHAT THIS COMPANY IS|"
                         r"FINANCIAL QUALITY|MAIN CONCERNS|WHAT TO VERIFY BEFORE APPLYING)\s*:|$)",
                         out, re.S):
        _txt = re.sub(r"\s+", " ", m.group(2)).strip()
        _txt = re.sub(r"\*{2,}|#{2,}|`+", "", _txt).strip()   # model-specific markdown junk
        parts.append({"title": m.group(1).title(), "text": _txt})
    return {"kind": "ai",
            "parts": parts if parts else [{"title": "Analyst read", "text": out}],
            "note": (f"groundedness: {len(off)} unverified number(s): {', '.join(off[:5])}"
                     if off else "all numbers verified against extracted facts")}


_DUAL_KEYS = {
    "borrowings": r"total\s+borrowings",
    "revenue":    r"revenue\s+from\s+operations",
    "ocf":        r"net\s+cash\s+(?:flows?\s+)?(?:generated|used)[^\n]{0,40}operat\w*\s+activit",
    "networth":   r"net\s*worth",
    "pat":        r"(?:profit|loss)\s+(?:after\s+tax|for\s+the\s+(?:year|period))",
}

def _statement_of(pages_dict, pno, max_back=4):
    """Classify which statement a page belongs to by scanning the page and up to
    max_back pages behind it for the nearest Consolidated/Standalone header."""
    for p in range(pno, max(pno - max_back, 0) - 1, -1):
        t = (pages_dict.get(p) or "")[:4000].lower()
        if "subsidiar" in t[:600]:
            return "subsidiary"
        cons = "consolidated" in t
        std = "standalone" in t
        if cons and not std: return "consolidated"
        if std and not cons: return "standalone"
        if cons and std:
            ic, is_ = t.find("consolidated"), t.find("standalone")
            return "consolidated" if ic < is_ else "standalone"
    return "unlabelled"

def detect_statement_dualities(raw_pages, fundamentals):
    """The documents themselves print TWO totals for some metrics (consolidated
    vs standalone, KPI-vs-note). No extractor can resolve that - only disclose
    it. For each duality-prone metric, scan the whole document for label rows
    carrying a value series; when a series materially different (>2% on the
    latest value) from the chosen one exists, attach it as an alternative WITH
    its statement tag, and record the duality. Deliberately discloses, never
    swaps: the chosen headline stays; the reader sees both, page-cited."""
    pages_dict = {p: t for p, t in (raw_pages or [])}
    byk = {f.get("key"): f for f in fundamentals}
    out = []
    for key, pat in _DUAL_KEYS.items():
        f = byk.get(key)
        if not f or not f.get("values"): continue
        try:
            chosen = float(str(f["values"][0]).replace(",", "").strip("()"))
        except ValueError as _sw:
            drhp_log.swallowed("drhp_digest", _sw)
            continue
        own_years = set()
        for v in f.get("values", [])[:4]:
            try: own_years.add(float(str(v).replace(",", "").strip("()")))
            except ValueError: pass
        seen = []
        for p, t in pages_dict.items():
            for line in re.sub(r"[ \t]+", " ", t).splitlines():
                m = re.search(r"^\s{0,8}(?:" + pat + r")", line, re.I)
                if not m: continue                             # statement rows start with the label
                tail = line[m.end():]
                if "%" in tail[:60]: continue                  # concentration/margin prose
                nums = re.findall(r"(?<![\d,.])\(?\d[\d,]*\.\d{2}\)?", tail)
                if len(nums) < 2: continue
                try:
                    v0 = float(nums[0].replace(",", "").strip("()"))
                except ValueError as _sw:
                    drhp_log.swallowed("drhp_digest", _sw)
                    continue
                if v0 <= 0: continue
                # breakdown rows: segment/split lines CONTAIN the chosen total
                _all = [float(n.replace(",", "").strip("()")) for n in nums
                        if n.replace(",", "").strip("()").replace(".", "").isdigit()]
                if any(abs(a - chosen) / max(chosen, 1e-9) <= 0.005 for a in _all[1:]):
                    continue
                if key == "pat" and re.search(r"\bbefore\b", line, re.I):
                    continue                                   # pre-tax line, not PAT
                rel = abs(v0 - chosen) / max(chosen, 1e-9)
                if rel <= 0.02: continue                       # same series
                if not (0.3 <= v0 / max(chosen, 1e-9) <= 3.5):
                    continue                                   # different quantity, not a duality
                if any(abs(v0 - y) / max(y, 1e-9) <= 0.02 for y in own_years):
                    continue                                   # prior-year value, same series
                if any(abs(v0 - s[0]) / max(s[0], 1e-9) <= 0.02 for s in seen):
                    continue                                   # already recorded
                seen.append((v0, nums[:3], p))
        for v0, series, p in seen[:1]:
            _st = _statement_of(pages_dict, p)
            if _st == "subsidiary":
                continue                    # a subsidiary's own statements, not the issuer's
            alt = {"values": series, "page": p,
                   "statement": _st}
            f.setdefault("alternatives", []).append(alt)
            out.append({"key": key, "label": f.get("label"),
                        "chosen": f["values"][0], "chosen_page": f.get("page"),
                        "alt": series[0], "alt_page": p,
                        "alt_statement": alt["statement"],
                        "note": "document prints two totals (different statement/"
                                "presentation); both shown, page-cited"})
    return out


def _num(v):
    """parse a displayed value back to a float; (1,2) means negative"""
    try:
        t = str(v).replace(",", "").strip().rstrip("%").rstrip("xX").strip()
        if t.startswith("(") and t.endswith(")"):
            return -float(t[1:-1])
        return float(t)
    except (ValueError, AttributeError):
        return None


def consistency_audit(digest):
    """Cross-check the extracted numbers AGAINST EACH OTHER and refuse to show
    values that are arithmetically impossible.

    Why this exists: golden tests prove we do not REGRESS on five known
    documents; they cannot prove we GENERALISE to a sixth layout. On an unseen
    prospectus the extractor picked a wrong revenue row and a wrong net-worth
    row, and the page then displayed - side by side - a revenue that was 7% of
    total income, a net worth that contradicted the document's own printed ROE
    by 17x, an EBITDA margin of 8% next to an EBITDA of 117% of revenue, and a
    salary bill of 119% of revenue. Every one of those is checkable with
    arithmetic we already have. A wrong number shown confidently is worse than
    a missing one, so a metric that fails its cross-check is marked suspect,
    and any derived ratio built on it is withdrawn rather than published.

    Returns a list of warnings and mutates the digest in place.
    """
    F = {f["key"]: f for f in digest.get("fundamentals", [])}
    def v(k, i=0):
        f = F.get(k)
        return _num(f["values"][i]) if f and len(f.get("values", [])) > i else None

    suspect, warn = set(), []

    def flag(key, msg):
        if key in F and key not in suspect:
            suspect.add(key)
            warn.append(msg)

    # 1. revenue must be a sensible share of total income. Other income is a
    #    minority line by definition; a revenue far below total income means a
    #    segment/KPI row was captured instead of the P&L line.
    rev, ti = v("revenue"), v("total_income")
    if rev and ti and ti > 0 and rev < drhp_config.AUDIT_REV_SHARE_MIN * ti:
        flag("revenue", f"revenue {rev:,.2f} is only {rev/ti:.0%} of total income "
                        f"{ti:,.2f} - likely the wrong row was captured")

    # 2. the document's own ROE implies a net worth. If the captured net worth
    #    disagrees by more than 2x, one of them is not what we think it is.
    pat, roe, nw = v("pat"), v("roe"), v("networth")
    if pat and roe and nw and roe > 0:
        implied = pat / (roe / 100.0)
        if implied > 0 and (nw / implied > drhp_config.AUDIT_ROE_DISAGREE_X or implied / nw > drhp_config.AUDIT_ROE_DISAGREE_X):
            flag("networth", f"net worth {nw:,.2f} contradicts the stated ROE "
                             f"{roe:.2f}% on PAT {pat:,.2f} (implies ~{implied:,.0f})")

    # 3. EBITDA must be consistent with revenue x its own margin
    ebitda, marg = v("ebitda"), v("ebitda_margin")
    if ebitda and marg and rev and rev > 0:
        shown = 100.0 * ebitda / rev
        if abs(shown - marg) > max(drhp_config.AUDIT_EBITDA_ABS_PTS, drhp_config.AUDIT_EBITDA_REL_FRAC * marg):
            flag("ebitda", f"EBITDA {ebitda:,.2f} is {shown:.0f}% of revenue but the "
                           f"stated margin is {marg:.2f}%")

    # 4. implausible-by-construction ratios
    wf = v("workforce_pct")
    if wf and wf > drhp_config.AUDIT_WORKFORCE_MAX:
        flag("workforce_pct", f"workforce cost is {wf:.0f}% of revenue - implausible; "
                              f"the revenue base is probably wrong")

    # 5. malformed thousands grouping ("2,3,5" parses to 235.0, so a float()
    #    check cannot catch it - the grouping itself is the tell). Valid groups
    #    after the first are 3 digits (western) or 2 (Indian lakh format).
    for k, f in list(F.items()):
        for val in f.get("values", []):
            t = str(val).strip().lstrip("(").rstrip(")").split(".")[0]
            parts = t.split(",")
            if len(parts) > 1 and any(len(p) not in (2, 3) for p in parts[1:]):
                flag(k, f"{f.get('label', k)} has a malformed number '{val}'")
                break

    if not suspect:
        digest["data_warnings"] = []
        digest["suspect_metrics"] = []      # must CLEAR, or a repaired metric
        return []                          # stays flagged for the rest of the run

    # withdraw every derived ratio that depends on a suspect input - publishing
    # "debt/equity 5.2x" off a broken net worth turns one extraction miss into a
    # false narrative (it drove two tensions and a red flag on that document)
    DEPENDS = {
        "debt_equity": ("networth",), "roe": ("networth", "pat"),
        "roce": ("networth",), "nd_ebitda": ("ebitda",),
        "interest_cov": ("ebitda",), "ebitda_margin": ("revenue", "ebitda"),
        "workforce_pct": ("revenue",), "wc_days": ("revenue",),
        "fixed_asset_turnover": ("revenue",), "top1_customer": ("revenue",),
        "top5_customers": ("revenue",), "top10_customers": ("revenue",),
        "top10_suppliers": ("revenue",),
    }
    withdrawn = [k for k, deps in DEPENDS.items()
                 if k in F and any(d in suspect for d in deps)
                 and "(computed" in F[k].get("label", "")]
    if withdrawn:
        digest["fundamentals"] = [f for f in digest["fundamentals"]
                                  if f["key"] not in withdrawn]
        warn.append("withdrew derived ratios that depend on the values above: "
                    + ", ".join(sorted(withdrawn)))

    # mark the suspect rows so the table itself carries the caveat
    for f in digest["fundamentals"]:
        if f["key"] in suspect and "(unverified" not in f.get("label", ""):
            f["label"] = f["label"] + " (unverified - failed cross-check)"

    digest["data_warnings"] = warn
    digest["suspect_metrics"] = sorted(suspect)
    return warn
