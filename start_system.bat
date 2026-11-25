@echo off
chcp 65001 >nul
rem 一键启动审核台 + 邮件守护（后台隐藏运行，不弹黑框）
echo 正在启动系统，请稍候...
powershell -ExecutionPolicy Bypass -File "%~dp0agent.ps1" start
echo.
echo 已启动。浏览器打开 http://127.0.0.1:8787 访问审核台。
echo 本窗口可关闭，系统继续在后台运行。
timeout /t 5 >nul
