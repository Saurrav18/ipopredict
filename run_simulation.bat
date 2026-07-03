@echo off
cd /d "%~dp0"
set "IPO_LEDGER_DIR=%~dp0"
if exist ".env" ( for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B" )
if exist "%~dp0data\GMP_ML_READY_FINAL_v3.xlsx" ( set "IPO_DATASET=%~dp0data\GMP_ML_READY_FINAL_v3.xlsx" ) else ( set "IPO_DATASET=%~dp0GMP_ML_READY_FINAL_v3.xlsx" )
echo Running the automation simulation (this proves the scheduler works end-to-end)...
echo.
py -3.12 simulate_automation.py
echo.
pause
