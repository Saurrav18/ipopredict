"""
drhp_corpus.py - cross-document intelligence. Every scan persists the issuer's
key metrics; once 3+ documents are in the corpus, each new digest gets
comparative context ("OCF negative: true for 2 of 7 scanned issuers").
"""
import os, json

PATH = os.environ.get("DRHP_CORPUS", "drhp_corpus.json")

def _load():
    try: return json.load(open(PATH))
    except Exception: return {}

def update(company, digest):
    db = _load()
    F = {f["key"]: f for f in digest.get("fundamentals", [])}
    def _n(k):
        try:
            v = F[k]["values"][0].strip("()%").replace(",", "")
            return float(v) * (-1 if F[k]["values"][0].startswith("(") else 1)
        except Exception: return None
    db[company or "unknown"] = {
        "flags_high": sum(1 for f in digest["red_flags"]
                          if f["triggered"] and f["severity"] == "high"),
        "flags_total": sum(1 for f in digest["red_flags"] if f["triggered"]),
        "ocf_negative": (_n("ocf") or 0) < 0 if "ocf" in F else None,
        "roe": _n("roe"), "roce": _n("roce"), "eps": _n("eps"),
    }
    json.dump(db, open(PATH, "w"), indent=1)
    return db

def context(company):
    db = _load()
    if len(db) < 3 or company not in db: return []
    me, others = db[company], db
    n = len(db)
    out = [f"Compared against {n} issuers scanned by this tool:"]
    worse = sum(1 for v in others.values() if v["flags_total"] > me["flags_total"])
    out.append(f"red flags: {me['flags_total']} triggered; {worse} of {n} issuers have more")
    if me.get("ocf_negative") is not None:
        negs = sum(1 for v in others.values() if v.get("ocf_negative"))
        out.append(f"negative operating cash flow: {negs} of {n} issuers "
                   f"({'including' if me['ocf_negative'] else 'not including'} this one)")
    if me.get("roe") is not None:
        hi = sum(1 for v in others.values() if (v.get("roe") or -1) > me["roe"])
        out.append(f"ROE {me['roe']:.1f}: {hi} of {n} issuers report higher")
    return out
