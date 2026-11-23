@echo off
rem Handprinter ONLINE launcher for Windows: runs the app on this PC and publishes it through a
rem Cloudflare Tunnel behind your password (HANDPRINTER_PASSWORD in .env). See docs\online.md.
rem UNTESTED ON WINDOWS until confirmed by the user.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv || exit /b 1
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -e ".[heic,pdf]" || exit /b 1
)
where cloudflared >nul 2>nul
if errorlevel 1 if not exist "C:\Program Files (x86)\cloudflared\cloudflared.exe" if not exist "C:\Program Files\cloudflared\cloudflared.exe" (
  echo Installing cloudflared with winget...
  winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
)
".venv\Scripts\python.exe" -m handprinter online
pause
