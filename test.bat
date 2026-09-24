@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Must match run.bat: tests should run under the same encoding as the server.
set PYTHONUTF8=1

if not exist .venv (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
)

echo === unit tests ===
.venv\Scripts\python.exe -m pytest
if errorlevel 1 goto :failed

echo.
echo === end-to-end smoke test ===
.venv\Scripts\python.exe smoke_test.py
if errorlevel 1 goto :failed

echo.
echo ALL TESTS PASSED
goto :end

:failed
echo.
echo TESTS FAILED

:end
pause
