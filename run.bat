@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Without this, open() defaults to the OEM codepage (cp1251 here) and the
rem authheaders public suffix list, which is UTF-8, fails to decode. DMARC then
rem falls over and every inbound message is accepted unverified.
set PYTHONUTF8=1

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

rem Tray monitor and the first-run wizard need PySide6 (LGPL — unlike the GPL
rem PyQt5 this replaced). The server does not depend on it: if the install
rem fails, mail still runs.
.venv\Scripts\python.exe -m pip show PySide6-Essentials >nul 2>&1
if errorlevel 1 (
    echo Installing PySide6-Essentials...
    .venv\Scripts\python.exe -m pip install PySide6-Essentials
)

rem First run: no .env yet. The setup wizard creates a full working .env
rem (API token, encryption and DKIM keys), the admin account, and shows the
rem DKIM DNS record. It runs synchronously and exits, so the server below
rem starts only once .env exists.
if not exist .env (
    echo.
    echo First run: .env not found. Launching the setup wizard...
    echo.
    .venv\Scripts\python.exe -m tray.app --setup-only
)

if not exist .env (
    echo.
    echo ERROR: .env still missing — setup was cancelled or failed.
    echo Complete the wizard, or copy .env.example to .env and set
    echo MG_DOMAIN and MG_API_TOKENS manually.
    echo.
    pause
    exit /b 1
)

rem Tray monitor (separate lightweight process). The server does not depend on
rem it: closing the tray does not stop mail.
start "" .venv\Scripts\pythonw.exe -m tray.app

.venv\Scripts\python.exe run.py
pause
