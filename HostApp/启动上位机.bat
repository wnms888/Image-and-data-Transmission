@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0启动上位机.ps1"
if errorlevel 1 pause
