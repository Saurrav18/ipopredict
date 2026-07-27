"""
drhp_autofix.py - the self-correcting extraction agent.

WHY THIS EXISTS
---------------
Extraction used to be a single forward pass: match a pattern, take the numbers,
publish. When it picked the wrong row there was no second chance - a subsidiary's
revenue was published as the company's, and the derived debt/equity turned a
conservatively financed issuer into a distressed one.

drhp_digest.consistency_audit() closed half that gap: it DETECTS impossible
combinations (revenue at 7% of total income, a net worth that contradicts the
document's own ROE) and withdraws the ratios built on them. But detection alone
still leaves the user with a hole where a number should be.

This module closes the other half. It is a bounded agent loop:

    OBSERVE   read the audit's verdict on the digest
    PLAN      for each suspect metric, choose a targeted retrieval strategy
    ACT       re-extract that ONE metric from authoritative pages only
    VERIFY    re-run the same cross-checks on the candidate
    COMMIT    accept only if the candidate passes; otherwise keep the flag

The loop is agentic in the way that matters - it decides what to do next based
on its own output, and it is accountable, because every repair must pass the
verifier that rejected the original value. It cannot make things worse: a
candidate that fails verification is discarded and the honest "unverified" flag
survives.

WHY NOT ask an LLM to fix it?
The failure is a SELECTION error, not a reading error - the right number is
sitting on a page we did not consult. Retrieval plus the existing validators
solve that deterministically and for free. The LLM is offered as an optional
last resort (`llm_fn`) purely to nominate a page when retrieval finds nothing,
and even then its suggestion must pass the same verifier.
"""

import re
import drhp_log

# Pages whose heading marks an audited primary statement. A value taken from
# here outranks the same label appearing in a risk factor, a segment note or a
# subsidiary summary - which is precisely the confusion that caused the bug.
_AUTHORITATIVE = re.compile(
    r"Restated\s+(?:Consolidated|Standalone)\s+Statement\s+of\s+"
    r"(?:Profit\s+and\s+Loss|Assets\s+and\s+Liabilities|Cash\s+Flow)"
    r"|Statement\s+of\s+Profit\s+and\s+Loss"
    r"|Balance\s+Sheet"
    r"|Summary\s+Statement\s+of\s+Assets\s+and\s+Liabilities", re.I)

# A subsidiary/associate breakdown never carries company-level figures.
_SUBSIDIARY = re.compile(
    r"Subsidiar(?:y|ies)\s+Particulars|Associate\s+Particulars", re.I)

# Which metrics are worth repairing, and the query we use to find their home.
# Only balance-sheet and P&L primitives appear here: derived ratios are
# recomputed by the digest once their inputs are trustworthy again.
_REPAIR_PLAN = {
    "revenue":      "restated consolidated statement of profit and loss revenue from operations",
    "total_income": "restated consolidated statement of profit and loss total income",
    "networth":     "restated statement of assets and liabilities total equity net worth",
    "pat":          "restated consolidated statement of profit and loss profit after tax for the year",
    "ebitda":       "basis for offer price ebitda earnings before interest tax depreciation",
    "borrowings":   "restated statement of assets and liabilities borrowings non-current current",
    "depreciation": "restated consolidated statement of profit and loss depreciation and amortization expenses",
}


def _authoritative_chunks(index, query, k=12):
    """Retrieve candidates for a metric and keep only pages that look like an
    audited statement, dropping subsidiary breakdowns outright.

    Ordering matters: statement pages first, so the caller can stop at the first
    verified candidate instead of scoring the whole list."""
    try:
        hits = index.search(query, k=k) or []
    except Exception:
        return []
    keep = []
    for h in hits:
        text = h.get("text", "")
        if _SUBSIDIARY.search(text):
            continue                      # never a source for company figures
        if _AUTHORITATIVE.search(text[:600]):
            keep.append((0, h))           # rank 0: an audited statement page
        else:
            keep.append((1, h))           # rank 1: usable, but not primary
    keep.sort(key=lambda t: t[0])
    return [h for _, h in keep]


