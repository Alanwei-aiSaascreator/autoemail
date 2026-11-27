# 由 Windows 计划任务（AutoEmailMorningSend，每日 21:50 唤醒执行）调用：
# 保持系统唤醒约 8.5 小时，覆盖纽约 09:00-17:00 自动发送窗口（北京冬令时 22:00-06:00），
# 期满释放电源请求，允许系统再次休眠。
Add-Type -Namespace Win32 -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
$ES_CONTINUOUS = 0x80000000
$ES_SYSTEM_REQUIRED = 0x00000001
[Win32.Power]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
Start-Sleep -Seconds 30600
[Win32.Power]::SetThreadExecutionState($ES_CONTINUOUS)
