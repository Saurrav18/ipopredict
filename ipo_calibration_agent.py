import os, time, json, numpy as np, pandas as pd
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
import warnings; warnings.filterwarnings('ignore')

DATASET = os.environ.get("IPO_DATASET",
          "GMP_ML_READY_FINAL_v3.xlsx")

_data = {}

def _build():
    if _data: return
    import ipo_scoring as S
    from sklearn.preprocessing import RobustScaler
    # SAME prep + SAME subscription-free features + SAME 4 models the live app uses,
    # so a tier calibrated here scores identically when the app runs it.
    df = S.prep_frame(pd.read_excel(DATASET))
    N = len(df)
    Xw = S.make_win_features(df)
    yw = df['win'].values
    splits = {'50/50':0.5,'60/40':0.4,'70/30':0.3,'80/20':0.2}
    models = S.win_models()
    def col(name):
        return df[name].fillna(0).values if name in df.columns else np.zeros(N)
    cached = {}
    for sp, frac in splits.items():
        te = max(N-int(N*frac),80)
        tr = list(range(te-50)); ts = list(range(te,N))
        sc = RobustScaler(); Xtr = sc.fit_transform(Xw.iloc[tr].values)
        Xts = sc.transform(Xw.iloc[ts].values)
        for mname, m in models.items():
            m.fit(Xtr, yw[tr])
            cached[(mname, sp)] = {
                'probs': m.predict_proba(Xts)[:,1],
                'wins':  yw[ts].copy(),
                'gmp':   df.iloc[ts]['gmp_closing_gain_pct'].values,
                'qib':   col('QIB')[ts], 'sub': col('Total')[ts],
                'rii':   col('RII')[ts], 'hni': col('HNI')[ts],
            }
    _data['cached'] = cached
    _data['models'] = list(models.keys())
    _data['splits'] = list(splits.keys())
    _data['N'] = N

def eval_combo(model: str, threshold: int) -> Dict:
    """Returns avg_wr, min_wr, avg_picks across all 4 splits."""
    if not _data: _build()
    thr = threshold / 100.0
    per_split = {}
    wrs, picks = [], []
    for sp in _data['splits']:
        d = _data['cached'][(model, sp)]
        mask = d['probs'] >= thr
        n = int(mask.sum())
        wr = float(d['wins'][mask].mean()*100) if n>0 else 0.0
        per_split[sp] = {'wr': round(wr,1), 'picks': n}
        wrs.append(wr); picks.append(n)
    return {
        'model': model, 'threshold': threshold,
        'avg_wr':    round(float(np.mean(wrs)), 1),
        'min_wr':    round(float(min(wrs)), 1),
        'avg_picks': round(float(np.mean(picks)), 1),
        'per_split': per_split,
    }

@dataclass
class CalibState:
    target: int
    tested: List[Dict] = field(default_factory=list)
    best: Optional[Dict] = None
    model_attempts: Dict[str, int] = field(default_factory=dict)
    decisions: List[str] = field(default_factory=list)

    def log(self, msg): self.decisions.append(msg)

    def is_eligible(self, r):
        return (r['avg_wr'] >= self.target
                and r['min_wr'] >= self.target - 5
                and r['avg_picks'] >= 5)

