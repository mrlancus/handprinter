@echo off
rem Handprinter launcher for Windows (cmd). UNTESTED ON WINDOWS until confirmed by the user.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv || exit /b 1
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -e ".[heic,pdf]" || exit /b 1
)
".venv\Scripts\python.exe" -m handprinter run
