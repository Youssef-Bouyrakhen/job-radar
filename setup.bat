@echo off
rem One-time setup for Job Radar. Double-click this file.
cd /d "%~dp0"

echo.
echo [1/4] Creating a private Python environment (.venv) ...
if not exist .venv (
    py -m venv .venv || goto :error
)

echo [2/4] Installing Python packages (this can take a few minutes) ...
.venv\Scripts\python -m pip install --upgrade pip --quiet
.venv\Scripts\python -m pip install -r requirements.txt || goto :error

echo [3/4] Installing the hidden Chrome browser for JavaScript pages ...
.venv\Scripts\python -m playwright install chromium || goto :error

echo [4/4] Creating your .env settings file ...
if not exist .env copy .env.example .env >nul

echo.
echo Setup finished! Now double-click start.bat
pause
exit /b 0

:error
echo.
echo Something went wrong. Scroll up to see the error message.
pause
exit /b 1