def find_best(target: int, verbose: bool = True) -> Dict:
    """
    Agentic search for best (model, threshold) for given WR target.
    Replaces brute force with bisection + intelligent model selection.
    """
    if not _data: _build()
    state = CalibState(target=target)

    if verbose:
        print(f"\n  Target: {target}% WR, agent searching...")

    for model in _data['models']:
        state.log(f"Trying {model}")
        lo, hi = 10, 99
        best_for_model = None
        for _ in range(8):
            if lo > hi: break
            mid = (lo + hi) // 2
            r = eval_combo(model, mid)
            state.tested.append(r)
            state.model_attempts[model] = state.model_attempts.get(model, 0) + 1

            if state.is_eligible(r):
                if best_for_model is None or r['avg_picks'] > best_for_model['avg_picks']:
                    best_for_model = r
                hi = mid - 1
                state.log(f"  {model}@{mid} OK ({r['avg_wr']}% WR, {r['avg_picks']} picks) -> lower")
            else:
                lo = mid + 1
                state.log(f"  {model}@{mid} too weak ({r['avg_wr']}% WR) -> raise")

        if best_for_model:
            if state.best is None or best_for_model['avg_picks'] > state.best['avg_picks']:
                state.best = best_for_model
                state.log(f"  ★ Best so far: {model}@{best_for_model['threshold']} -> {best_for_model['avg_picks']} picks")

    if state.best:
        m = state.best['model']
        t0 = state.best['threshold']
        for delta in [-2, -1, 1, 2]:
            t = t0 + delta
            if 10 <= t <= 99 and not any(r['model']==m and r['threshold']==t for r in state.tested):
                r = eval_combo(m, t)
                state.tested.append(r)
                if state.is_eligible(r) and r['avg_picks'] > state.best['avg_picks']:
                    state.best = r
                    state.log(f"  Refinement: {m}@{t} better ({r['avg_picks']} picks)")

    if verbose:
        n_tested = len(state.tested)
        print(f" Agent tested {n_tested} combos (vs 4800 brute force) = {4800//max(n_tested,1)}x fewer")
        if state.best:
            b = state.best
            print(f" WINNER: {b['model']}@{b['threshold']}% -> "
                  f"avg {b['avg_wr']}% min {b['min_wr']}% picks {b['avg_picks']}")
        else:
            print(f" No eligible combo found")

    return {
        'target':      target,
        'best':        state.best,
        'n_tested':    len(state.tested),
        'decisions':   state.decisions,
    }

def brute_force(target: int) -> Dict:
    """Original brute force, for comparison only."""
    if not _data: _build()
    eligible = []
    for model in _data['models']:
        for thr in range(10, 100):
            r = eval_combo(model, thr)
            if (r['avg_wr']>=target and r['min_wr']>=target-5 and r['avg_picks']>=5):
                eligible.append(r)
    if not eligible: return None
    return max(eligible, key=lambda x: x['avg_picks'])

GMP_FLOORS = [0, 3, 5, 7, 10]
QIB_FLOORS = [0]   # not a tier floor (see docstring in qualifying_tiers)
SUB_FLOORS = [0]   # subscription is NOT used at all now: the win-models are
                   # subscription-free, so a half-built bidding number can never
                   # drag a tier down. The tier depends only on the model score
                   # (GMP + fundamentals + history) plus an optional GMP floor.

# Breathing room: a tier may sit up to BREATHE% under its label on average, so
# the agent can fill in more tiers (65,66,67,...) instead of leaving gaps. The
# worst single split is still held within BREATHE+ a small margin of the label.
BREATHE = 2.5

def eval_combo_ext(model: str, threshold: int, g: float = 0, q: float = 0, s: float = 0) -> Dict:
    """Evaluate (model, score thr, gmp floor, qib floor, sub floor) across all 4 splits."""
    if not _data: _build()
    thr = threshold / 100.0
    wrs, picks = [], []
    for sp in _data['splits']:
        d = _data['cached'][(model, sp)]
        mask = (d['probs'] >= thr) & (d['qib'] >= q) & (d['sub'] >= s)
        if g > 0:
            mask = mask & (d['gmp'] >= g)
        n = int(mask.sum())
        wr = float(d['wins'][mask].mean()*100) if n>0 else 0.0
        wrs.append(wr); picks.append(n)
    return {'model': model, 'threshold': threshold, 'gmp_floor': g, 'qib_floor': q, 'sub_floor': s,
            'avg_wr': round(float(np.mean(wrs)),1), 'min_wr': round(float(min(wrs)),1),
            'avg_picks': round(float(np.mean(picks)),1), 'min_picks': int(min(picks))}

