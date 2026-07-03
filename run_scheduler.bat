@echo off
REM ============================================================
REM  IPOPredict scheduler launcher (Task Scheduler runs this).
REM  Secrets load from .env - see .env.example. Runs one tick, exits.
REM ============================================================

cd /d "%~dp0"

REM --- force all data paths to THIS folder (prevents reading a stale copy in
REM     Downloads or elsewhere if IPO_LEDGER_DIR was set system-wide before) ---
set "IPO_LEDGER_DIR=%~dp0"

REM --- load secrets/config from .env ---
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B"
)

REM --- LIVE SETTINGS: refresh every 30 min during bidding hours (9 AM - 5 PM) ---
REM    Closing day: locks the final call the first run after the IPO closes.
REM    To go back to TEST mode (act any hour, every 10 min), swap the two blocks.
set FAST_EVERY_MIN=30
set MARKET_OPEN=9
set MARKET_CLOSE=17

REM --- TEST SETTINGS (uncomment these 3 lines, comment the 3 above, to test anytime) ---
REM set FAST_EVERY_MIN=10
REM set MARKET_OPEN=0
REM set MARKET_CLOSE=23

REM --- find the dataset whether it sits in the main folder or in data\ ---
if exist "%~dp0data\GMP_ML_READY_FINAL_v3.xlsx" (
  set IPO_DATASET=%~dp0data\GMP_ML_READY_FINAL_v3.xlsx
) else (
  set IPO_DATASET=%~dp0GMP_ML_READY_FINAL_v3.xlsx
)

REM --- run one tick (scrape/score/publish/alerts as needed) ---
py -3.12 scheduler.py
