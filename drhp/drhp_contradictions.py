"""
drhp_contradictions.py - where the document argues with itself.

Professional analysts don't just read facts; they notice when facts are in
TENSION. This engine encodes those tension-pairs as deterministic rules over
the page-cited extractions (so it cannot hallucinate): narrative-vs-numbers,
growth-vs-cash, insider-actions-vs-story, capacity-vs-capex, margins-vs-mix,
valuation-vs-peers, and document-vs-web reconciliation.
"""
import re


def _n(v):
    v = str(v).strip()
    neg = v.startswith("(")
    v = v.strip("()%\u20b9").replace(",", "").rstrip("xX").strip()
    try: return -float(v) if neg else float(v)
    except ValueError: return None


def detect(digest, index=None, web_text=None):
    F = {f["key"]: f for f in digest.get("fundamentals", [])}
    flags = {f["id"]: f for f in digest.get("red_flags", []) if f.get("triggered")}
    offer = digest.get("offer") or {}
    out = []

    def vals(k):
        return [x for x in (map(_n, F[k]["values"]) if k in F else []) if x is not None]
    def pg(*ks): return next((F[k]["page"] for k in ks if k in F), None)
    def add(cid, title, a, b, question, sev="notable"):
        out.append({"id": cid, "title": title, "a": a, "b": b,
                    "severity": sev, "question": question})

    # 1. moat narrative vs R&D spend
    rnd = vals("rnd_pct")
    claim = None
    if index is not None:
        _pat = (r"difficult (for competitors )?to replicate|high entry barriers?|"
                r"technical know[-\s]?how|barriers to entry")
        _chunks = index.chunks
        _chunks = list(_chunks.values()) if isinstance(_chunks, dict) else list(_chunks)
        for prefer_business in (True, False):
            for c in _chunks:
                if prefer_business and c.get("section") != "business": continue
                if re.search(_pat, c["text"], re.I):
                    claim = c["page_start"]; break
            if claim: break
    if rnd and rnd[0] < 1.0 and claim:
        add("moat_vs_rnd", "Moat claims vs R&D spend",
            f"Business section claims hard-to-replicate technology (p.{claim})",
            f"R&D spend is only {F['rnd_pct']['values'][0]} of revenue (p.{pg('rnd_pct')})",
            "If the moat is technical, why is R&D under 1% of revenue - is the barrier actually approvals/relationships?")

    # 2. profits vs cash (growth without collection)
    rec, rv = vals("receivables"), vals("revenue")
    ocf_v, pat_v = vals("ocf"), vals("pat")
    if "receivable_days" in flags or ("neg_cashflow" in flags and pat_v and pat_v[0] > 0):
        a = (f"PAT positive at {F['pat']['values'][0]} (p.{pg('pat')})" if "pat" in F else "Profits reported")
        b = (flags.get("receivable_days") or flags.get("neg_cashflow"))["evidence"]
        _q = _clean_quote(b.get("quote"))
        if not _q and ocf_v:
            _q = f"Operating cash flow {F['ocf']['values'][0]} in the latest year"
        add("profit_vs_cash", "Reported profits vs cash reality", a,
            f"{_q or b['quote'][:140]} (p.{b['page']})",
            "Are profits converting to cash, or building up in receivables? Ask for the top-10 debtor ageing.",
            sev="major")
    elif ocf_v and pat_v and ocf_v[0] < 0 and pat_v[0] > 0:
        # numbers-driven: extracted OCF is negative in the latest year while PAT is
        # positive - the divergence itself, independent of how the doc phrases it.
        add("profit_vs_cash", "Reported profits vs cash reality",
            f"PAT positive at {F['pat']['values'][0]} (p.{pg('pat')})",
            f"operating cash flow {F['ocf']['values'][0]} in the same year (p.{pg('ocf')})",
            "Profit is booked but operations consumed cash - where is the gap sitting: receivables, inventory, or advances?",
            sev="major")

    # 3. insider selling vs growth story
    waca = offer.get("promoter_waca", "")
    if ("ofs_heavy" in flags or "100% OFS" in str(offer.get("ofs", ""))) and waca:
        add("sellers_vs_story", "Insider exit vs growth narrative",
            f"Offer is heavily/fully promoter exit; promoter acquisition cost {waca}",
            "Prospectus narrative pitches a multi-year growth story",
            "If the future is bright, why are the people who know the company best selling at these prices?",
            sev="major")

    # 4. capacity paradox: utilization falling while debt-funded expansion
    cu, bor = vals("capacity_util"), vals("borrowings")
    if len(cu) >= 2 and len(bor) >= 2 and cu[0] < cu[-1] - 15 and bor[0] > bor[-1] * 1.8:
        add("capacity_vs_capex", "Expansion into under-utilization",
            f"Capacity utilisation fell {F['capacity_util']['values'][-1]} -> {F['capacity_util']['values'][0]} (p.{pg('capacity_util')})",
            f"Borrowings rose {F['borrowings']['values'][-1]} -> {F['borrowings']['values'][0]} (p.{pg('borrowings')})",
            "Debt-funded capacity is sitting idle - what demand visibility justified the expansion?")

    # 5. margins up while revenue down (mix-driven margins)
    marg, rvv = vals("ebitda_margin"), vals("revenue")
    if len(marg) >= 2 and len(rvv) >= 2 and marg[0] > marg[1] and rvv[0] < rvv[1]:
        add("margin_vs_mix", "Margins improving on falling revenue",
            f"EBITDA margin {F['ebitda_margin']['values'][1]} -> {F['ebitda_margin']['values'][0]} (p.{pg('ebitda_margin')})",
            f"Revenue {F['revenue']['values'][1]} -> {F['revenue']['values'][0]} (p.{pg('revenue')})",
            "Is the margin gain durable pricing power, or just a one-off high-margin order in the mix?")

    # 6. PAT rising while EBITDA falls (below-the-line earnings)
    pat, eb = vals("pat"), vals("ebitda")
    if len(pat) >= 2 and len(eb) >= 2 and pat[0] > pat[1] and eb[0] < eb[1]:
        add("pat_vs_ebitda", "Profit growing below the operating line",
            f"PAT rose to {F['pat']['values'][0]} (p.{pg('pat')})",
            f"EBITDA fell to {F['ebitda']['values'][0]} (p.{pg('ebitda')})",
            "Operating earnings are shrinking while PAT grows - other income, tax, or finance-cost effects? Not repeatable.")

    # 7. "diversified" claim vs measured concentration
    conc = None
    for k in ("top10_customers", "top5_customers"):
        v = vals(k)
        if v and F[k]["values"][0].endswith("%") and v[0] > 50:
            conc = (k, F[k]["values"][0]); break
    dclaim = None
    if index is not None and conc:
        for h in index.search("diversified customer base clients across segments", k=4):
            if re.search(r"diversified (customer|client)", h["text"], re.I):
                dclaim = h["page_start"]; break
    if conc and dclaim:
        add("diverse_vs_conc", "'Diversified' claim vs measured concentration",
            f"Prospectus claims a diversified customer base (p.{dclaim})",
            f"{conc[0].replace('_',' ')} = {conc[1]} of revenue (p.{pg(conc[0])})",
            "Reconcile the diversification claim with majority revenue from a handful of customers.")

    # 8. working-capital deterioration surfaced by the computed ratio
    wc = vals("wc_days")
    if len(wc) >= 2 and wc[0] > 120 and wc[0] > wc[1] * 1.5:
        add("wc_blowout", "Working capital stretching fast",
            f"Working-capital days rose to ~{wc[0]:.0f} (p.{pg('wc_days','receivables')})",
            f"was ~{wc[1]:.0f} a year earlier",
            "Cash is being tied up in receivables/inventory faster than the business grows - who owes it and when does it come back?",
            sev="major")

    # 9. document vs web reconciliation (order book and issue size)
    if web_text:
        m = re.search(r"order book[^.\n]{0,80}?([\d,]+(?:\.\d+)?)\s*crore", web_text, re.I)
        ob = vals("order_book")
        if m and ob:
            web_m = _n(m.group(1)) * 10          # crore -> millions
            if web_m and abs(web_m - ob[0]) / max(ob[0], 1) > 0.25:
                add("doc_vs_web", "Document vs web: order book mismatch",
                    f"Prospectus order book {F['order_book']['values'][0]} million (p.{pg('order_book')})",
                    f"Web sources report ~\u20b9{m.group(1)} crore",
                    "Two grounded sources disagree by >25% - stale disclosure or different scope? Verify before trusting either.",
                    sev="major")
    # 9b. customer concentration as a standalone risk (independent of any
    # "diversified" claim) - >65% of revenue from 10 customers is material
    for ck in ("top10_customers", "top5_customers"):
        cv = vals(ck)
        if cv and F[ck]["values"][0].endswith("%") and cv[0] >= 65:
            already = any(x["id"] == "diverse_vs_conc" for x in out)
            if not already:
                add("customer_conc", "High customer concentration",
                    f"{ck.replace('_',' ')} = {F[ck]['values'][0]} of revenue (p.{pg(ck)})",
                    "revenue depends on a handful of customers with no firm long-term contracts",
                    "If the largest one or two customers reduce orders, what is the revenue at risk?",
                    sev="notable")
            break
    # 10. supplier dependency (concentration on the BUY side is often ignored)
    for sk in ("top10_suppliers",):
        sv = vals(sk)
        if sv and F[sk]["values"][0].endswith("%") and sv[0] > 70:
            add("supplier_conc", "Heavy supplier concentration",
                f"Top-10 suppliers = {F[sk]['values'][0]} of purchases (p.{pg(sk)})",
                "raw-material sourcing is concentrated in few hands",
                "What happens to margins if one major supplier raises prices or exits? Any long-term contracts?",
                sev="notable")
    # 11. debt-funded while cash-negative (leverage + OCF)
    if "high_leverage" in flags and ("neg_cashflow" in flags or "ocf_neg_trend" in flags):
        add("leverage_vs_cash", "Rising debt into negative cash generation",
            "Borrowings exceed 1.5x net worth",
            "operating cash flow is negative",
            "Debt is funding operations that don't yet generate cash - how long can this run before refinancing risk?",
            sev="major")
    # 12. profit up but ROE collapsing (equity dilution masking returns)
    roe = vals("roe")
    pat = vals("pat")
    if len(roe) >= 3 and len(pat) >= 3 and pat[0] > pat[2] and roe[0] < roe[2] * 0.6:
        add("roe_collapse", "Returns diluting despite profit growth",
            f"PAT grew but ROE fell {F['roe']['values'][2]} -> {F['roe']['values'][0]} (p.{pg('roe')})",
            "equity base expanded faster than profits",
            "New equity is diluting returns - is the capital being deployed productively yet?",
            sev="notable")
    # 13. rising leverage AND falling interest coverage (deteriorating solvency)
    de, ic = vals("debt_equity"), vals("interest_cov")
    if len(de) >= 3 and len(ic) >= 3 and de[0] > de[2] and ic[0] < ic[2]:
        add("leverage_deteriorating", "Leverage rising as coverage weakens",
            f"Debt/equity rose {F['debt_equity']['values'][2]} -> {F['debt_equity']['values'][0]} (p.{pg('debt_equity')})",
            f"interest coverage fell {F['interest_cov']['values'][2]} -> {F['interest_cov']['values'][0]} (p.{pg('interest_cov')})",
            "Debt is climbing while the cushion to service it shrinks - how close is the covenant/refinancing risk?",
            sev="major")
    # 14. thin interest coverage in absolute terms (solvency risk)
    if ic and ic[0] < 3.0:
        add("thin_coverage", "Thin interest coverage",
            f"EBITDA covers interest only {F['interest_cov']['values'][0]} (p.{pg('interest_cov')})",
            "below the ~3x comfort threshold lenders watch",
            "A small EBITDA dip could breach coverage - is the debt load sustainable at this margin?",
            sev="major" if ic[0] < 2.0 else "notable")
    # 16. high leverage in absolute terms vs equity (balance-sheet risk)
    if de and de[0] >= 1.5:
        add("high_leverage", "Debt exceeds equity",
            f"Debt/equity at {F['debt_equity']['values'][0]} (p.{pg('debt_equity')})",
            "borrowings are larger than the entire equity base",
            "The balance sheet is debt-heavy - how much of the issue is really to de-lever versus grow?",
            sev="major")
    return out