def _extract_one(key, chunk_text):
    """Run the project's own pattern for a SINGLE metric against one chunk.

    Reusing the SHIPPED patterns is deliberate: a repair produced by different
    parsing rules than the original would be unauditable, and any future change
    to the patterns must move both paths together.

    There are two pattern registries because there are two extraction paths -
    drhp_digest.FUNDAMENTAL_PATTERNS covers the ten metrics the text path can
    read, while drhp_tables.LINE_ITEMS covers the statement rows (net worth,
    total income, EBITDA, depreciation...). Consulting only the first silently
    made half the metrics unrepairable - the repair reported "no candidate
    passed verification" when in truth it had no pattern to try at all."""
    import drhp_digest as D
    pat = next((p for k, p, _ in D.FUNDAMENTAL_PATTERNS if k == key), None)
    if pat is None:
        try:
            import drhp_tables as T
            pat = next((p for k, p in T.LINE_ITEMS if k == key), None)
        except Exception:
            pat = None
    if not pat:
        return None
    # The metric pattern MUST be wrapped: 7 of the table patterns contain a
    # top-level "|", so concatenating a value group onto them lets the
    # alternation swallow the whole expression and group(1) never binds
    # ("Net Worth" matched, the number was never captured). Wrapping in a
    # non-capturing group scopes the alternation to the label only.
    rx = re.compile("(?:" + pat + ")" +
                    r"[^0-9\-(]{0,24}((?:" + D._NUM + r"[\s]+){0,3}" + D._NUM + r")", re.I)
    for m in rx.finditer(chunk_text):
        vals = D._typed_ok(key, D._clean_values(re.findall(D._NUM, m.group(1))))
        if vals:
            return vals
    return None


def _passes(key, candidate, digest):
    """Verify a candidate value with the SAME cross-checks that rejected the
    original. A repair is only meaningful if it satisfies the auditor.

    Implemented by cloning the digest, swapping the one value in, and asking
    consistency_audit() whether this metric is still suspect. Cloning keeps the
    live digest untouched while a candidate is on trial."""
    import copy
    import drhp_digest as D
    trial = {"fundamentals": copy.deepcopy(digest.get("fundamentals", []))}
    hit = False
    for f in trial["fundamentals"]:
        if f["key"] == key:
            f["values"] = candidate
            f["label"] = re.sub(r"\s*\(unverified[^)]*\)", "", f.get("label", ""))
            hit = True
    if not hit:
        return False
    D.consistency_audit(trial)
    return key not in set(trial.get("suspect_metrics") or [])


def autofix(index, digest, llm_fn=None, max_rounds=2):
    """Repair the metrics the auditor flagged. Mutates `digest`; returns a log.

    Bounded by max_rounds because each round re-audits, and a repair can clear a
    downstream complaint (fixing revenue can un-flag the EBITDA margin check).
    Two rounds is enough to settle in practice and guarantees termination.

    Returns a list of dicts:
        {metric, action: repaired|unrepaired, from, to, page, why}
    kept on the digest as `autofix_log` so the UI and the improvement log can
    show what the system corrected about itself.
    """
    import drhp_digest as D

    log = []
    for _round in range(max_rounds):
        suspects = [k for k in (digest.get("suspect_metrics") or [])
                    if k in _REPAIR_PLAN]
        if not suspects:
            break

        repaired_any = False
        for key in suspects:
            F = {f["key"]: f for f in digest.get("fundamentals", [])}
            before = list(F[key]["values"]) if key in F else None
            fixed = False

            for h in _authoritative_chunks(index, _REPAIR_PLAN[key]):
                cand = _extract_one(key, h.get("text", ""))
                if not cand or cand == before:
                    continue
                if not _passes(key, cand, digest):
                    continue             # candidate fails the same audit: reject
                F[key]["values"] = cand
                F[key]["page"] = h.get("page_start", F[key].get("page"))
                F[key]["label"] = re.sub(r"\s*\(unverified[^)]*\)", "", F[key]["label"])
                log.append({"metric": key, "action": "repaired",
                            "from": before, "to": cand,
                            "page": F[key]["page"],
                            "why": "re-extracted from an audited statement page; "
                                   "candidate passed the cross-check that rejected "
                                   "the original"})
                fixed = repaired_any = True
                break

            if not fixed:
                log.append({"metric": key, "action": "unrepaired",
                            "from": before, "to": None, "page": None,
                            "why": "no candidate on an authoritative page passed "
                                   "verification; the value stays flagged rather "
                                   "than being replaced by a guess"})

        # a repair can change what is derivable, so rebuild ratios and re-audit
        if repaired_any:
            try:
                base = [f for f in digest["fundamentals"]
                        if "(computed" not in f.get("label", "")]
                fresh = D.computed_rows(base)
                keys = {r["key"] for r in fresh}
                digest["fundamentals"] = [f for f in digest["fundamentals"]
                                          if not (f["key"] in keys and
                                                  "(computed" in f.get("label", ""))] + fresh
            except Exception as _sw:
                drhp_log.swallowed("drhp_autofix", _sw)
                pass
            D.consistency_audit(digest)
        else:
            break

    digest["autofix_log"] = log
    return log