def _eligible(r, target):
    return (r['avg_wr'] >= target - BREATHE and r['min_wr'] >= target - 5
            and r['avg_picks'] >= 5 and r['min_picks'] >= 5)

def find_best_extended(target: int, verbose: bool = True) -> Dict:
    """
    AGENTIC search over the extended space.
    Strategy per model: for each (gmp, qib) floor pair, BISECT the score
    threshold (max 8 evals) to find the lowest eligible threshold (= max picks).
    Then refine the global best: try sub floors and +-2 threshold neighbours.
    ~650 evals worst case vs 21,600 brute force.
    """
    if not _data: _build()
    tested = 0
    best = None

    for model in _data['models']:
        for g in GMP_FLOORS:
            for q in QIB_FLOORS:
                lo, hi = 10, 99
                best_local = None
                for _ in range(8):
                    if lo > hi: break
                    mid = (lo + hi) // 2
                    r = eval_combo_ext(model, mid, g, q, 0); tested += 1
                    if _eligible(r, target):
                        if best_local is None or r['avg_picks'] > best_local['avg_picks']:
                            best_local = r
                        hi = mid - 1
                    else:
                        lo = mid + 1
                if best_local and (best is None or best_local['avg_picks'] > best['avg_picks']):
                    best = best_local

    if best:
        m, t0, g0, q0 = best['model'], best['threshold'], best['gmp_floor'], best['qib_floor']
        for dt in [-2, -1, 1, 2]:
            t = t0 + dt
            if 10 <= t <= 99:
                r = eval_combo_ext(m, t, g0, q0, 0); tested += 1
                if _eligible(r, target) and r['avg_picks'] > best['avg_picks']:
                    best = r
        for s in SUB_FLOORS[1:]:
            for dt in [-2, -1, 0, 1, 2]:
                t = best['threshold'] + dt
                if 10 <= t <= 99:
                    r = eval_combo_ext(best['model'], t, best['gmp_floor'], best['qib_floor'], s); tested += 1
                    if _eligible(r, target) and r['avg_picks'] > best['avg_picks']:
                        best = r

    if verbose and best:
        print(f" target {target}%: {best['model']}@{best['threshold']} gmp>={best['gmp_floor']} "
              f"qib>={best['qib_floor']} sub>={best['sub_floor']} -> {best['avg_picks']} picks "
              f"({best['avg_wr']}% avg, {best['min_wr']}% min) [{tested} evals]")
    return {'target': target, 'best': best, 'n_tested': tested}

def brute_force_extended(target: int) -> Dict:
    """Exhaustive search over the full extended space (ground truth)."""
    if not _data: _build()
    best = None
    for model in _data['models']:
        for thr in range(10, 100):
            for g in GMP_FLOORS:
                for q in QIB_FLOORS:
                    for s in SUB_FLOORS:
                        r = eval_combo_ext(model, thr, g, q, s)
                        if _eligible(r, target):
                            if best is None or r['avg_picks'] > best['avg_picks']:
                                best = r
    return best

def find_best_optimal(target: int, verbose: bool = False) -> Dict:
    """Pick the max-picks combo for a tier. Uses EXHAUSTIVE search when the
    search space is small (guarantees the true optimum - binary search can
    overshoot on noisy win-rate curves and leave picks on the table). Falls
    back to the fast binary agent only when the space is huge (e.g. a dataset
    of thousands of IPOs), where brute force would be slow."""
    if not _data: _build()
    space = len(_data['models'])*90*len(GMP_FLOORS)*len(QIB_FLOORS)*len(SUB_FLOORS)
    if space <= 8000:
        b = brute_force_extended(target)
        if verbose and b:
            print(f" target {target}%: {b['model']}@{b['threshold']} gmp>={b['gmp_floor']} "
                  f"-> {b['avg_picks']} picks ({b['avg_wr']}% avg, {b['min_wr']}% min) [exhaustive {space}]")
        return {'target': target, 'best': b, 'n_tested': space, 'method': 'exhaustive'}
    r = find_best_extended(target, verbose)
    r['method'] = 'binary'
    return r

