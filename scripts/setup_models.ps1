# Sets up the handwriting models on this PC (Windows PowerShell). Called by SetupModels.bat.
#  1. creates a separate Python environment for the models (.venv-models) with CUDA PyTorch
#  2. clones DiffusionPen at the commit the driver was verified against
#  3. writes the paths into .env
# Weights are NOT downloaded here: start that from Experiments in the app (explicit action).
Set-Location (Join-Path $PSScriptRoot "..")
function Step($text) { Write-Host "`n== $text" -ForegroundColor Green }
$DiffusionPenCommit = "059e2f2bcb7372231038719e955597917c1a8783"

Step "Creating the model environment (.venv-models)"
$py = ".\.venv-models\Scripts\python.exe"
if (-not (Test-Path $py)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv-models } else { python -m venv .venv-models }
    if (-not (Test-Path $py)) { Write-Host "Could not create the environment. Is Python 3.11+ installed?" -ForegroundColor Red; exit 1 }
}
& $py -m pip install --upgrade pip --quiet

Step "Installing PyTorch with CUDA (about 2.5 GB, takes a while)"
# CUDA 12.4 builds support the RTX 20xx series (Turing) with a recent NVIDIA driver.
& $py -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
if ($LASTEXITCODE -ne 0) { Write-Host "PyTorch install failed; see above." -ForegroundColor Red; exit 1 }

Step "Installing the model libraries"
# Versions pinned by Eruku's requirements.txt; DiffusionPen works with the same ones (verified).
& $py -m pip install "diffusers==0.34.0" "transformers==4.53.0" "accelerate==1.8.1" timm einops omegaconf huggingface_hub pillow numpy
if ($LASTEXITCODE -ne 0) { Write-Host "Library install failed; see above." -ForegroundColor Red; exit 1 }

Step "Getting the DiffusionPen code"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Host "Git is needed: install it from git-scm.com and run this again." -ForegroundColor Red; exit 1 }
$repo = ".\.models\DiffusionPen"
if (-not (Test-Path "$repo\unet.py")) {
    New-Item -ItemType Directory -Force .models | Out-Null
    git clone https://github.com/koninik/DiffusionPen.git $repo
}
git -C $repo fetch --quiet origin
git -C $repo checkout --quiet $DiffusionPenCommit
if ($LASTEXITCODE -ne 0) { Write-Host "Could not check out the verified commit." -ForegroundColor Red; exit 1 }

Step "Checking the GPU"
& $py -c "import torch; ok=torch.cuda.is_available(); print('CUDA:', ok, torch.cuda.get_device_name(0) if ok else '(none: models will run on the CPU, slowly)')"

Step "Saving the paths in .env"
$full = (Resolve-Path $py).Path
$repoFull = (Resolve-Path $repo).Path
$set = @{ "HANDPRINTER_DIFFUSIONPEN_PYTHON" = $full; "HANDPRINTER_DIFFUSIONPEN_REPO" = $repoFull; "HANDPRINTER_ERUKU_PYTHON" = $full }
$lines = if (Test-Path .env) { @(Get-Content .env) } else { @() }
foreach ($key in $set.Keys) {
    $lines = @($lines | Where-Object { $_ -notmatch "^$key=" })
    $lines += "$key=$($set[$key])"
}
[IO.File]::WriteAllLines((Join-Path (Get-Location) ".env"), [string[]]$lines, (New-Object Text.UTF8Encoding $false))  # no BOM
Write-Host "`nDone. Start Handprinter, open Experiments, and press 'Download weights' on DiffusionPen (about 1.5 GB)." -ForegroundColor Green
