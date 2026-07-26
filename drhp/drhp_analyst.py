"""
drhp_analyst.py - a deterministic IPO-analyst agent over the extracted data.

Mirrors how professional analysts actually evaluate an RHP, as pillars:
  growth, profitability, EARNINGS QUALITY (OCF vs PAT - the pro move),
  leverage, governance, customer concentration, offer structure, order
  visibility. Every score has reasons with page citations. No LLM in the
  scoring loop - rules over extracted numbers, so it cannot hallucinate.
  An optional LLM narrative (grounded in the computed memo only) can be
  layered on for presentation via drhp_digest._gemini.

Also reports its own COVERAGE GAPS: what an analyst needs that this document
scan did not surface - honesty about missing content is part of the product.

CLI:  py -3.12 drhp_analyst.py            (runs on cached docs if present)
"""
import json, math, re


def _n(v):
    v = str(v).strip()
    neg = v.startswith("(")
    v = v.strip("()%").replace(",", "")
    try: return -float(v) if neg else float(v)
    except ValueError: return None


def _series(F, key):
    return [x for x in (map(_n, F[key]["values"]) if key in F else []) if x is not None]


def analyze(digest, index=None):
    """Full analyst memo from a digest (with table-merged fundamentals)."""
    F = {f["key"]: f for f in digest.get("fundamentals", [])}
    flags = {f["id"]: f for f in digest.get("red_flags", []) if f.get("triggered")}
    P = []      # pillars
    def pillar(name, weight, score, reasons):
        P.append({"name": name, "weight": weight,
                  "score": max(0, min(10, round(score, 1))),
                  "reasons": reasons})
    def pg(k): return F[k]["page"] if k in F else None
    def R(text, k=None, page=None):
        return {"text": text, "page": page or (pg(k) if k else None)}

    # ---- growth ----
    rev = _series(F, "revenue")
    if len(rev) >= 3 and rev[2] > 0:
        cagr = (rev[0] / rev[2]) ** 0.5 - 1
        sc = 5 + min(4.5, cagr * 18) if cagr >= 0 else 3 + cagr * 10
        reasons = [R(f"Revenue {F['revenue']['values'][2]} -> {F['revenue']['values'][0]} "
                     f"(~{cagr*100:.0f}% CAGR over 2 years)", "revenue")]
        if rev[0] < rev[1]:                      # the latest year matters most
            drop = (rev[1] - rev[0]) / rev[1] * 100
            sc -= min(3, drop / 4)
            reasons.append(R(f"BUT revenue FELL {drop:.1f}% in the latest year "
                             f"({F['revenue']['values'][1]} -> {F['revenue']['values'][0]})", "revenue"))
            if index is not None:
                hits = index.search("large order one-time revenue recognised from this order", k=3)
                one = next((h for h in hits
                            if re.search(r"large order|one[- ]time|not be indicative", h["text"], re.I)), None)
                if one:
                    sc += 1
                    reasons.append(R("prior year appears inflated by a large one-off order - "
                                     "normalize before judging the decline (see cited page)",
                                     page=one["page_start"]))
        eb = _series(F, "ebitda")
        if len(eb) >= 3 and eb[0] < eb[1] < eb[2]:
            sc -= 1.5
            reasons.append(R(f"EBITDA declining every year "
                             f"({' -> '.join(reversed(F['ebitda']['values']))}) even as PAT rises - "
                             f"check other income and finance-cost effects", "ebitda"))
        pillar("Growth", 0.15, sc, reasons)
    # ---- profitability ----
    roe, roce = _series(F, "roe"), _series(F, "roce")
    if roe or roce:
        base = (roe[0] if roe else roce[0])
        reasons = []
        if roe:  reasons.append(R(f"ROE {F['roe']['values'][0]}", "roe"))
        if roce: reasons.append(R(f"ROCE {F['roce']['values'][0]}", "roce"))
        marg = _series(F, "ebitda_margin")
        sc = min(9.5, base / 3)
        if len(marg) >= 3 and marg[0] < marg[1] < marg[2]:
            sc -= 1.5; reasons.append(R("EBITDA margin compressing", "ebitda_margin"))
        pillar("Profitability", 0.15, sc, reasons)
    # ---- earnings quality: the professional's first check ----
    pat, ocf = _series(F, "pat"), _series(F, "ocf")
    if pat:
        reasons, sc = [], 6.0
        if ocf:
            if all(v < 0 for v in ocf[:3]) and pat[0] > 0:
                sc = 1.5
                reasons.append(R(f"PAT positive ({F['pat']['values'][0]}) while operating cash flow "
                                 f"is NEGATIVE across reported years ({' | '.join(F['ocf']['values'])}) - "
                                 f"profits are not converting to cash; classic working-capital/accrual risk", "ocf"))
            elif ocf[0] < 0:
                sc = 3.5; reasons.append(R(f"Latest OCF negative ({F['ocf']['values'][0]})", "ocf"))
            elif pat[0] > 0:
                conv = ocf[0] / pat[0]
                sc = 8.5 if conv >= 0.8 else 6.5 if conv >= 0.4 else 4.5
                reasons.append(R(f"OCF/PAT conversion ~{conv:.1f}x", "ocf"))
        else:
            reasons.append(R("Operating cash flow not extracted - verify in the cash flow statement"))
        if "auditor" in flags:
            sc -= 1.5
            reasons.append(R("Auditor qualification / emphasis of matter present",
                             page=flags["auditor"]["evidence"]["page"]))
        pillar("Earnings quality", 0.2, sc, reasons)
    # ---- leverage ----
    bor, nw = _series(F, "borrowings"), _series(F, "networth")
    if bor and nw and nw[0]:
        de = bor[0] / nw[0]
        pillar("Leverage", 0.1, 8.5 - min(6.5, de * 3),
               [R(f"Borrowings {F['borrowings']['values'][0]} vs net worth "
                  f"{F['networth']['values'][0]} (D/E ~ {de:.2f})", "borrowings")])
    # ---- governance ----
    g, reasons = 8.5, []
    for fid, hit, why in (("criminal", 3.0, "Criminal proceedings involving promoters/directors"),
                          ("pledge", 1.5, "Promoter shares pledged"),
                          ("related_dep", 1.5, "Material related-party dependence"),
                          ("losses", 0.5, "History of losses")):
        if fid in flags:
            g -= hit
            reasons.append(R(why, page=flags[fid]["evidence"]["page"]))
    pillar("Governance", 0.2, g, reasons or [R("No governance flags triggered in scan")])
    # ---- customer concentration ----
    conc_pct = None
    for k in ("top5_customers", "top10_customers", "top1_customer"):
        vals = _series(F, k)
        if not vals: continue
        v = vals[0]
        if F[k]["values"][0].endswith("%"):
            conc_pct = (k, v)
        elif rev:
            conc_pct = (k, v / rev[0] * 100)
        if conc_pct: break
    if conc_pct:
        k, pct = conc_pct
        label = k.replace("_", " ")
        pillar("Customer concentration", 0.1, 9 - min(7, max(0, (pct - 25) / 9)),
               [R(f"{label} ~ {pct:.0f}% of revenue", k)])
    # ---- offer structure ----
    if "ofs_heavy" in flags:
        pillar("Offer structure", 0.05, 3,
               [R("Offer-for-sale heavy: company receives little/no fresh money",
                  page=flags["ofs_heavy"]["evidence"]["page"])])
    # ---- order visibility ----
    ob = _series(F, "order_book")
    if ob and rev:
        btb = ob[0] / rev[0]
        pillar("Order visibility", 0.05, 5 + min(4, (btb - 0.5) * 4),
               [R(f"Order book {F['order_book']['values'][0]} ~ {btb:.1f}x latest revenue", "order_book")])

    # ---- weighted stance ----
    tw = sum(p["weight"] for p in P) or 1
    total = sum(p["score"] * p["weight"] for p in P) / tw
    hi_flags = sum(1 for f in flags.values() if f.get("severity") == "high")
    stance = ("POSITIVE BIAS" if total >= 7 and hi_flags == 0 else
              "NEUTRAL" if total >= 5.5 and hi_flags <= 1 else
              "CAUTION" if total >= 4 else "HIGH CAUTION")

    # ---- what an analyst still needs (coverage gaps) ----
    CHECKLIST = [("networth", "net worth"), ("ocf", "operating cash flow"),
                 ("borrowings", "total borrowings"), ("capacity_util", "capacity utilisation"),
                 ("order_book", "order book"), ("rpt_total", "related-party totals"),
                 ("pe", "own P/E (often [\u25cf] pre-pricing)")]
    gaps = [label for key, label in CHECKLIST if key not in F]
    if not any(k in F for k in ("top1_customer", "top5_customers", "top10_customers")):
        gaps.append("customer concentration")

    questions = []
    if ocf and all(v < 0 for v in ocf[:3]):
        questions.append("Why is operating cash flow negative for three straight years while profits grow - receivables or inventory build-up?")
    if "criminal" in flags:
        questions.append("What is the current status and worst-case exposure of the criminal proceedings named in the litigation section?")
    if "pledge" in flags:
        questions.append("What triggers invocation of the pledged promoter shares, and at what level?")
    if conc_pct and conc_pct[1] > 45:
        questions.append("What contractual protection exists on the top customers, and what is the renewal calendar?")
    if "ofs_heavy" in flags:
        questions.append("If the business needs growth capital, why is the offer largely an exit for selling shareholders?")
    waca = (digest.get("offer") or {}).get("promoter_waca", "")
    if waca and ("nil" in waca.lower() or waca.strip("\u20b9() ").replace(".","").isdigit() and float(waca.strip("\u20b9() ").replace(",","")) < 5):
        questions.append(f"Promoters' weighted average cost of acquisition is {waca} versus the offer price - "
                         "judge the risk-reward asymmetry of buying what insiders got for (almost) nothing.")
    segs = digest.get("segments") or []
    if len(segs) >= 2 and all(len(s_["shares"]) >= 2 for s_ in segs[:2]):
        def _p(x): return float(x.strip("%"))
        top = segs[0]
        if abs(_p(top["shares"][0]) - _p(top["shares"][-1])) > 15:
            questions.append(f"Revenue mix is shifting hard ({top['label'].strip()}: "
                             f"{top['shares'][-1]} -> {top['shares'][0]}) - is the change durable or one large order?")

    try:
        import drhp_contradictions
        contradictions = drhp_contradictions.detect(digest, index=index)
    except Exception:
        contradictions = []
    return {"kind": "analyst", "stance": stance, "score": round(total, 1),
            "contradictions": contradictions,
            "high_severity_flags": hi_flags, "pillars": P,
            "questions": questions, "coverage_gaps": gaps,
            "method": "deterministic pillar scoring over page-cited extractions; no generative step"}


