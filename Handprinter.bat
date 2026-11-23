@echo off
rem One click: update Handprinter, install anything missing, and start it.
rem   Handprinter.bat          -> online (iPad/phone via Cloudflare, password required)
rem   Handprinter.bat local    -> this PC only, no Cloudflare
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
pause
