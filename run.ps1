$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Kill any leftover bot.py from a previous run (or a manually-started one)
# before spawning a new one — a stale process still holding the IBKR
# clientId (config.yaml's ibkr.client_id) would make the new connection
# fail, and a stale Discord session would double-process every message.
Write-Host "Stopping any leftover bot.py from a previous run..."
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*bot.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Setup (venv + dependencies) lives in install.ps1 now, not here — run that
# first if either of these is missing.
$VenvPython = Join-Path $Dir "venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Error "venv not found. Run .\install.ps1 first."
    exit 1
}

# config.yaml holds real secrets (Discord token, IBKR settings) and is
# gitignored/untracked on purpose — fail early with a clear message rather
# than letting bot.py crash on a missing key deep in main().
$ConfigPath = Join-Path $Dir "config.yaml"
if (-not (Test-Path $ConfigPath)) {
    Write-Error "config.yaml not found. Run .\install.ps1 first, then fill in real values."
    exit 1
}

Write-Host "Starting bot.py... Casey Bridge UI will be at http://127.0.0.1:8787"
& $VenvPython (Join-Path $Dir "bot.py")
exit $LASTEXITCODE
