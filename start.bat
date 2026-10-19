@echo off
rem Starts Job Radar. Keep this window open while you use it; close it to stop.
cd /d "%~dp0"
.venv\Scripts\python run.py
pause
