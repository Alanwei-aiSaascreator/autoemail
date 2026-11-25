@echo off
rem 开机/登录自启入口：调用 agent.ps1 启动审核台 + 邮件守护
cd /d "%~dp0"
set "ROOT=%~dp0"
echo [%date% %time%] run_agent.bat invoked parent=%CMDCMDLINE% >> "%ROOT%logs\start.log"
powershell -ExecutionPolicy Bypass -Command "echo ('[%date% %time%] agent.ps1 start from ' + $PID) | Out-File -Append '%ROOT%logs\start.log'; & '%ROOT%agent.ps1' start *>> '%ROOT%logs\start.log'"
