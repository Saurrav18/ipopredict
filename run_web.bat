@echo off
REM ============================================================
REM  IPOPredict website launcher.
REM  Secrets now live in the .env file next to this script (never
REM  committed). Copy .env.example to .env and fill it in once.
REM  Leave this window open while using the site.
REM ============================================================

cd /d "%~dp0"

REM --- load secrets/config from .env (skips blank + # comment lines) ---
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do set "%%A=%%B"
) else (
  echo WARNING: no .env file found. Copy .env.example to .env and fill it in.
)

REM --- start the website ---
REM --host 0.0.0.0 lets other devices on your WiFi (your phone) reach this site
REM at  http://YOUR-LAPTOP-IP:8000  . 127.0.0.1:8000 still works on the laptop.
REM For a PUBLIC deploy, set SECURE_COOKIES=1 and APP_URL in .env and serve over HTTPS.
py -3.12 -m uvicorn app:app --host 0.0.0.0 --port 8000

echo.
echo (the website stopped - read any error message above)
pause
