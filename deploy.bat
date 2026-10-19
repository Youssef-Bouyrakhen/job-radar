@echo off
rem Updates Job Radar on the server with one double-click:
rem   1. backs up the server's database
rem   2. copies app\, static\, requirements.txt and README.md
rem      (never data\, .env or config.yaml: the server keeps its own)
rem   3. installs packages as the jobradar user and restarts the service
rem   4. checks that the server runs the same version as this folder
cd /d "%~dp0"
rem Your server goes in deploy.local.bat (not in git), one line:  set SERVER=root@your-server-ip
set SERVER=root@your-server-ip
if exist deploy.local.bat call deploy.local.bat
if "%SERVER%"=="root@your-server-ip" (
  echo   Create deploy.local.bat with the line:  set SERVER=root@your-server-ip
  goto failed
)
set APPDIR=/home/jobradar/job-radar

for /f %%v in ('.venv\Scripts\python -c "from app import VERSION; print(VERSION)"') do set VERSION=%%v
if "%VERSION%"=="" goto failed
echo.
echo   Deploying Job Radar %VERSION% to %SERVER%
echo.

echo 1/3 Backing up the server database...
ssh -o BatchMode=yes %SERVER% "bash -s backup" < deploy_server.sh
if errorlevel 1 goto failed

echo 2/3 Copying app, static, requirements.txt and README.md...
tar -czf - --exclude=__pycache__ app static requirements.txt README.md | ssh -o BatchMode=yes %SERVER% "tar -xzmf - --warning=no-unknown-keyword -C %APPDIR% && chown -R jobradar:jobradar %APPDIR%/app %APPDIR%/static %APPDIR%/requirements.txt %APPDIR%/README.md"
if errorlevel 1 goto failed

echo 3/3 Installing, restarting and checking...
ssh -o BatchMode=yes %SERVER% "bash -s finish %VERSION%" < deploy_server.sh
if errorlevel 1 goto failed

echo.
echo   Done. Reload the Job Radar page in your browser.
echo.
pause
exit /b 0

:failed
echo.
echo   Deploy FAILED (see the messages above). The server's database backup is in %APPDIR%/data.
echo.
pause
exit /b 1
