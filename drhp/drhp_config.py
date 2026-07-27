"""
drhp_config.py - named, overridable thresholds.

WHY: the audit counted 187 magic numbers. The worst offenders are thresholds
that encode a JUDGEMENT (when is leverage "high"? when does a cross-check
fail?) compiled into source, so tuning them required a code deploy. Every
constant here has a name, a rationale, and an env override, which also makes
the numbers visible in one place for an interviewer instead of scattered.

This migrates the highest-judgement thresholds first (consistency audit + the
leverage flag family); remaining literals migrate as they are next touched.
"""
import os


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- consistency audit (iteration 35) ---
# revenue below this share of total income means a wrong row was captured;
# other income is a minority line by definition. 0.30 leaves room for holding
# companies with large other income while catching the 7% Xtranet case.
AUDIT_REV_SHARE_MIN   = _f("DRHP_AUDIT_REV_SHARE_MIN", 0.30)
# stated ROE vs PAT/networth disagreement beyond this factor flags networth
AUDIT_ROE_DISAGREE_X  = _f("DRHP_AUDIT_ROE_DISAGREE_X", 2.0)
# EBITDA-vs-margin tolerance: max(abs points, fraction of stated margin)
AUDIT_EBITDA_ABS_PTS  = _f("DRHP_AUDIT_EBITDA_ABS_PTS", 10.0)
AUDIT_EBITDA_REL_FRAC = _f("DRHP_AUDIT_EBITDA_REL_FRAC", 0.5)
# workforce cost above this % of revenue is implausible for a real company
AUDIT_WORKFORCE_MAX   = _f("DRHP_AUDIT_WORKFORCE_MAX", 60.0)

# --- red-flag family: leverage ---
FLAG_DE_HIGH_X        = _f("DRHP_FLAG_DE_HIGH_X", 1.5)   # D/E above this flags
