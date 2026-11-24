@echo off
rem 开机/登录自启入口：调用 agent.ps1 启动审核台 + 邮件守护
cd /d "C:\Users\26562\Documents\Trae-CN\temp_20261116_01\autoemail"
echo [%date% %time%] run_agent.bat invoked parent=%CMDCMDLINE% >> "C:\Users\26562\Documents\Trae-CN\temp_20261116_01\autoemail\logs\start.log"
powershell -ExecutionPolicy Bypass -Command "echo ('[%date% %time%] agent.ps1 start from ' + $PID) | Out-File -Append 'C:\Users\26562\Documents\Trae-CN\temp_20261116_01\autoemail\logs\start.log'; & 'C:\Users\26562\Documents\Trae-CN\temp_20261116_01\autoemail\agent.ps1' start *>> 'C:\Users\26562\Documents\Trae-CN\temp_20261116_01\autoemail\logs\start.log'"
