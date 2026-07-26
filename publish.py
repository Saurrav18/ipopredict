"""
Turns the scraper's Excel output into the website's qualified_ipos.json.

Run it right after the scraper, in the same folder:
    python publish.py

For each scraped IPO:
  - has GMP + subscription data  -> scored, with qualifying accuracy tiers
  - still upcoming (no data yet)  -> listed as "upcoming" (no call until data lands)
  - already closed >14 days       -> skipped (belongs in the archive)

Writes qualified_ipos.json.

Point it at the dataset with IPO_DATASET and the scraper output with SCRAPER_XLSX.
"""
import os, json, sys, re, math
from datetime import date as _real_date, timedelta

class date(_real_date):
    """Same as datetime.date, but honors SIM_TODAY (set only by the simulator)
    so you can prove the closing/listing lifecycle without waiting for real dates.
    In normal use SIM_TODAY is unset, so this behaves exactly like date."""
    @classmethod
    def today(cls):
        s = os.environ.get("SIM_TODAY")
        return _real_date.fromisoformat(s) if s else _real_date.today()

# SEBI-safe public build. When on, the published data carries NO forward-looking
# research on live IPOs: no APPLY/SKIP, no score/confidence, no listing-gain
# range (price target), no accuracy tiers, no per-IPO reasoning. Only public
# facts (price, dates, sector, reported subscription/GMP, fundamentals) and, for
# already-listed IPOs, the real outcome. Flip COMPLIANCE_MODE in .env to switch.
COMPLIANCE_MODE = os.environ.get("COMPLIANCE_MODE", "") == "1"

# Fields that constitute "research services" under SEBI and are removed in compliance mode.
_FORWARD_FIELDS = ("win_score", "prediction", "qualifying_tiers", "tier_preliminary",
                   "preliminary", "cases", "note", "signal1_win_probs",
                   "expected_wr", "maxTier", "opens_note")

def _compliance_sanitize(entry):
    """Strip forward-looking research from a live/upcoming entry, leaving only
    public facts. Already-listed IPOs keep their real, factual listing result."""
    for k in _FORWARD_FIELDS:
        entry.pop(k, None)
    entry["compliance_mode"] = True
    # neutral factual state label the compliant UI can show
    if entry.get("state") == "open":
        entry["public_note"] = "Public data for your own research. Not a recommendation."
    return entry


# freeze-and-grade: lock the closing-day prediction, judge it after listing
FROZEN_FILE = os.environ.get("FROZEN_FILE", "predictions_frozen.json")

def _load_frozen():
    try:
        return json.load(open(FROZEN_FILE))
    except Exception:
        return {}

def _save_frozen(d):
    try:
        json.dump(d, open(FROZEN_FILE, "w"), indent=2, default=str)
    except Exception as e:
        print(f"  [freeze] could not write {FROZEN_FILE}: {e}")

def _norm_name(s):
    return re.sub(r'[^a-z0-9]+', ' ', str(s or "").lower()).replace(" ipo", "").strip()

def freeze_prediction(entry):
    """Option B. While an IPO is OPEN, keep refreshing a 'pending' snapshot of its
    latest prediction (so it always reflects the newest subscription/GMP). We do
    NOT lock yet. The lock happens in lock_if_closed(), which runs BEFORE any new
    scrape overwrites things, so the locked call is the last open-day prediction."""
    if entry.get("state") != "open":
        return
    pred = entry.get("prediction") or {}
    frozen = _load_frozen()
    key = _norm_name(entry.get("ipo_name"))
    rec = frozen.get(key)
    if rec and rec.get("locked"):
        return   # already permanently locked, never touch again
    frozen[key] = {
        "ipo_name": entry.get("ipo_name"),
        "locked": False,               # still open -> pending, keeps refreshing
        "updated_on": date.today().isoformat(),
        "verdict": pred.get("verdict"),
        "range_low": pred.get("range_low"),
        "range_high": pred.get("range_high"),
        "median": pred.get("median"),
        "confidence": pred.get("confidence"),
        "max_tier": max((t.get("tier", 0) for t in entry.get("qualifying_tiers", [])), default=None),
    }
    _save_frozen(frozen)

