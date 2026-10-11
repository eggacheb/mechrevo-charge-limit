@echo off
chcp 65001 >nul
rem Read-only EC probe through the firmware ACPI WMI mailbox.
rem Requires administrator, because the mailbox instance is not readable otherwise.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)
echo Running read-only EC probe via firmware WMI mailbox...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wmi_read.ps1"
echo.
echo This probe is READ-ONLY. No register was written.
pause
