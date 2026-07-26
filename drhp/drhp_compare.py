"""
drhp_compare.py - Dynamic multi-document comparison.

Design rules (per the generalization mandate):
- NO hardcoded document names, counts, or metric lists: the comparison matrix is
  built from the UNION of metrics present across whatever digests are passed in.
  2 docs or 20 - same code path.
- Missing fields render as None (honest n/a), never invented or interpolated.
- The rank score is deterministic and EXPLAINABLE: each component names the
  metric, its value, and the points it contributed - an analyst can audit it.
- Library = one JSON per scanned document; adding an IPO to the comparison is
  just scanning it.
"""
import os, json, re

LIB_DIR = os.environ.get("DRHP_LIBRARY", os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "library"))


def _num(v):
    if v is None: return None
    s = str(v).strip()
    neg = s.startswith("(")
    s = s.strip("()%\u20b9xX ").replace(",", "")
    try: return -float(s) if neg else float(s)
    except ValueError: return None


def save_to_library(name, digest):
    os.makedirs(LIB_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name))[:60] or "doc"
    path = os.path.join(LIB_DIR, safe + ".json")
    slim = {k: digest.get(k) for k in
            ("fundamentals", "red_flags", "contradictions", "offer",
             "peer_comparison", "litigation_itemized", "meta")}
    slim["name"] = name
    with open(path, "w") as f:
        json.dump(slim, f)
    return path


def list_library():
    if not os.path.isdir(LIB_DIR): return []
    out = []
    for fn in sorted(os.listdir(LIB_DIR)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(LIB_DIR, fn)) as f:
                    d = json.load(f)
                out.append({"file": fn, "name": d.get("name", fn[:-5])})
            except Exception:
                pass
    return out


def _load(sel):
    """sel = list of names/files; empty -> whole library."""
    docs = {}
    for entry in list_library():
        if not sel or entry["name"] in sel or entry["file"] in sel \
                or entry["file"][:-5] in sel:
            try:
                with open(os.path.join(LIB_DIR, entry["file"])) as f:
                    docs[entry["name"]] = json.load(f)
            except Exception:
                pass
    return docs


def compare(selection=None, digests=None):
    """Compare any number of documents. `digests` may be passed directly
    (name -> digest) or loaded from the library by `selection`. Returns the
    metric matrix (union of keys), flags, tensions, and an explainable rank."""
    docs = digests or _load(selection or [])
    if len(docs) < 2:
        return {"error": "need at least 2 documents in the library to compare",
                "library": list_library()}
    # metric matrix over the UNION of keys actually present - fully dynamic
    all_keys, per_doc = [], {}
    for name, d in docs.items():
        F = {x["key"]: x for x in d.get("fundamentals", [])}
        per_doc[name] = F
        for k in F:
            if k not in all_keys: all_keys.append(k)
    matrix = {k: {n: (per_doc[n].get(k, {}).get("values") or [None])[0]
                  for n in docs} for k in all_keys}
    flags = {n: [f.get("id") for f in d.get("red_flags", []) if f.get("triggered")]
             for n, d in docs.items()}
    tensions = {n: len(d.get("contradictions", [])) for n, d in docs.items()}
    # explainable score: each component records metric, value, points
    scores = {}
    for n in docs:
        F = per_doc[n]
        comp, pts = [], 0.0
        def add(label, p):
            nonlocal pts; pts += p; comp.append({"component": label, "points": round(p, 2)})
        roe = _num((F.get("roe", {}).get("values") or [None])[0])
        if roe is not None: add(f"ROE {roe:.1f}%", min(max(roe, 0), 30) / 6)
        de = _num((F.get("debt_equity", {}).get("values") or [None])[0])
        if de is not None: add(f"D/E {de:.1f}x", 3 - min(de, 3))
        ocf = _num((F.get("ocf", {}).get("values") or [None])[0])
        pat = _num((F.get("pat", {}).get("values") or [None])[0])
        if ocf is not None and pat and pat > 0:
            conv = ocf / pat
            add(f"OCF/PAT {conv:.2f}", min(max(conv, -1), 1.5) * 2)
        rv = [_num(x) for x in (F.get("revenue", {}).get("values") or [])[:3]]
        if len(rv) >= 3 and all(rv) and rv[2] > 0:
            g = (rv[0] / rv[2]) ** 0.5 - 1
            add(f"rev CAGR~{g*100:.0f}%", min(max(g, 0), 0.5) * 8)
        add(f"{len(flags[n])} flags", -0.7 * len(flags[n]))
        add(f"{tensions[n]} tensions", -0.4 * tensions[n])
        scores[n] = {"score": round(pts, 2), "explain": comp}
    ranked = sorted(scores, key=lambda n: -scores[n]["score"])
    return {"documents": list(docs), "metrics": matrix, "flags": flags,
            "tensions": tensions, "scores": scores, "ranking": ranked,
            "note": "score is a deterministic screen, not advice; every point is itemized in 'explain'"}
