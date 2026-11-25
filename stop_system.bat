@echo off
chcp 65001 >nul
rem 一键停止审核台 + 邮件守护
echo 正在停止系统...
powershell -ExecutionPolicy Bypass -File "%~dp0agent.ps1" stop
echo.
echo 系统已停止。
timeout /t 5 >nul
