Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $RepoRoot

# Check for venv
if (-not (Test-Path ".\.venv")) {
    Write-Host "Creating virtual environment..."
    py -3 -m venv .venv
}

# Activate venv
. ".\.venv\Scripts\Activate.ps1"

# Ensure deps are installed (skip by setting SKIP_PIP_INSTALL=1)
if (-not (($env:SKIP_PIP_INSTALL -as [string]) -eq "1")) {
    try { python -m pip install --upgrade pip } catch {}
    python -m pip install -r requirements.txt
    # Optional: Install Playwright Chromium for PDF rendering
    try { python -m playwright install chromium } catch {}
}

$tgCmd = "Set-Location -LiteralPath '$RepoRoot'; & '$RepoRoot\.venv\Scripts\Activate.ps1'; python app.py"
$waCmd = "Set-Location -LiteralPath '$RepoRoot'; & '$RepoRoot\.venv\Scripts\Activate.ps1'; python whatsapp_app.py"

# Start Telegram Bot
Write-Host "Starting Telegram Bot..."
Start-Process -FilePath "powershell.exe" -WorkingDirectory $RepoRoot -ArgumentList "-NoExit", "-Command", $tgCmd

# Start WhatsApp Bot
Write-Host "Starting WhatsApp Bot (FastAPI)..."
# Use python whatsapp_app.py instead of uvicorn command to ensure ProactorEventLoop policy is applied
Start-Process -FilePath "powershell.exe" -WorkingDirectory $RepoRoot -ArgumentList "-NoExit", "-Command", $waCmd

Write-Host "Both bots have been launched in separate windows."
