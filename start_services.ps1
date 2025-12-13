# Check for venv
if (-not (Test-Path ".\.venv")) {
    Write-Host "Creating virtual environment..."
    py -3 -m venv .venv
}

# Activate venv
. ".\.venv\Scripts\Activate.ps1"

# Install dependencies if needed
# pip install -r requirements.txt

# Start Telegram Bot
Write-Host "Starting Telegram Bot..."
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '.\.venv\Scripts\Activate.ps1'; python app.py"

# Start WhatsApp Bot
Write-Host "Starting WhatsApp Bot (FastAPI)..."
# Use python whatsapp_app.py instead of uvicorn command to ensure ProactorEventLoop policy is applied
Start-Process powershell -ArgumentList "-NoExit", "-Command", "& '.\.venv\Scripts\Activate.ps1'; python whatsapp_app.py"

Write-Host "Both bots have been launched in separate windows."