def lock_if_closed(all_names_seen_closed):
    """Permanently lock any pending prediction whose IPO is no longer open (it has
    closed or listed). Runs FIRST, before grading/scraping, so we freeze the clean
    final open-day call. Once locked, it can never change. Returns nothing."""
    frozen = _load_frozen()
    changed = False
    for key, rec in frozen.items():
        if rec.get("locked"):
            continue
        if key in all_names_seen_closed:      # this IPO is closed/settled now
            if rec.get("verdict") is not None:  # we have a real final open-day call
                rec["locked"] = True
                rec["locked_on"] = date.today().isoformat()
                changed = True
                print(f"  [freeze] LOCKED final call for {rec.get('ipo_name')}: "
                      f"{rec.get('verdict')} {rec.get('range_low')}% to {rec.get('range_high')}%")
    if changed:
        _save_frozen(frozen)

def grade_frozen(entry):
    """When a settled IPO has a LOCKED closing-day call, attach it + the verdict
    grade so the UI can show 'we predicted X, it did Y -> HIT/MISS'."""
    actual = entry.get("listing_gain")
    if actual is None and isinstance(entry.get("actual"), dict):
        actual = entry["actual"].get("pct")
    if actual is None:
        return
    frozen = _load_frozen().get(_norm_name(entry.get("ipo_name")))
    if not frozen or not frozen.get("locked"):
        return   # only grade against a permanently-locked call, never a pending one
    verdict = frozen.get("verdict")
    # HIT = the call was right: APPLY and it gained, or SKIP and it did not.
    hit = None
    if verdict == "APPLY":
        hit = actual > 0
    elif verdict == "SKIP":
        hit = actual <= 0
    lo, hi = frozen.get("range_low"), frozen.get("range_high")
    in_range = (lo is not None and hi is not None and lo <= actual <= hi)
    entry["frozen"] = frozen
    entry["graded"] = {
        "verdict": verdict, "actual": actual, "hit": hit,
        "range_low": lo, "range_high": hi, "in_range": in_range,
        "confidence": frozen.get("confidence"),
    }
    if isinstance(entry.get("actual"), dict):
        entry["actual"]["hit"] = hit   # so the ledger row can stamp HIT/MISS



def _json_safe(o):
    """Recursively replace NaN/Infinity (invalid JSON) with None, so the
    browser's JSON.parse never chokes. Pandas leaves NaN in empty cells."""
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


