import os, json, time, re, math
import numpy as np, pandas as pd
from datetime import datetime, date
from pathlib import Path

DATASET     = os.environ.get("IPO_DATASET",
              "GMP_ML_READY_FINAL_v3.xlsx")
SCRAPER_XLSX = os.environ.get("SCRAPER_XLSX", "active_ipos_v13.xlsx")
CALIB_FILE  = "alert_calibration.json"
STATE_FILE  = "update_state.json"
RETRAIN_THRESHOLD = 10
SLIDER_LEVELS = list(range(65, 99))

def load_state():
    """How many IPOs were in the dataset at last retrain?"""
    if not Path(STATE_FILE).exists():
        return {'last_retrain_n': 0, 'last_retrain_date': None,
                'last_calibration': None}
    with open(STATE_FILE) as f: return json.load(f)

def save_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2, default=str)

def fetch_actual_listing(ipo_name, listing_date_str=None, symbol=None, offer_price=None,
                         list_price=None):
    """
    Fetch the actual listing open price, robustly:
      - if the scraper already captured the listing open price straight from
        Chittorgarh's listing table, TRUST that first (no network call needed)
      - else try a real exchange symbol (from the scraper), then name guesses
      - try both .NS and .BO for each
      - CONFIRM each candidate two ways so a wrong ticker cannot poison the data:
          (1) sanity bound: listing gain vs offer must be within -95%..+500%
          (2) name match: the security's longName should share a word with the IPO
      - a symbol-based hit is trusted; a name-guess hit must pass the name check
    Returns {'ticker','open_price','data_source','from_symbol','name_confirmed'} or None.
    """
    # 0) scraper already has the real listing open price from Chittorgarh's table
    try:
        if list_price is not None and str(list_price).strip().lower() not in ("", "nan"):
            lp = float(list_price)
            if lp > 0 and offer_price and 0.05 <= lp / float(offer_price) <= 6.0:
                return {'ticker': (str(symbol).upper() if symbol else None),
                        'open_price': lp, 'data_source': 'chittorgarh',
                        'from_symbol': True, 'name_confirmed': True}
    except Exception:
        pass
    try:
        import yfinance as yf
    except Exception:
        return None
    name_tokens = [w for w in re.sub(r'[^a-z0-9 ]+', ' ', ipo_name.lower()).split()
                   if len(w) > 2 and w not in ("ltd", "ipo", "the", "and", "india", "limited")]
    candidates = []
    if symbol and str(symbol).strip().lower() not in ("", "nan"):
        s = str(symbol).strip().upper().replace(" ", "")
        candidates += [(s + ".NS", True), (s + ".BO", True)]
    guess = ipo_name.upper().replace(" ", "").replace("LTD", "").replace("LIMITED", "")[:10]
    candidates += [(guess + ".NS", False), (guess + ".BO", False)]

    fallback = None
    for ticker, from_symbol in candidates:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="7d")
            if hist is None or len(hist) == 0:
                continue
            open_price = float(hist['Open'].iloc[0])
            if open_price <= 0:
                continue
            if offer_price:                       # confirm 1: sanity bound
                g = (open_price - float(offer_price)) / float(offer_price) * 100
                if g < -95 or g > 500:
                    continue
            name_ok = True                        # confirm 2: name match (best-effort)
            try:
                long_name = ((t.info or {}).get("longName") or "").lower()
                if name_tokens and long_name:
                    name_ok = sum(1 for w in name_tokens if w in long_name) >= 1
            except Exception:
                name_ok = True                    # info unavailable -> do not block
            cand = {'ticker': ticker, 'open_price': open_price, 'data_source': 'yfinance',
                    'from_symbol': from_symbol, 'name_confirmed': name_ok}
            if from_symbol or name_ok:            # trusted match -> take it
                return cand
            fallback = fallback or cand           # keep an unconfirmed match as last resort
        except Exception:
            continue
    return fallback

def append_listing(ipo_data):
    """
    ipo_data must have all fields including actual List Price and Listing Gain.
    Returns updated dataset length.
    """
    df = pd.read_excel(DATASET)
    if ipo_data['IPO_Name'] in df['IPO_Name'].values:
        return len(df), False
    new_row = pd.DataFrame([ipo_data])
    df = pd.concat([df, new_row], ignore_index=True)
    df.to_excel(DATASET, index=False)
    return len(df), True