def _clean_quote(q, maxlen=140):
    """Trim an evidence window to sentence boundaries so the UI never shows a
    mid-sentence fragment ('the Company. Cash flow from Operating Activity
    Reasons for negative...'). Prefer the sentence containing a number; if no
    clean sentence survives, return None so the caller uses a values-based line."""
    if not q:
        return None
    import re as _re
    parts = [p.strip() for p in _re.split(r"(?<=[.!?])\s+", q.strip()) if p.strip()]
    parts = [p for p in parts if len(p) >= 25 and p[0].isupper() or _re.match(r"^[A-Z(\u20b9]", p)]
    if not parts:
        return None
    withnum = [p for p in parts if _re.search(r"\d", p)]
    if not withnum:
        # a tension quote exists to evidence a NUMBER; a numberless window is
        # glued headers/prose fragments - worse than showing a values-based line
        return None
    best = withnum[0]
    # header-glue tell: a capitalized word directly following a lowercase word
    # with no punctuation ("...Activity Reasons for...") means concatenated
    # headings, not a sentence - reject unless it still reads past the glue
    if _re.search(r"[a-z] [A-Z][a-z]+ [A-Z][a-z]+ ", best) and not _re.search(r"[.!?]", best):
        return None
    return best[:maxlen] if len(best) >= 25 else None
