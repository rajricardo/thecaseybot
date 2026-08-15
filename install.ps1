$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Setting up theCaseyBot (Windows)..."

# Prefer the `py` launcher (bundled with the python.org installer); fall
# back to `python` on PATH.
$PythonCmd = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $PythonCmd = "py" }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $PythonCmd = "python" }
if (-not $PythonCmd) {
    Write-Error "Python not found. Install Python 3.9+ from https://www.python.org/downloads/ (check 'Add python.exe to PATH' during install), then re-run this script."
    exit 1
}

$VenvDir = Join-Path $Dir "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $VenvDir)) {
    Write-Host "Creating virtual environment (venv)..."
    & $PythonCmd -m venv $VenvDir
    # $ErrorActionPreference = "Stop" only catches cmdlet/.NET errors, not a
    # non-zero exit code from a native command like this — check explicitly
    # so a failed venv creation doesn't silently fall through to pip install.
    if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed."; exit $LASTEXITCODE }
} else {
    Write-Host "venv already exists, skipping creation."
}

Write-Host "Installing dependencies from requirements.txt..."
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Error "pip upgrade failed."; exit $LASTEXITCODE }
& $VenvPython -m pip install -r (Join-Path $Dir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Error "Dependency install failed."; exit $LASTEXITCODE }

$ConfigPath = Join-Path $Dir "config.yaml"
$ExamplePath = Join-Path $Dir "config.example.yaml"
if (-not (Test-Path $ConfigPath)) {
    Write-Host "Creating config.yaml from config.example.yaml -- edit it with your real"
    Write-Host "Discord/IBKR/Anthropic values before running the bot (see README.md)."
    Copy-Item $ExamplePath $ConfigPath
} else {
    Write-Host "config.yaml already exists, leaving it as-is."
}

Write-Host ""
Write-Host "Setup complete. Edit config.yaml, then run .\run.ps1 to start the bot."
