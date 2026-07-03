"""
simulate_automation.py  -  PROVE the automation on your own machine.

What it does (no manual steps, no real scraping, no waiting days):
  - fakes the clock and a fake "Knack Packaging" IPO that is closing today
  - drives the REAL scheduler tick() across: closing day -> day after (lock)
    -> listing day (grade), exactly as Windows Task Scheduler would
  - prints the locked prediction, the actual listing gain, and HIT/MISS
  - writes sim output to a throwaway folder (simdir/) so your real data is untouched

Run it:   py -3.12 simulate_automation.py
(or just double-click  run_simulation.bat)
"""
import os, sys, json, datetime as dt, subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIM  = HERE / "simdir"
SIM.mkdir(exist_ok=True)
for f in SIM.glob("*"):
    try: f.unlink()
    except: pass

# point everything at the throwaway sim folder
os.environ.update({
    "IPO_DATASET":  os.environ.get("IPO_DATASET", str(HERE / "GMP_ML_READY_FINAL_v3.xlsx")),
    "SCRAPER_XLSX": str(SIM / "active_ipos_v13.xlsx"),
    "FROZEN_FILE":  str(SIM / "predictions_frozen.json"),
    "IPO_LEDGER_DIR": str(SIM),
    "OUT_JSON":     str(SIM / "qualified_ipos.json"),
    "INDEX_HTML":   str(HERE / "index.html"),
    "MARKET_OPEN":"9","MARKET_CLOSE":"17","FAST_EVERY_MIN":"30","DAILY_HOUR":"9",
})
if not Path(os.environ["IPO_DATASET"]).exists():
    alt = HERE / "data" / "GMP_ML_READY_FINAL_v3.xlsx"
    if alt.exists(): os.environ["IPO_DATASET"] = str(alt)

import pandas as pd
CUR = [dt.date(2026, 7, 8)]   # controllable "today"

def scrape(close, listing, qib, total, gmp, lp=None, lg=None, status=None, sym=None):
    """Write a fake scraper output row (what code.py would produce)."""
    r = {"Date":"08/07/26","IPO_Name":"Knack Packaging IPO","Issue_Size(crores)":439.0,
         "QIB":qib,"HNI":19.67,"RII":4.32,"Total":total,"Offer Price":170.0,
         "List Price":lp,"Listing Gain":lg,"Close_Date":close,"gmp_closing_day":gmp,
         "gmp_closing_gain_pct":round(gmp/170*100,2),"pre_issue_pe":18.33,"roe_ronw":35.75,
         "promoter_holding_post_ipo":70.59,"sector":"BFSI","ofs_pct":13.7,"fresh_issue_pct":86.6,
         "market_sentiment_ratio":1.03,"ratio_1m_vs_3m":1.0,"nifty_volatility_30d":11.5,
         "trend_score":2,"_listing_date":listing,"_status":status,"_symbol":sym}
    pd.DataFrame([r]).to_excel(SIM / "active_ipos_v13.xlsx", index=False)

def run_publish():
    env = dict(os.environ); env["SIM_TODAY"] = CUR[0].isoformat()
    subprocess.run([sys.executable, str(HERE / "publish.py")], env=env,
                   cwd=str(SIM), capture_output=True, text=True)

import scheduler as S
class D(dt.date):
    @classmethod
    def today(cls): return CUR[0]
class DT(dt.datetime):
    _h, _m = 9, 0
    @classmethod
    def now(cls, tz=None): return dt.datetime(CUR[0].year,CUR[0].month,CUR[0].day,cls._h,cls._m)
S.date, S.datetime = D, DT
def fake_run(cmd):
    run_publish()
    S._log(f"    (ran {cmd.split()[-1] if isinstance(cmd,str) else cmd[-1]})")
S._run = fake_run
S.run_alerts = lambda *a, **k: S._log("    (alerts checked)")
S.run_stage_alerts = lambda *a, **k: None

def frozen():
    try:
        d = json.load(open(SIM / "predictions_frozen.json"))
        return " | ".join(f"{v['verdict']} {v.get('range_low')}..{v.get('range_high')} LOCKED={v.get('locked')}"
                          for v in d.values())
    except Exception: return "(none yet)"

def tick(day, h, m, label):
    CUR[0] = day; DT._h, DT._m = h, m
    print(f"\n----- Task Scheduler fires: {day} {h:02d}:{m:02d}  ({label}) -----")
    S.tick()
    print(f"    frozen call: {frozen()}")

print("="*72)
print(" AUTOMATION SIMULATION  -  real scheduler, fake clock, no manual steps")
print("="*72)

scrape("8 Jul, 2026", "Wed, Jul 15, 2026T", 3.61, 7.38, 27.0)   # Knack OPEN, closes 8 Jul
print("\n########## 8 Jul  -  CLOSING DAY ##########")
tick(dt.date(2026,7,8), 9, 0,  "9 AM daily update")
tick(dt.date(2026,7,8), 13, 0, "1 PM bidding refresh")
scrape("8 Jul, 2026", "Wed, Jul 15, 2026T", 3.61, 7.38, 31.0)   # GMP climbed late
tick(dt.date(2026,7,8), 16, 30,"4:30 PM final refresh before close")

print("\n########## 9 Jul  -  DAY AFTER CLOSE (should LOCK) ##########")
scrape("8 Jul, 2026", "Wed, Jul 15, 2026T", 3.61, 7.38, 31.0)
tick(dt.date(2026,7,9), 9, 0, "first run after it closed")

print("\n########## 15 Jul  -  LISTING DAY, +23.5% (should GRADE) ##########")
scrape("8 Jul, 2026", "Wed, Jul 15, 2026", 3.61, 7.38, 31.0, lp=210.0, lg=23.53, status="listed", sym="KNACK")
tick(dt.date(2026,7,15), 11, 0, "listing fetch + grade")

print("\n" + "="*72)
try:
    d = json.load(open(SIM / "qualified_ipos.json"))
    for x in d:
        g = x.get("graded")
        if g:
            print(" FINAL RESULT the website would show:")
            print(f"   {x['ipo_name']}")
            print(f"   Locked prediction : {g['verdict']}  {g['range_low']}% to {g['range_high']}%")
            print(f"   Actual listing    : {g['actual']}%")
            print(f"   Grade             : {'HIT' if g['hit'] else 'MISS'}")
except Exception as e:
    print(" could not read result:", e)
print("="*72)
print(f" Full run log: {SIM / 'scheduler.log'}")
print(" (simdir/ is a throwaway folder - your real data was NOT touched.)")
