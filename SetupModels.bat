@echo off
rem One-time setup of the handwriting models (PyTorch with CUDA + DiffusionPen code). Safe to run again.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_models.ps1" %*
pause
