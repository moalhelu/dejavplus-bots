# ===== Carfax Bot starter (PowerShell) =====
if (-not (Test-Path ".\.venv")) {
  py -3 -m venv .venv
}
. ".\.venv\Scripts\Activate.ps1"

python -m pip install --upgrade pip
pip install -r requirements.txt

# (Optional) Install Playwright Chromium for PDF rendering
try { python -m playwright install chromium } catch {}

python .\app.py
