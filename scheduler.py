"""IPOPredict scheduler.

  - NORMAL days: run the full update ONCE a day (scrape -> score -> publish -> learn).
  - BIDDING / CLOSING window (a tracked IPO is OPEN for subscription): refresh
    EVERY 30 MINUTES during market hours, so the live subscription numbers and the
    call stay current right up to the close. Closing-day numbers move fast, so this
    keeps the site and the prediction fresh. Alerts fire for IPOs closing today/tomorrow.
  - LISTING days (a tracked IPO debuts today): fetch the actual listing price every
    hour during market hours, so the outcome is stamped and fed back into learning.

Run it EVERY 30 MINUTES from Windows Task Scheduler (recommended):
       py -V:3.12 scheduler.py
Each run decides what is needed and is idempotent (a state file prevents double
runs): the heavy daily job runs once a day, the 30-min refresh runs only while an
IPO is open, and the hourly listing fetch runs only on a listing day.

Or leave it looping in a terminal:
       py -V:3.12 scheduler.py --daemon

Settings via environment (all optional):
  PYTHON_CMD               python used to run the steps. Default: the SAME python
                           running this scheduler, so `py -V:3.12 scheduler.py`
                           makes every step use 3.12 automatically.
  SCRAPER_SCRIPT=code.py   your scraper filename
  DAILY_HOUR=9             hour (0-23, local) to run the once-a-day update
  MARKET_OPEN=9            intraday fetches start at this hour
  MARKET_CLOSE=16          intraday fetches stop at this hour
  FAST_EVERY_MIN=30        minutes between refreshes while an IPO is open
  CLOSING_LOOKAHEAD=4      treat an IPO as "open" if it closes within this many days
  IPO_LEDGER_DIR=.         folder holding qualified_ipos.json + the scripts
"""
import os, sys, json, subprocess, time
from datetime import datetime, date, timedelta
from pathlib import Path

# Anchor to this script's own folder by default, so the log + state files always
# land next to the code no matter what directory the task is launched from.
_SCRIPT_DIR = Path(__file__).resolve().parent
DIR            = Path(os.environ.get("IPO_LEDGER_DIR", str(_SCRIPT_DIR)))
# Safety net: if IPO_LEDGER_DIR was left pointing at an old/empty folder (e.g. a
# stale copy in Downloads) that has no data, but the script's own folder does,
# trust the script's folder instead. Prevents reading stale IPO data.
if not (DIR / "qualified_ipos.json").exists() and (_SCRIPT_DIR / "qualified_ipos.json").exists():
    DIR = _SCRIPT_DIR
STATE_FILE     = DIR / "scheduler_state.json"
QUALIFIED      = DIR / "qualified_ipos.json"
DAILY_HOUR     = int(os.environ.get("DAILY_HOUR", "9"))
MARKET_OPEN    = int(os.environ.get("MARKET_OPEN", "9"))
MARKET_CLOSE   = int(os.environ.get("MARKET_CLOSE", "16"))
FAST_EVERY_MIN = max(5, int(os.environ.get("FAST_EVERY_MIN", "30")))
CLOSING_LOOKAHEAD = int(os.environ.get("CLOSING_LOOKAHEAD", "4"))

# Run each step with the SAME python as this scheduler unless overridden, so
# `py -V:3.12 scheduler.py` makes the scraper/publish/update all use 3.12 too.
PYTHON = os.environ.get("PYTHON_CMD") or sys.executable or "python"
def _cmd(script): return f'"{PYTHON}" {script}'
SCRAPER_CMD = os.environ.get("SCRAPER_CMD") or _cmd(os.environ.get("SCRAPER_SCRIPT", "code.py"))
PUBLISH_CMD = os.environ.get("PUBLISH_CMD") or _cmd("publish.py")
UPDATE_CMD  = os.environ.get("UPDATE_CMD")  or _cmd("ipo_update.py")
ALERTS_CMD  = os.environ.get("ALERTS_CMD")  or _cmd("send_alerts.py")


def _load_state():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except Exception: pass
    return {}

def _save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2, default=str))

LOG_FILE = DIR / "scheduler.log"
def _log(msg):
    """Print AND append to scheduler.log, so Task Scheduler runs are visible."""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        # don't crash the tick, but make the failure visible instead of hiding it
        print(f"  [warn] could not write {LOG_FILE}: {e}")

def _parse_date(s):
    if not s: return None
    s = str(s).strip()
    for fmt in ("%d %b, %Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y", "%b %d, %Y"):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: continue
    try:
        import pandas as pd
        return pd.to_datetime(s, dayfirst=True).date()
    except Exception:
        return None

