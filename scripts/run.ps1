# Handprinter launcher for Windows PowerShell. UNTESTED ON WINDOWS until confirmed by the user.
# First run creates .venv and installs the app; later runs just launch it.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.11 -m venv .venv 2>$null; if ($LASTEXITCODE -ne 0) { python -m venv .venv }
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -e ".[heic,pdf]"
}
.\.venv\Scripts\python.exe -m handprinter run