if __name__ == "__main__":
    import sys
    if len(sys.argv)>1 and sys.argv[1] == '--verify-extended':
        import time as _t
        print("="*100)
        print("EXTENDED SPACE VERIFICATION - agent vs brute force (21,600 combos), all tiers 65-98")
        print("="*100)
        _build()
        matches = 0; total = 0; t_a = t_b = 0; evals = []
        print(f"{'Tier':<6}{'AGENT combo':<42}{'picks':<8}{'| BRUTE combo':<42}{'picks':<8}{'Match'}")
        print("-"*116)
        for target in range(65, 99):
            total += 1
            t0=_t.time(); a = find_best_extended(target, verbose=False); t_a += _t.time()-t0
            evals.append(a['n_tested'])
            t0=_t.time(); b = brute_force_extended(target); t_b += _t.time()-t0
            ab = a['best']
            ok = (ab is None and b is None) or (ab and b and ab['avg_picks']==b['avg_picks'])
            if ok: matches += 1
            a_s = (f"{ab['model'][:11]}@{ab['threshold']} g{ab['gmp_floor']} q{ab['qib_floor']} s{ab['sub_floor']}"
                   if ab else 'none')
            b_s = (f"{b['model'][:11]}@{b['threshold']} g{b['gmp_floor']} q{b['qib_floor']} s{b['sub_floor']}"
                   if b else 'none')
            print(f"{target:<6}{a_s:<42}{ab['avg_picks'] if ab else 0:<8}| {b_s:<40}{b['avg_picks'] if b else 0:<8}{'OK' if ok else 'MISS'}")
        print("-"*116)
        print(f"Match: {matches}/{total} | agent avg evals/tier: {int(np.mean(evals))} vs 21600 brute "
              f"({int(21600/np.mean(evals))}x fewer) | agent time {t_a:.1f}s vs brute {t_b:.1f}s ({t_b/max(t_a,0.01):.1f}x faster)")
    elif len(sys.argv)>1 and sys.argv[1] == '--verify':
        print("="*65)
        print("VERIFICATION, agent vs brute force, ALL slider levels 65-98")
        print("="*65)
        _build()
        matches = mismatches = 0
        t_agent = t_brute = 0
        for target in range(65, 99):
            t0 = time.time()
            agent_result = find_best(target, verbose=False)['best']
            t_agent += (time.time()-t0)
            t0 = time.time()
            brute_result = brute_force(target)
            t_brute += (time.time()-t0)

            if agent_result and brute_result:
                ok = agent_result['avg_picks'] == brute_result['avg_picks']
            else:
                ok = agent_result == brute_result

            if ok:
                matches += 1
                if target % 5 == 0:
                    print(f" [OK] {target}%, agent picks {agent_result['avg_picks']:.0f}, brute picks {brute_result['avg_picks']:.0f}")
            else:
                mismatches += 1
                print(f" [X] {target}%, agent: {agent_result['model']}@{agent_result['threshold']} ({agent_result['avg_picks']} picks)"
                      f" vs brute: {brute_result['model']}@{brute_result['threshold']} ({brute_result['avg_picks']} picks)")

        print()
        print(f" Match: {matches}/{matches+mismatches} = {matches/(matches+mismatches)*100:.0f}%")
        print(f" Agent total time: {t_agent:.2f}s")
        print(f" Brute total time: {t_brute:.2f}s")
        print(f" Speedup: {t_brute/t_agent:.1f}x")
    elif len(sys.argv)>1:
        find_best(int(sys.argv[1]))
    else:
        print(__doc__)
