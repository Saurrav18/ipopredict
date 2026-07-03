@echo off
REM ==========================================================
REM  IPOPredict - one-time cleanup of OLD leftover files
REM  Safe: it ONLY deletes files from previous versions.
REM  It does NOT touch the current app, your code.py, your
REM  dataset xlsx, or your users.json.
REM  Run this once, inside your ipopredict folder.
REM ==========================================================
echo Cleaning up old leftover files...

REM --- old code from the previous architecture ---
del /q ipo_card_builder.py        2>nul
del /q ipo_explainer.py           2>nul
del /q ipo_reasoning_agent.py     2>nul
del /q ipo_runner.py              2>nul
del /q ipo_qualifier.py           2>nul
del /q learning_logger.py         2>nul
del /q pattern_extractor.py       2>nul
del /q screener_scraper.py        2>nul
del /q cloud_compat_test.py       2>nul
del /q scraper_test.py            2>nul
del /q test_gemini.py             2>nul
del /q verify_setup.py            2>nul

REM --- old docs ---
del /q ARCHITECTURE_REPORT.md     2>nul
del /q COMMIT_PLAN.md             2>nul
del /q DECISIONS.md               2>nul
del /q GETTING_LIVE.md            2>nul
del /q MIGRATION_PLAN.md          2>nul
del /q PROJECT_STATE.md           2>nul
del /q START_HERE.md              2>nul
del /q TOMORROW_TEST.md           2>nul
del /q DEPLOY.md                  2>nul
del /q DEPLOY_GUIDE.md            2>nul
del /q rule_update_2026-06-09.md  2>nul

REM --- stale pages + old requirements ---
del /q page_ipo_hexagon.html      2>nul
del /q page_ipo_cmr.html          2>nul
del /q card_hexagon_nutrition.json 2>nul
del /q card_cmr_green_technologies.json 2>nul
del /q requirements-local.txt     2>nul
del /q requirements-pipeline.txt  2>nul

REM --- runtime files that rebuild themselves (and the OLD db) ---
del /q ipo_ledger.db              2>nul
del /q alerts_sent.json           2>nul
del /q scheduler.log              2>nul
del /q scheduler_state.json       2>nul
del /q update_state.json          2>nul
rmdir /s /q __pycache__           2>nul

REM --- old zips (keep the newest one if you like, this clears stray copies) ---
del /q "ipo-ledger (1).zip"       2>nul
del /q "ipo-ledger (2).zip"       2>nul
del /q "ipo-ledger (3).zip"       2>nul
del /q "ipo-ledger (4).zip"       2>nul
del /q "ipo-ledger (5).zip"       2>nul

echo.
echo Done. Old files removed.
echo Your current app, code.py, dataset, and users.json were left untouched.
echo NOTE: ipo_ledger.db was deleted on purpose (new accounts format) -
echo it will be recreated the first time you run the site.
pause
