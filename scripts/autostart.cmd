@echo off
rem 自动邮箱系统常驻启动：网页(8787) + 收件守护(含每日询价自动发送)
rem 幂等：已在跑的组件不会重复启动；登录时由计划任务调用。
cd /d C:\Users\26562\Documents\Trae-CN\temp_20261116_01\autoemail

powershell -NoProfile -Command "if (-not (Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue)) { Start-Process -FilePath '.venv\Scripts\python.exe' -ArgumentList 'scripts\serve_web.py' -WorkingDirectory '.' -WindowStyle Hidden }"

powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*autoemail.cli*watch*' })) { Start-Process -FilePath '.venv\Scripts\python.exe' -ArgumentList '-m','autoemail.cli','mail','watch','--allow-auto-send','--interval','60','--log','logs\watch.log' -WorkingDirectory '.' -WindowStyle Hidden }"
