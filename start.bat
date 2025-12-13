@echo off
REM ===== Carfax Bot starter (Windows CMD) =====
setlocal enableextensions enabledelayedexpansion

if not exist .venv (
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
pip install -r requirements.txt

REM (Optional) Install Playwright Chromium for PDF rendering
python -m playwright install chromium >nul 2>nul

echo.
echo Starting Carfax bot...
python app.py
echo.
pause