def narrative(memo):
    """Optional grounded LLM presentation of the memo (needs GEMINI_API_KEY)."""
    import drhp_digest
    if not drhp_digest.GEMINI_KEY:
        return None
    facts = json.dumps(memo, indent=0)[:12000]
    prompt = ("You are a sell-side analyst. Rewrite this scored memo as 5-8 crisp "
              "sentences a portfolio manager would read. Use ONLY the memo facts; "
              "keep every page citation; state the stance plainly; no advice language.\n" + facts)
    try:
        text = drhp_digest._gemini(prompt).strip()
        off = drhp_digest._grounded_numbers(text, facts)
        return {"text": text, "note": (f"{len(off)} unverified number(s)" if off else "all numbers verified")}
    except Exception as e:
        return {"text": None, "note": f"llm error: {str(e)[:220]}"}


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import drhp_ingest, drhp_digest
    for name in ("ICEL", "RHP2", "HAPPY"):
        try:
            pages = json.load(open(f"/tmp/cache_{name}.json"))
            tf = json.load(open(f"/tmp/tables_{name}.json"))
        except Exception:
            continue
        idx = drhp_ingest.TfidfIndex(drhp_ingest.chunk_document([(p, t) for p, t in pages]))
        d = drhp_digest.build_digest(idx, table_fund=tf)
        m = analyze(d)
        print(f"\n{'='*62}\n{name}: {m['stance']} (score {m['score']}, {m['high_severity_flags']} high flags)")
        for p in m["pillars"]:
            print(f"  {p['name']:<24} {p['score']:>4}/10  w={p['weight']}")
            for r in p["reasons"][:2]:
                print(f"     - {r['text'][:104]}" + (f" (p.{r['page']})" if r['page'] else ""))
        if m["questions"]:
            print("  ANALYST QUESTIONS:")
            for q in m["questions"][:3]: print(f"     ? {q[:110]}")
        if m["coverage_gaps"]:
            print(f"  GAPS: {', '.join(m['coverage_gaps'])}")
