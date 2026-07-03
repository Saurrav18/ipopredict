@echo off
cd /d "%~dp0"
echo ===============================================
echo IPOPredict - Local Setup (Windows)
echo ===============================================
echo.
echo [1/2] Installing Python packages (1-3 min)...
py -3.12 -m pip install -q -r requirements.txt
if errorlevel 1 goto pipfail
echo   Dependencies installed
echo.
echo [2/2] Checking data...
if exist "qualified_ipos.json" echo   qualified_ipos.json found - the site has IPOs to show right away.
if not exist "qualified_ipos.json" echo   qualified_ipos.json not here yet - run_scheduler.bat will build it later.
echo.
echo ===============================================
echo SETUP COMPLETE
echo ===============================================
echo.
echo Next: double-click run_web.bat, then open  127.0.0.1:8000
echo.
pause
goto end

:pipfail
echo.
echo   *** Could not install packages with Python 3.12. Check it with:
echo   ***     py --list
echo   *** You should see a line with 3.12. If not, reinstall Python 3.12
echo   *** from python.org (tick "Add to PATH" and "py launcher").
echo.
pause

:end
