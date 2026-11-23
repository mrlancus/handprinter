# Handprinter one-step launcher (Windows PowerShell). Called by Handprinter.bat.
#  1. pulls the latest code   2. creates/updates the Python environment when needed
#  3. installs cloudflared if missing (online mode)   4. starts the app
param([string]$Mode = "online")
Set-Location (Join-Path $PSScriptRoot "..")
function Step($text) { Write-Host "`n== $text" -ForegroundColor Green }

Step "Updating the code"
if (Get-Command git -ErrorAction SilentlyContinue) {
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Host "Could not update (offline, or files changed locally). Starting the version you have." -ForegroundColor Yellow }
} else { Write-Host "Git not found; skipping the update." -ForegroundColor Yellow }

Step "Checking Python"
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
    if (-not (Test-Path $py)) { Write-Host "Python 3.11+ is needed: install it from python.org (tick 'Add python.exe to PATH') and run this again." -ForegroundColor Red; exit 1 }
}
# Reinstall only when the dependency list changed (fast on normal days).
$stamp = ".\.venv\handprinter-installed.txt"
$want = (Get-FileHash .\pyproject.toml -Algorithm SHA256).Hash
$have = if (Test-Path $stamp) { Get-Content $stamp -Raw } else { "" }
if ($have.Trim() -ne $want) {
    Step "Installing / updating packages (first time takes a few minutes)"
    & $py -m pip install --upgrade pip --quiet
    & $py -m pip install -e ".[heic,pdf]" --quiet
    if ($LASTEXITCODE -ne 0) { Write-Host "Package install failed; see the messages above." -ForegroundColor Red; exit 1 }
    Set-Content -Path $stamp -Value $want
}

if ($Mode -eq "local") {
    Step "Starting Handprinter on this PC: http://127.0.0.1:8765"
    Start-Process "http://127.0.0.1:8765"
    & $py -m handprinter run
    exit $LASTEXITCODE
}

Step "Checking cloudflared"
$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue) -or (Test-Path "C:\Program Files (x86)\cloudflared\cloudflared.exe") -or (Test-Path "C:\Program Files\cloudflared\cloudflared.exe")
if (-not $cf) {
    Write-Host "Installing cloudflared (Cloudflare's tunnel program)..."
    winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
}

Step "Starting Handprinter online (keep this window open)"
& $py -m handprinter online