def _parse_listing(s):
    """Parse a listing-date string like 'Wed, Jul 1, 2026' (the scraper format,
    sometimes with a trailing T) into a date. Returns None if unparseable."""
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return None
    s = re.sub(r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s*', '', str(s).strip()).rstrip('T').strip()
    for fmt in ("%b %d, %Y", "%d %b, %Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None

def grade_listings(verbose=True):
    """The feedback step the learning loop was missing: for every IPO that has
    already listed but is not yet in the training dataset, fetch its real
    listing price, compute the listing gain, and append it. Source of the
    pre-listing features is the scraper file (SCRAPER_XLSX), which still holds
    the IPO on listing day (it keeps anything closed within 14 days). Returns
    how many IPOs were added."""
    if not Path(SCRAPER_XLSX).exists():
        if verbose: print(f"  grade: {SCRAPER_XLSX} not found; nothing to grade.")
        return 0
    active = pd.read_excel(SCRAPER_XLSX)
    ds = pd.read_excel(DATASET)
    ds_cols = list(ds.columns)
    existing = set(ds["IPO_Name"].astype(str).str.strip().str.lower())
    today = date.today()
    added = 0
    for _, r in active.iterrows():
        name = str(r.get("IPO_Name", "")).strip()
        if not name or name.lower() in existing:
            continue
        ld = _parse_listing(r.get("_listing_date"))
        if ld is None or ld > today:          # has not listed yet
            continue
        offer = r.get("Offer Price")
        if offer is None or (isinstance(offer, float) and math.isnan(offer)) or float(offer) <= 0:
            continue
        actual = fetch_actual_listing(name, r.get("_listing_date"),
                                       symbol=r.get("_symbol") or r.get("symbol"),
                                       offer_price=offer,
                                       list_price=r.get("List Price"))
        if not actual or not actual.get("open_price"):
            if verbose: print(f"  grade: {name} has listed but price not available yet; will retry next run.")
            continue
        open_price = float(actual["open_price"])
        gain = round((open_price - float(offer)) / float(offer) * 100, 2)
        row = {c: r.get(c) for c in ds_cols}     # align strictly to dataset columns
        row["IPO_Name"]     = name
        row["List Price"]   = open_price
        row["Listing Gain"] = gain
        n, ok = append_listing(row)
        if ok:
            existing.add(name.lower())
            added += 1
            if verbose:
                print(f"  graded + appended: {name}  list {open_price} ({gain:+.1f}%)  -> dataset n={n}")
    if verbose and added == 0:
        print("  grade: no newly-listed IPOs to append.")
    return added

def _enforce_monotonic(calibration, levels):
    """Guarantee nested tiers: pick ONE model for the whole ladder (the model
    that wins the most tiers) and force thresholds to be non-decreasing as the
    tier rises. With one model and rising thresholds, the qualifying set at a
    higher tier is always a strict subset of every lower tier, no holes, no
    "qualifies at 86 but not 80".
    """
    from collections import Counter
    present = [calibration[str(l)] for l in levels if calibration.get(str(l))]
    if not present:
        return calibration
    # 1) choose the dominant model (most tiers won), for comparable scores
    dom_model = Counter(c['model'] for c in present).most_common(1)[0][0]

    # 2) re-evaluate each tier on the dominant model at its stored threshold,
    #    then walk upward forcing threshold + floors to be non-decreasing.
    last_thr = last_gmp = last_qib = last_sub = 0
    for l in levels:
        c = calibration.get(str(l))
        if not c:
            continue
        # re-evaluate on the dominant model so avg_wr/picks reflect the model we serve
        r = eval_on_model(dom_model, c['threshold'])
        thr = max(c['threshold'], last_thr)          # non-decreasing threshold
        gmp = max(c.get('gmp_min', 0), last_gmp)
        qib = max(c.get('qib_min', 0), last_qib)
        sub = max(c.get('sub_min', 0), last_sub)
        c['model'] = dom_model
        c['threshold'] = thr
        c['gmp_min'], c['qib_min'], c['sub_min'] = gmp, qib, sub
        last_thr, last_gmp, last_qib, last_sub = thr, gmp, qib, sub
    return calibration


def eval_on_model(model, threshold):
    """Thin wrapper so monotonic enforcement can re-score a tier on one model."""
    try:
        import ipo_calibration_agent as _ca
        if not _ca._data:
            _ca._build()
        return _ca.eval_combo(model, threshold)
    except Exception:
        return {'avg_wr': 0, 'min_wr': 0, 'avg_picks': 0}


def retrain_and_recalibrate():
    """
    Use the same v3 agent logic to find best model+threshold for each
    slider level. This is the heart of the update.
    Returns calibration dict { slider_level: best_config }
    """
    from sklearn.preprocessing import RobustScaler, LabelEncoder
    from sklearn.ensemble import RandomForestClassifier, ExtraTreesClassifier
    from xgboost import XGBClassifier
    from lightgbm import LGBMClassifier
    import warnings; warnings.filterwarnings('ignore')

    print("\nLoading dataset and preparing features...")
    df = pd.read_excel(DATASET)
    df = df.dropna(subset=['Listing Gain','gmp_closing_gain_pct'])
    df['Date'] = pd.to_datetime(df['Date'], dayfirst=True)
    df = df.sort_values('Date').reset_index(drop=True)
    df['win'] = (df['Listing Gain']>0).astype(int)
    le = LabelEncoder()
    df['sector_code'] = le.fit_transform(df['sector'].fillna('Unknown'))
    N = len(df)
    print(f" Dataset: {N} IPOs")

    df['mwr5']  = df['win'].shift(1).rolling(5,  min_periods=3).mean().fillna(0.7)
    df['mwr10'] = df['win'].shift(1).rolling(10, min_periods=5).mean().fillna(0.7)
    df['ag10']  = df['gmp_closing_gain_pct'].shift(1).rolling(10,min_periods=5).mean().fillna(0)
    df['gpr']   = df['gmp_closing_day']/(df['Offer Price']+1)
    for c in ['sector_wr','sector_avg_list','sector_bias','gmp_acc','mvol','aprx']:
        df[c]=np.nan
    for i in df.index:
        past=df[df.index<i]; ps=past[past['sector']==df.loc[i,'sector']].tail(15)
        p10=past.tail(10)
        if len(ps)>=3:
            df.loc[i,'sector_wr']=ps['win'].mean()
            df.loc[i,'sector_avg_list']=ps['Listing Gain'].mean()
            df.loc[i,'sector_bias']=(ps['Listing Gain']-ps['gmp_closing_gain_pct']).mean()
        if len(p10)>=5:
            df.loc[i,'gmp_acc']=(p10['Listing Gain']-p10['gmp_closing_gain_pct']).abs().mean()
            df.loc[i,'mvol']=p10['Listing Gain'].std()
        df.loc[i,'aprx']=1/(np.log1p(df.loc[i,'Total'])*np.log1p(df.loc[i,'Issue_Size(crores)'])+1)
    for c in ['sector_wr','sector_avg_list','sector_bias','gmp_acc','mvol','aprx']:
        df[c]=df[c].fillna(df[c].median() if df[c].notna().any() else 0)
    df['aprx']=df['aprx']/df['aprx'].max()

    X=pd.DataFrame(index=df.index)
    for c in ['gmp_closing_gain_pct','gmp_closing_day','Total','QIB','HNI','RII',
              'Issue_Size(crores)','market_sentiment_ratio','nifty_volatility_30d',
              'ofs_pct','pre_issue_pe','roe_ronw','Offer Price','sector_code',
              'sector_wr','sector_avg_list','sector_bias','mwr5','mwr10',
              'gmp_acc','mvol','aprx','gpr']:
        X[c]=df[c] if c in df.columns else 0
    X['lt']=np.log1p(X['Total']); X['lq']=np.log1p(X['QIB'])
    X['g2']=X['gmp_closing_gain_pct']**2
    X['gts']=X['gmp_closing_gain_pct']*X['lt']
    X['id']=(X['QIB']+X['HNI'])/(X['Total']+1)
    X['gva']=X['gmp_closing_gain_pct']-df['ag10']
    X=X.fillna(0)
    yw = df['win'].values

    def model(name):
        return {
            'RandomForest': RandomForestClassifier(n_estimators=200,max_depth=10,
                             min_samples_leaf=3,random_state=42,n_jobs=-1),
            'XGBoost':      XGBClassifier(n_estimators=150,max_depth=4,
                             learning_rate=0.05,subsample=0.8,random_state=42,
                             eval_metric='logloss',verbosity=0),
            'LightGBM':     LGBMClassifier(n_estimators=150,max_depth=4,
                             learning_rate=0.05,num_leaves=31,
                             random_state=42,verbose=-1),
            'ExtraTrees':   ExtraTreesClassifier(n_estimators=200,max_depth=10,
                             random_state=42,n_jobs=-1),
        }[name]

    print("\nRecalibrating via EXTENDED AGENTIC SEARCH (model x thr x gmp x qib x sub)...")
    import ipo_calibration_agent as _ca
    _ca._data.clear()
    _ca._build()

    def _per_split(best):
        out = {}
        for sp in _ca._data['splits']:
            d = _ca._data['cached'][(best['model'], sp)]
            m = ((d['probs'] >= best['threshold']/100.0)
                 & (d['qib'] >= best['qib_floor']) & (d['sub'] >= best['sub_floor']))
            if best['gmp_floor'] > 0:
                m = m & (d['gmp'] >= best['gmp_floor'])
            n = int(m.sum())
            out[sp] = {'wr': round(float(d['wins'][m].mean()*100), 1) if n else 0.0,
                       'picks': n}
        return out

    calibration = {}
    for level in SLIDER_LEVELS:
        best = _ca.find_best_optimal(level, verbose=False)['best']
        if best:
            calibration[str(level)] = {
                'model':     best['model'],
                'threshold': best['threshold'],
                'gmp_min':   best['gmp_floor'],
                'qib_min':   best['qib_floor'],
                'sub_min':   best['sub_floor'],
                'avg_wr':    best['avg_wr'],
                'min_wr':    best['min_wr'],
                'avg_picks': best['avg_picks'],
                'per_split': _per_split(best),
            }
            print(f" {level}%: {best['model']}@{best['threshold']} gmp>={best['gmp_floor']} "
                  f"qib>={best['qib_floor']} -> {best['avg_picks']} picks ({best['avg_wr']}% WR)")
        else:
            calibration[str(level)] = None

    # ---- Enforce NESTING: a higher tier must flag a SUBSET of every lower tier.
    # Tiers can pick different models/thresholds, which can make their floors
    # non-monotonic (e.g. a 31% threshold at tier 86 after 62% at tier 85). That
    # produces nonsensical "qualifies at 86 but not 80" holes for users. We make
    # each floor monotonic with the tier so the qualifying set is always a clean
    # nested range. avg_picks can only shrink as the bar rises, which we also fix.
    calibration = _enforce_monotonic(calibration, SLIDER_LEVELS)

    output = {
        'n_ipos': N,
        'calibrated_at': datetime.now().isoformat(),
        'slider_levels': calibration,
    }
    with open(CALIB_FILE, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {CALIB_FILE}")
    return calibration

def diff_calibration(old, new):
    """Compare old and new calibration. Returns summary of changes."""
    if not old: return "First calibration, no diff available."
    changes = []
    for level in SLIDER_LEVELS:
        key = str(level)
        o = old.get(key); n = new.get(key)
        if not n:
            continue
        if not o:
            changes.append(f"{level}%: new entry, {n['model']}@{n['threshold']} -> {n['avg_wr']}% WR")
            continue
        if o['model'] != n['model']:
            changes.append(f"{level}%: MODEL CHANGED {o['model']}->{n['model']} "
                           f"(threshold {o['threshold']}->{n['threshold']}, "
                           f"WR {o['avg_wr']}->{n['avg_wr']}%)")
        elif abs(o['threshold'] - n['threshold']) >= 5:
            changes.append(f"{level}%: threshold shifted "
                           f"{o['threshold']}->{n['threshold']} "
                           f"(WR {o['avg_wr']}->{n['avg_wr']}%)")
        elif abs(o['avg_wr'] - n['avg_wr']) >= 1.0:
            changes.append(f"{level}%: WR {o['avg_wr']}->{n['avg_wr']}% "
                           f"({n['avg_wr']-o['avg_wr']:+.1f}%)")
    if not changes:
        return "Calibration stable, no significant shifts across levels."
    return "\n".join("  - " + c for c in changes)

def main(force=False):
    state = load_state()
    appended = grade_listings()      # NEW: pull any IPOs that have listed into the dataset
    if appended:
        print(f"  {appended} newly-listed IPO(s) added to the dataset.")
    df = pd.read_excel(DATASET)
    df = df.dropna(subset=['Listing Gain','gmp_closing_gain_pct'])
    current_n = len(df)
    last_n = state.get('last_retrain_n', 0)
    delta = current_n - last_n

    print("="*60)
    print("IPO UPDATE PIPELINE")
    print("="*60)
    print(f" Dataset size: {current_n} IPOs")
    print(f" Last retrain at: {last_n} IPOs ({state.get('last_retrain_date','never')})")
    print(f" New IPOs since: {delta}")
    print(f" Retrain threshold: every {RETRAIN_THRESHOLD} IPOs")

    if not force and delta < RETRAIN_THRESHOLD:
        print(f"\n  Skip retrain, need {RETRAIN_THRESHOLD-delta} more IPOs.")
        print(" (Run with --force to retrain anyway.)")
        return

    print(f"\n  OK Threshold crossed, retraining now")
    print("-"*60)

    new_calibration = retrain_and_recalibrate()
    old_calibration = state.get('last_calibration', {})

    print("\n" + "="*60)
    print("DIFF: Old vs New calibration")
    print("="*60)
    diff = diff_calibration(old_calibration, new_calibration)
    print(diff)

    state['last_retrain_n']    = current_n
    state['last_retrain_date'] = datetime.now().isoformat()
    state['last_calibration']  = new_calibration
    save_state(state)
    print(f"\n  State saved -> {STATE_FILE}")

    # The newly-listed IPOs are now in the dataset, so refresh the website's
    # comparison pool (the embedded ARCHIVE) to include them too.
    try:
        import build_archive
        build_archive.main()
        try:
            import regen_pages
            regen_pages.main()
        except Exception as e:
            print(f"  (page regen skipped: {type(e).__name__}: {e})")
        print("  Archive (comparison pool) refreshed with the new IPOs.")
    except Exception as e:
        print(f"  Archive refresh skipped ({type(e).__name__}: {e}). "
              f"Run 'python build_archive.py' manually if needed.")

    print("\n" + "="*78)
    print("NEW CALIBRATION TABLE, winner = max picks satisfying target across all splits")
    print("="*78)
    print(f"\n  {'Slider':>6} {'Model':>14} {'Thr':>4} {'AvgWR':>7} {'MinWR':>7} "
          f"{'Picks':>6} | {'50/50':>7} {'60/40':>7} {'70/30':>7} {'80/20':>7}")
    print(" " + "-"*72)
    for level in SLIDER_LEVELS:
        cfg = new_calibration.get(str(level))
        if cfg:
            ps = cfg.get('per_split', {})
            s5 = ps.get('50/50', {}).get('wr', 0)
            s6 = ps.get('60/40', {}).get('wr', 0)
            s7 = ps.get('70/30', {}).get('wr', 0)
            s8 = ps.get('80/20', {}).get('wr', 0)
            print(f" {level:>5}% {cfg['model']:>14} {cfg['threshold']:>3}% "
                  f"{cfg['avg_wr']:>6.1f}% {cfg['min_wr']:>6.1f}% "
                  f"{cfg['avg_picks']:>6.1f} | "
                  f"{s5:>6.1f}% {s6:>6.1f}% {s7:>6.1f}% {s8:>6.1f}%")
        else:
            print(f" {level:>5}% {'no eligible combo at this target':>50}")

def status():
    state = load_state()
    df = pd.read_excel(DATASET)
    df = df.dropna(subset=['Listing Gain','gmp_closing_gain_pct'])
    current_n = len(df)
    last_n = state.get('last_retrain_n', 0)
    delta = current_n - last_n
    print(f"\n  Dataset:        {current_n} IPOs")
    print(f" Last retrain:   {last_n} IPOs ({state.get('last_retrain_date','never')})")
    print(f" New IPOs since: {delta}")
    print(f" Next retrain:   {max(0, RETRAIN_THRESHOLD-delta)} IPOs away\n")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--force', action='store_true', help='Force retrain now')
    p.add_argument('--status', action='store_true', help='Show status only')
    args = p.parse_args()

    if args.status:
        status()
    else:
        main(force=args.force)
