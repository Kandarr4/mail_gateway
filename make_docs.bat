@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

rem Rebuilds both .docx documents from docs\generate\*.py
rem Prices and company details live in docs\generate\config.py

if not exist .venv (
    echo ERROR: .venv not found. Run run.bat once first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m pip show python-docx >nul 2>&1
if errorlevel 1 (
    echo Installing python-docx...
    .venv\Scripts\python.exe -m pip install python-docx
)

echo.
echo === Building documents ===
.venv\Scripts\python.exe docs\generate\make_manual.py
if errorlevel 1 goto :failed
.venv\Scripts\python.exe docs\generate\make_offer.py
if errorlevel 1 goto :failed

echo.
echo DONE. Files are in the docs folder.
goto :end

:failed
echo.
echo BUILD FAILED

:end
pause