def _load_ipos():
    if not QUALIFIED.exists(): return []
    try: return json.loads(QUALIFIED.read_text())
    except Exception: return []


def bidding_window():
    """IPOs currently OPEN for subscription (refresh every 30 min while true).
    Open = scored as 'open', or close date is today/within CLOSING_LOOKAHEAD days,
    and the IPO has not listed yet."""
    today = date.today()
    open_names, closing_today = [], False
    for ipo in _load_ipos():
        if ipo.get("listed_on"):
            continue
        cd = _parse_date(ipo.get("close_date"))
        is_open = (ipo.get("state") == "open") or (cd and 0 <= (cd - today).days <= CLOSING_LOOKAHEAD)
        if not is_open:
            continue
        if cd and cd < today:        # already closed, awaiting listing
            continue
        open_names.append(ipo.get("ipo_name", "?"))
        if cd == today:
            closing_today = True
    return (len(open_names) > 0, open_names, closing_today)


def is_listing_day():
    """True if any tracked IPO lists today (or closed 1-7 days ago and isn't
    marked listed yet, the usual gap before a debut)."""
    today = date.today()
    listing = []
    for ipo in _load_ipos():
        name = ipo.get("ipo_name", "?")
        ld = _parse_date(ipo.get("listing_date"))
        if ld == today:
            listing.append(name); continue
        if ipo.get("listed_on"):
            continue
        cd = _parse_date(ipo.get("close_date"))
        if cd and 0 < (today - cd).days <= 7 and ld is None:
            listing.append(name + " (expected)")
    return (len(listing) > 0), listing


def _run(cmd, retries=2):
    """Run a step, retrying a couple times on a transient hiccup."""
    for attempt in range(retries + 1):
        print(f"  $ {cmd}" + (f"  (retry {attempt})" if attempt else ""))
        r = subprocess.run(cmd, shell=True, cwd=str(DIR))
        if r.returncode == 0:
            return True
        if attempt < retries:
            wait = 30 * (attempt + 1)
            print(f"  ! exited {r.returncode}; waiting {wait}s and retrying")
            time.sleep(wait)
    print(f"  ! command failed after {retries+1} attempts: {cmd}")
    return False


# closing-day alert windows (local time): fire each once, at/after this time
ALERT_WINDOWS = [("morning", 9, 30), ("midday", 13, 30), ("final", 15, 0)]

def _due_alert_stage(now, state, today_str):
    """The latest closing-day alert window that has arrived and not yet fired today.
    Returns a stage name (morning/midday/final) or None."""
    mins = now.hour * 60 + now.minute
    done = set(state.get("alerts_day", {}).get(today_str, []))
    pick = None
    for stage, h, m in ALERT_WINDOWS:
        if mins >= h * 60 + m and stage not in done:
            pick = stage  # keep the latest one that is due
    return pick

def run_alerts():
    """Night-before heads-up for IPOs closing tomorrow. The closing-DAY alerts are
    staged (morning / midday / final) and handled by run_stage_alerts()."""
    today = date.today()
    ipos = _load_ipos()
    if any(_parse_date(i.get("close_date")) == today + timedelta(days=1) for i in ipos):
        _log("  alerts: an IPO closes tomorrow (heads-up)")
        _run(ALERTS_CMD, retries=1)

def run_stage_alerts(stage):
    """Fire the staged closing-day alert (morning / midday / final)."""
    _log(f"  alerts: closing-day {stage} alert")
    _run(ALERTS_CMD + f" --{stage}", retries=1)


def run_daily():
    _log("DAILY UPDATE starting (scrape -> publish -> learn -> alerts)")
    _run(SCRAPER_CMD)      # scrape latest IPOs -> active_ipos_v13.xlsx
    _run(PUBLISH_CMD)      # score + mainboard filter -> qualified_ipos.json
    _run(UPDATE_CMD)       # append listings, retrain after 7, rebuild archive
    run_alerts()
    _log("DAILY UPDATE done. git push the refreshed files to update the live site.")


def run_bidding_fetch(names, closing_today):
    _log(f"BIDDING REFRESH starting (every {FAST_EVERY_MIN} min)")
    print(f"  open now: {', '.join(names)}" + ("   [CLOSING TODAY]" if closing_today else ""))
    _run(SCRAPER_CMD)      # pull the latest subscription numbers
    _run(PUBLISH_CMD)      # re-score with the fresh numbers
    run_alerts()
    _log("BIDDING REFRESH done. git push to update the live site.")


