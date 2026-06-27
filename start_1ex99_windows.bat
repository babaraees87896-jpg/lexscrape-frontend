@echo off
cd /d "%~dp0"
set "SKIP_PAUSE=0"
if /i "%~1"=="-Silent" set "SKIP_PAUSE=1"
if /i "%~1"=="--silent" set "SKIP_PAUSE=1"
if /i "%~1"=="-AutoStart" set "SKIP_PAUSE=1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_1ex99_windows.ps1" %*
if "%SKIP_PAUSE%"=="0" pause