def build_cases(r, s):
    """Build case-for / neutral / case-against (each a list of {head, more})
    from the IPO's data and the scoring result, plus a 'note' warning when
    subscription data is not out yet (a preliminary call)."""
    def g(k, d=None):
        v = r.get(k)
        try:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return d
            return float(v)
        except (TypeError, ValueError):
            return d

    gmp = g("gmp_closing_gain_pct")
    qib, hni, sub = g("QIB"), g("HNI"), g("Total")
    pe, roe = g("pre_issue_pe"), g("roe_ronw")
    prom, ofs, fresh = g("promoter_holding_post_ipo"), g("ofs_pct"), g("fresh_issue_pct")
    rag_wr, rag_avg = s.get("rag_wr"), s.get("rag_avg")
    pos, neu, neg = [], [], []

    # positives
    if gmp is not None and gmp >= 3:
        pos.append({"head": f"Grey market premium +{gmp:.1f}%",
            "more": "The grey market is unofficial trading before listing. A positive premium means buyers are paying above the offer price ahead of time, a sign of demand."})
    if qib is not None and qib >= 10:
        pos.append({"head": f"Institutions piled in: QIB {qib:.0f}x",
            "more": "Qualified institutional buyers (large funds) bid this many times their quota. They do deep analysis, so heavy QIB interest is usually a positive signal."})
    if sub is not None and sub >= 5:
        pos.append({"head": f"Oversubscribed {sub:.0f}x overall",
            "more": "Total demand was several times the shares on offer, which often supports a stronger listing."})
    if hni is not None and hni >= 10:
        pos.append({"head": f"Wealthy investors keen: HNI {hni:.0f}x",
            "more": "High-net-worth individuals often apply with borrowed money, so they avoid IPOs they do not expect to pop. Strong HNI demand is a positive."})
    if roe is not None and roe >= 15:
        pos.append({"head": f"Strong returns: ROE {roe:.1f}%",
            "more": "Return on equity shows how efficiently the company turns shareholder money into profit. Above roughly 15% is healthy."})
    if pe is not None and 0 < pe <= 25:
        pos.append({"head": f"Reasonable price: P/E {pe:.1f}",
            "more": "A lower price-to-earnings ratio means you pay less per rupee of profit, leaving more room to rise."})
    if prom is not None and prom >= 50:
        pos.append({"head": f"Founders keep {prom:.0f}%",
            "more": "A high promoter holding after the IPO means founders keep significant skin in the game, aligning them with shareholders."})
    if fresh is not None and fresh >= 60:
        pos.append({"head": f"Mostly fresh capital ({fresh:.0f}% new money)",
            "more": "Most of the money raised goes into the company (growth, repaying debt) rather than founders cashing out."})
    if rag_wr is not None and rag_wr >= 0.65:
        w5 = round(rag_wr*5)
        extra = f", averaging {rag_avg:+.1f}% on day one" if rag_avg is not None else ""
        pos.append({"head": f"Similar past IPOs: {w5} of 5 listed positive",
            "more": f"Of the 5 closest historical matches (by GMP, subscription, size and fundamentals), {w5} listed above the offer price{extra}."})

    # negatives: scoring conflicts first, then data flags
    for c in s.get("conflicts", []):
        neg.append({"head": c, "more": ""})
    if gmp is not None and gmp < 0:
        neg.append({"head": f"Grey market negative {gmp:.1f}%",
            "more": "Buyers are paying below the offer price in unofficial trading, a sign of weak demand."})
    if pe is not None and pe < 0:
        neg.append({"head": "Loss-making (negative P/E)",
            "more": "The company is not yet profitable, which adds risk to the valuation."})
    elif pe is not None and pe > 40:
        neg.append({"head": f"Expensive: P/E {pe:.1f}",
            "more": "A high price-to-earnings ratio means you pay a lot per rupee of profit, leaving less room to rise."})
    if ofs is not None and ofs >= 80:
        neg.append({"head": f"Mostly promoters cashing out ({ofs:.0f}% OFS)",
            "more": "Most of the issue is an offer-for-sale, so the money goes to selling shareholders, not into the business."})
    if sub is not None and 0 < sub < 1:
        neg.append({"head": f"Undersubscribed: {sub:.2f}x",
            "more": "Total demand was below the shares on offer, a weak sign."})
    if roe is not None and 0 <= roe < 5:
        neg.append({"head": f"Weak returns: ROE {roe:.1f}%",
            "more": "Low return on equity suggests the company is not very efficient at turning capital into profit."})
    if rag_wr is not None and rag_wr < 0.5:
        w5 = round(rag_wr*5)
        cnt = "none of the 5" if w5 == 0 else f"only {w5} of 5"
        avgtxt = f" They averaged {rag_avg:+.1f}% on day one." if rag_avg is not None else ""
        neg.append({"head": f"Similar past IPOs: {w5} of 5 listed positive",
            "more": f"Of the 5 closest historical matches (by GMP, subscription, size and fundamentals), {cnt} listed above the offer price.{avgtxt}"})

    # neutral
    if pe is not None and 25 < pe <= 40:
        neu.append({"head": f"Middling valuation: P/E {pe:.1f}",
            "more": "Not cheap, not extreme. Valuation sits in a neutral zone."})
    if prom is not None and 30 <= prom < 50:
        neu.append({"head": f"Moderate founder stake: {prom:.0f}%",
            "more": "Promoter holding is neither high nor low after the issue."})

    # preliminary warning when subscription is not out yet
    note = None
    if (qib is None and sub is None) or (qib in (0, None) and sub in (0, None)):
        note = ("Preliminary call. Subscription data (QIB, HNI, retail) is not out yet "
                "- it appears once bidding opens, usually the last 1-3 days before the "
                "closing date. This call is based on grey-market premium and fundamentals "
                "only, and will sharpen as subscription data comes in.")
    return pos, neu, neg, note