def run_hourly_listing(names):
    _log("LISTING-DAY HOURLY FETCH starting")
    print(f"  listing today: {', '.join(names)}")
    _run(PUBLISH_CMD)      # re-fetch GMP/listing and re-score
    _run(UPDATE_CMD)       # grab the actual listing price, feed back into learning
    _log("LISTING-DAY HOURLY FETCH done. git push to update the live site.")


def tick():
    """One decision cycle. Safe to call every 30 min from Task Scheduler."""
    now = datetime.now()
    state = _load_state()
    today_str = str(date.today())
    in_market = MARKET_OPEN <= now.hour <= MARKET_CLOSE

    bidding_now, open_names, closing_today = bidding_window()
    listing_now, listing_names = is_listing_day()
    _log(f"tick {now:%H:%M} | bidding={'yes' if bidding_now else 'no'} "
         f"open={','.join(open_names) if open_names else 'none'} | "
         f"listing={'yes' if listing_now else 'no'} | market_hours={'yes' if in_market else 'no'} "
         f"({MARKET_OPEN}:00-{MARKET_CLOSE}:00) | every {FAST_EVERY_MIN}min")

    # 1) once-a-day heavy update (scrape -> publish -> learn -> alerts)
    if now.hour >= DAILY_HOUR and state.get("last_daily") != today_str:
        run_daily()
        state["last_daily"] = today_str
        _save_state(state)

    # 2) fast refresh while an IPO is OPEN (every FAST_EVERY_MIN, market hours)
    if bidding_now and in_market:
        slot = (now.hour * 60 + now.minute) // FAST_EVERY_MIN   # one slot per FAST_EVERY_MIN
        tag = f"{today_str} {slot}"
        if state.get("last_fast") != tag:
            run_bidding_fetch(open_names, closing_today)
            state["last_fast"] = tag
            _save_state(state)
        else:
            _log(f"  already refreshed this {FAST_EVERY_MIN}-min slot; skipping fetch.")

    # 3) hourly listing fetch on listing days (market hours)
    elif listing_now and in_market:
        this_hour = now.strftime("%Y-%m-%d %H")
        if state.get("last_hourly") != this_hour:
            run_hourly_listing(listing_names)
            state["last_hourly"] = this_hour
            _save_state(state)

    else:
        if bidding_now or listing_now:
            _log(f"  {'an IPO is open' if bidding_now else 'an IPO lists today'}; "
                 f"outside market hours ({MARKET_OPEN}:00-{MARKET_CLOSE}:00), waiting.")
        else:
            _log("  nothing open or listing right now; daily update only.")

    # 4) closing-day staged alerts (morning / midday / final), each fired once.
    #    Runs after the fetch above so the email reflects the freshest subscription.
    if closing_today:
        stage = _due_alert_stage(now, state, today_str)
        if stage:
            run_stage_alerts(stage)
            mins = now.hour * 60 + now.minute
            day = state.setdefault("alerts_day", {})
            done = set(day.get(today_str, []))
            for s, h, m in ALERT_WINDOWS:
                if h * 60 + m <= mins:        # mark this + earlier windows done
                    done.add(s)               # so morning never fires in the afternoon
            day[today_str] = sorted(done)
            _save_state(state)


def daemon():
    print("Scheduler running as a loop. Ctrl+C to stop.")
    print(f"  daily {DAILY_HOUR}:00 | refresh every {FAST_EVERY_MIN} min while an IPO is open | "
          f"listing fetch hourly {MARKET_OPEN}:00-{MARKET_CLOSE}:00")
    while True:
        try:
            tick()
        except Exception as e:
            _log(f"  tick error: {type(e).__name__}: {e}")
        time.sleep(60 * FAST_EVERY_MIN)


if __name__ == "__main__":
    try:
        if "--daemon" in sys.argv:
            daemon()
        elif "--status" in sys.argv:
            b, bn, ct = bidding_window()
            l, ln = is_listing_day()
            print("Bidding open:", ("YES -> " + ", ".join(bn) + (" [closing today]" if ct else "")) if b else "no")
            print("Listing today:", ("YES -> " + ", ".join(ln)) if l else "no")
            print("State:", _load_state())
        elif "--once" in sys.argv:
            run_daily()        # force a full refresh now (handy for the local test)
        else:
            tick()             # single shot - ideal for a Task Scheduler trigger
    except Exception as e:
        _log(f"FATAL: {type(e).__name__}: {e}")
        raise
