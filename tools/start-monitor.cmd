@echo off
chcp 65001 >nul
title Battery charge-limit monitor (read-only)
rem Logs battery %, AC state and EC registers every 5 s. Read-only, writes nothing.
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%~dp0battery_monitor.py"
) else (
    python "%~dp0battery_monitor.py"
)
echo.
echo Monitor stopped. Log saved as battery_log.csv next to this script.
pause