SCRAPER_XLSX = os.environ.get("SCRAPER_XLSX", "active_ipos_v13.xlsx")
DATASET      = os.environ.get("IPO_DATASET", "GMP_ML_READY_FINAL_v3.xlsx")
CALIB        = os.environ.get("CALIBRATION", "alert_calibration.json")
OUT_JSON     = "qualified_ipos.json"

MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
          "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}

def parse_date(s):
    if not s or str(s) == "nan":
        return None
    s = re.sub(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s*', '', str(s)).strip().rstrip("T")
    m = re.search(r'(\d+)\s+([A-Za-z]+),?\s*(\d{4})', s)
    if m:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
        except Exception:
            return None
    return None

def num(v, default=None):
    try:
        if v is None or str(v) == "nan":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default

def has_live_data(r):
    return (num(r.get("gmp_closing_gain_pct"), 0) != 0) or (num(r.get("Total"), 0) > 0)

# Our models are trained on MAINBOARD IPOs only. SME IPOs (NSE Emerge / BSE SME)
# behave very differently, so we must never score or show them with this model.
# The scraper already targets the mainboard list, but this is a defensive net.
SME_HINTS = ("sme", "emerge", "bse sme", "nse sme")
# Issue-size floor: mainboard issues are materially larger than SME ones. Set via
# env (MAINBOARD_MIN_CR) since SEBI thresholds shift; conservative default.
import os
MAINBOARD_MIN_CR = float(os.environ.get("MAINBOARD_MIN_CR", "50"))

def is_mainboard(r):
    """Return (ok, reason). ok=False means treat as SME / non-mainboard and drop."""
    # 1) explicit signal from the scraper, if present
    board = str(r.get("board") or r.get("ipo_type") or r.get("category") or r.get("exchange") or "").lower()
    if board:
        if any(h in board for h in SME_HINTS):
            return False, f"board='{board}'"
        if "main" in board:
            return True, "board=mainboard"
    # 2) name/text hints (some sources tag 'XYZ SME IPO')
    blob = " ".join(str(r.get(k, "")) for k in ("IPO_Name", "name", "notes")).lower()
    if any(h in blob for h in SME_HINTS):
        return False, "name mentions SME"
    # 3) size floor (only when we actually have a size; never drop on missing data)
    size = num(r.get("Issue_Size(crores)"))
    if size is not None and size < MAINBOARD_MIN_CR:
        return False, f"issue size {size}cr < {MAINBOARD_MIN_CR}cr floor"
    return True, "ok"

def qualifying_tiers(scores, gmp, qib, sub, slider_levels):
    """Which accuracy tiers does this IPO clear?

    Each tier is defined by (model, score threshold, GMP/subscription floors).
    An IPO clears a tier if the score from that tier's model beats the threshold
    and the floors are met. QIB is intentionally NOT a floor: it still feeds the
    win score, but gating on it (with the nesting cutoff below) used to slam a
    strong, low-QIB IPO all the way down to tier 65. Walk-forward testing showed
    dropping the QIB floor leaves the whole ladder of picks/win-rates intact.

    `scores` is a dict of {model_name: win_probability} so each tier is judged by
    its OWN model (tiers can use different models). If only a single score is
    available, pass {"_default": score} and it is used for every tier.

    Tiers are nested: a higher tier is a stricter bar, so we count the contiguous
    run from the lowest tier upward and stop at the first tier the IPO fails. That
    keeps the ladder monotonic and avoids "qualifies at 86 but not 80" holes.
    """
    def score_for(model):
        if isinstance(scores, dict):
            return scores.get(model, scores.get("_default", next(iter(scores.values()), 0)))
        return scores  # a bare number

    out = []
    for k in sorted(slider_levels, key=int):
        lvl = slider_levels[k]
        if not lvl:
            continue
        ws = score_for(lvl["model"])
        # QIB deliberately omitted from the gate (see docstring). The ladder is
        # set by the score threshold plus GMP/subscription floors.
        clears = (ws >= lvl["threshold"]
                  and gmp >= lvl.get("gmp_min", 0)
                  and sub >= lvl.get("sub_min", 0))
        if not clears:
            break  # nesting: once a tier fails, no higher tier counts
        out.append({"tier": int(k), "expected_wr": lvl["avg_wr"],
                    "model": lvl["model"], "score": round(float(ws), 3)})
    return out


def main():
    try:
        import pandas as pd, numpy as np
    except ImportError:
        print("pandas/numpy not installed. Use the environment that runs the scraper.")
        sys.exit(1)

    if not os.path.exists(SCRAPER_XLSX):
        print(f"Cannot find {SCRAPER_XLSX}. Run the scraper first, or set SCRAPER_XLSX.")
        sys.exit(1)

    slider = {}
    if os.path.exists(CALIB):
        slider = json.load(open(CALIB)).get("slider_levels", {})

    df = pd.read_excel(SCRAPER_XLSX)
    rows = df.where(df.notna(), None).to_dict("records")
    print(f"Read {len(rows)} IPOs from {SCRAPER_XLSX}")

    # Optional scorer. Self-contained import attempt; if the ensemble or dataset
    # is missing we still publish upcoming IPOs (they need no scoring).
    score_fn = build = None
    if os.path.exists(DATASET):
        try:
            from ipo_scoring import build_models, score_ipo   # optional helper module
            build, score_fn = build_models, score_ipo
        except Exception:
            build = score_fn = None

    models = None
    if build and os.path.exists(DATASET):
        try:
            models = build(DATASET)
            print("ML models loaded, scorable IPOs will get a full call.")
        except Exception as e:
            print(f"Could not train models ({type(e).__name__}); upcoming IPOs only.")

    out, today = [], date.today()
    skipped_sme = []

    # OPTION B, step 1: LOCK FIRST
    # Before we score or grade anything this run, permanently lock the pending
    # prediction of any IPO that is no longer open (closed or already listed).
    # This freezes the clean final open-day call before new data can touch it.
    closed_now = set()
    for r in rows:
        nm = r.get("IPO_Name")
        if not nm:
            continue
        cd = parse_date(r.get("Close_Date"))
        has_listed = (r.get("_status") == "listed") or (num(r.get("Listing Gain")) is not None)
        if has_listed or (cd and cd < today):
            closed_now.add(_norm_name(nm))
    lock_if_closed(closed_now)
    for r in rows:
        name = r.get("IPO_Name")
        if not name:
            continue
        close_d = parse_date(r.get("Close_Date"))
        if close_d and (today - close_d).days > 14:
            continue   # old, archive handles it

        ok, reason = is_mainboard(r)
        if not ok:
            skipped_sme.append((str(name), reason))
            continue   # SME / non-mainboard: model is not trained for these

        # already listed? the scraper captured the real listing gain. Score it
        # from its CLOSING-DAY data (same archive methodology as the embedded
        # history) so it shows a full PRED vs ACTUAL + HIT/MISS like every other
        # graded call. A frozen locked call, when present, takes precedence.
        listing_gain = num(r.get("Listing Gain"))
        if r.get("_status") == "listed" or listing_gain is not None:
            settled = {
                "ipo_name": str(name).replace(" IPO", "").strip(),
                "close_date": r.get("Close_Date"),
                "listing_date": r.get("_listing_date") or r.get("Listing Date"),
                "listed_on": r.get("_listing_date") or r.get("Listing Date"),
                "offer_price": num(r.get("Offer Price")),
                "list_price": num(r.get("List Price")),
                "size_cr": num(r.get("Issue_Size(crores)")),
                "symbol": r.get("_symbol"),
                "sector": r.get("sector"),
                "actual": {"pct": listing_gain, "price": num(r.get("List Price")),
                           "hit": None} if listing_gain is not None else None,
                "listing_gain": listing_gain,
                "state": "settled",
                "qualifying_tiers": [],
                # A settled IPO kept no inputs, so its card rendered bare: no
                # "case for/against", no numbers, no peer comparison - even though
                # the closing-day data that produced the call is right here in `r`.
                # The story of a finished IPO ("here is what we saw, here is what
                # happened") is the most useful card on the site, so carry the same
                # inputs block the open path builds.
                "inputs": {
                    "offer_price": num(r.get("Offer Price")),
                    "size_cr": num(r.get("Issue_Size(crores)")),
                    "pe": num(r.get("pre_issue_pe")),
                    "roe": num(r.get("roe_ronw")),
                    "promoter_holding": num(r.get("promoter_holding_post_ipo")),
                    "ofs_pct": num(r.get("ofs_pct")),
                    "fresh_pct": num(r.get("fresh_issue_pct")),
                    "sector": r.get("sector"),
                    "gmp": num(r.get("gmp_closing_gain_pct")),
                    "qib": num(r.get("QIB")),
                    "hni": num(r.get("HNI")) if num(r.get("HNI")) is not None else num(r.get("NII")),
                    "rii": num(r.get("RII")) if num(r.get("RII")) is not None else num(r.get("Retail")),
                    "sub": num(r.get("Total")) if num(r.get("Total")) is not None else num(r.get("Overall")),
                    "market_sentiment": num(r.get("market_sentiment_ratio")),
                    "volatility": num(r.get("nifty_volatility_30d")),
                    "trend": num(r.get("trend_score")),
                },
            }
            # retro-score from closing-day inputs (GMP, subscription, fundamentals)
            if models is not None and listing_gain is not None:
                try:
                    s = score_fn(r, models)
                    lo, hi = s["signal2_lo"], s["signal2_hi"]
                    verdict = "APPLY" if s["signal1_apply"] else "SKIP"
                    wprobs = s.get("signal1_win_probs") or {"_default": s["signal1_win_score"]}
                    tiers = qualifying_tiers(wprobs, num(r.get("gmp_closing_gain_pct"), 0),
                                             num(r.get("QIB"), 0), 999, slider)
                    settled["qualifying_tiers"] = tiers
                    settled["prediction"] = {
                        "verdict": verdict,
                        "confidence": s["confidence"],
                        "range_low": lo, "range_high": hi,
                        "median": s["signal2_mid"],
                        "big_win_prob": int(s["signal3_big_prob"] * 100),
                        "allotment_pct": s["signal4_allot_pct"],
                    }
                    # the retro-score gives us everything build_cases needs, so a
                    # settled card can explain itself exactly like a live one
                    _pos, _neu, _neg, _note = build_cases(r, s)
                    settled["cases"] = {"pos": _pos, "neu": _neu, "neg": _neg}
                    settled["note"] = _note
                    # HIT = the call was right (APPLY->gain / SKIP->no gain)
                    hit = (listing_gain > 0) if verdict == "APPLY" else (listing_gain <= 0)
                    settled["actual"]["hit"] = bool(hit)
                    settled["retro"] = True   # scored from closing-day data, not a live lock
                except Exception as e:
                    print(f"  [settled] could not retro-score {name}: {type(e).__name__}")
            grade_frozen(settled)   # attach the locked closing-day call + HIT/MISS, if we have one
            out.append(settled)
            continue

        base = {
            "ipo_name": str(name).replace(" IPO", "").strip(),
            "close_date": r.get("Close_Date"),
            "_closing_today": bool(close_d and close_d == today),
            "_closing_tomorrow": bool(close_d and close_d == today + timedelta(days=1)),
            "listing_date": r.get("Listing Date") or r.get("_listing_date"),
            "listed_on": None,
            "offer_price": num(r.get("Offer Price")),
            "size_cr": num(r.get("Issue_Size(crores)")),
            "inputs": {
                "offer_price": num(r.get("Offer Price")),
                "size_cr": num(r.get("Issue_Size(crores)")),
                "pe": num(r.get("pre_issue_pe")),
                "roe": num(r.get("roe_ronw")),
                "promoter_holding": num(r.get("promoter_holding_post_ipo")),
                "ofs_pct": num(r.get("ofs_pct")),
                "fresh_pct": num(r.get("fresh_issue_pct")),
                "sector": r.get("sector"),
                "gmp": num(r.get("gmp_closing_gain_pct")),
                "qib": num(r.get("QIB")),
                "hni": num(r.get("HNI")) if num(r.get("HNI")) is not None else num(r.get("NII")),
                "rii": num(r.get("RII")) if num(r.get("RII")) is not None else num(r.get("Retail")),
                "sub": num(r.get("Total")) if num(r.get("Total")) is not None else num(r.get("Overall")),
                "market_sentiment": num(r.get("market_sentiment_ratio")),
                "volatility": num(r.get("nifty_volatility_30d")),
                "trend": num(r.get("trend_score")),
            },
        }

        if has_live_data(r) and models and score_fn:
            try:
                s = score_fn(r, models)
                ws = s["signal1_win_score"]
                # per-tier model scores (subscription-free); each tier is judged by
                # the model the calibration chose for it. Fall back to the single
                # win_score if the per-model probs are unavailable.
                wprobs = s.get("signal1_win_probs") or {"_default": ws}
                gmp_v = num(r.get("gmp_closing_gain_pct"), 0)
                sub_v = num(r.get("Total"))   # None if the scraper has not captured it yet
                # Subscription no longer gates the tier at all (the win-models are
                # subscription-free). We still mark a tier PRELIMINARY while the
                # subscription number is missing or under 1x, since the grey-market
                # premium can still move on the 30-min refresh.
                sub_known_ok = (sub_v is not None and sub_v >= 1)
                tiers = qualifying_tiers(wprobs, gmp_v, num(r.get("QIB"), 0), 999, slider)
                tier_preliminary = not sub_known_ok
                pos, neu, neg, note = build_cases(r, s)
                base.update({
                    "win_score": ws,
                    "prediction": {
                        "verdict": "APPLY" if s["signal1_apply"] else "SKIP",
                        "confidence": s["confidence"],
                        "range_low": s["signal2_lo"], "range_high": s["signal2_hi"],
                        "median": s["signal2_mid"],
                        "big_win_prob": int(s["signal3_big_prob"] * 100),
                        "allotment_pct": s["signal4_allot_pct"],
                    },
                    "cases": {"pos": pos, "neu": neu, "neg": neg},
                    "note": note,
                    "preliminary": (note is not None) or tier_preliminary,
                    "tier_preliminary": tier_preliminary,
                    "qualifying_tiers": tiers,
                    "state": "open",
                })
            except Exception as e:
                base.update({"state": "upcoming", "note": f"scoring error: {type(e).__name__}",
                             "qualifying_tiers": []})
        else:
            base.update({
                "state": "upcoming",
                "qualifying_tiers": [],
                "opens_note": "Opening soon. A call appears once grey-market and subscription data is available.",
            })
        out.append(base)

    # refresh the pending prediction for any still-open IPO (keeps it current;
    # it gets permanently locked on the first run after the IPO closes)
    for e in out:
        freeze_prediction(e)

    if COMPLIANCE_MODE:
        out = [_compliance_sanitize(e) for e in out]

    json.dump(_json_safe(out), open(OUT_JSON, "w"), indent=2, allow_nan=False, default=str)
    n_open = sum(1 for e in out if e.get("state") == "open")
    mode = "COMPLIANCE (facts only)" if COMPLIANCE_MODE else "full"
    print(f"\nWrote {OUT_JSON} [{mode}]: {len(out)} IPOs ({n_open} open, {len(out)-n_open} upcoming)")
    if skipped_sme:
        print(f"\nSkipped {len(skipped_sme)} non-mainboard / SME IPO(s) (model is mainboard-only):")
        for nm, why in skipped_sme:
            print(f"  [skip] {nm}  ({why})")
    for e in out:
        if e.get("state") == "open" and not COMPLIANCE_MODE:
            p = e["prediction"]
            print(f"  [scored]   {e['ipo_name']}: {p['verdict']} conf {p['confidence']}, "
                  f"{len(e['qualifying_tiers'])} tiers")
        else:
            print(f"  [{e.get('state','?')}] {e['ipo_name']}")


if __name__ == "__main__":
    main()
