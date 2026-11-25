param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('start', 'stop', 'status', 'restart')]
    [string]$Action
)

$Root = $PSScriptRoot
$Python = "$Root\.venv\Scripts\python.exe"
$LogDir = "$Root\logs"
$WebPidFile = "$LogDir\web.pid"
$WatchPidFile = "$LogDir\watch.pid"
$WatchLog = "$LogDir\watch.log"

function Ensure-LogDir {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
}

function Get-Instance([string]$Pattern) {
    # 按命令行匹配已在跑的实例，避免重复启动
    return Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*autoemail.cli*$Pattern*" }
}

function Stop-Pattern([string]$Pattern) {
    # 按命令行清理残留实例（pid 文件丢失/竞态双启动时兜底）
    foreach ($proc in Get-Instance $Pattern) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Output "killed stale $Pattern PID=$($proc.ProcessId)"
    }
}

function Start-Web {
    Ensure-LogDir
    Stop-Pattern "web *--port 8787*"
    $proc = Start-Process -FilePath $Python `
        -ArgumentList "-m", "autoemail.cli", "web", "--port", "8787", "--no-open" `
        -WorkingDirectory $Root -PassThru `
        -RedirectStandardOutput "$LogDir\web.out.log" `
        -RedirectStandardError "$LogDir\web.err.log"
    $proc.Id | Out-File -FilePath $WebPidFile -Encoding ascii
    Write-Output "web started PID=$($proc.Id)"
}

function Start-Watch {
    Ensure-LogDir
    Stop-Pattern "mail watch*--allow-auto-send*"
    $proc = Start-Process -FilePath $Python `
        -ArgumentList "-m", "autoemail.cli", "mail", "watch", "--interval", "30", "--allow-auto-send", "--log", $WatchLog `
        -WorkingDirectory $Root -PassThru `
        -RedirectStandardOutput "$LogDir\watch.out.log" `
        -RedirectStandardError "$LogDir\watch.err.log"
    $proc.Id | Out-File -FilePath $WatchPidFile -Encoding ascii
    Write-Output "watch started PID=$($proc.Id)"
}

function Stop-One([string]$PidFile, [string]$Name) {
    if (Test-Path $PidFile) {
        $procId = [int](Get-Content $PidFile -ErrorAction SilentlyContinue)
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $procId -Force
            Write-Output "$Name stopped PID=$procId"
        }
        else {
            Write-Output "$Name not running (stale pid)"
        }
        Remove-Item $PidFile -ErrorAction SilentlyContinue
    }
    else {
        Write-Output "$Name not started"
    }
}

function Show-Status {
    foreach ($item in @(@($WebPidFile, "web"), @($WatchPidFile, "watch"))) {
        $f = $item[0]; $n = $item[1]
        if (Test-Path $f) {
            $procId = [int](Get-Content $f -ErrorAction SilentlyContinue)
            $alive = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($alive) { Write-Output "$n : PID=$procId running" }
            else { Write-Output "$n : PID=$procId exited" }
        }
        else {
            Write-Output "$n : not started"
        }
    }
    if (Test-Path $WatchLog) {
        Write-Output "--- watch.log (last 5) ---"
        Get-Content $WatchLog -Tail 5 -Encoding UTF8
    }
}

switch ($Action) {
    'start'   { Start-Web; Start-Watch }
    'stop'    { Stop-One $WebPidFile "web"; Stop-One $WatchPidFile "watch"; Stop-Pattern "web *--port 8787*"; Stop-Pattern "mail watch*--allow-auto-send*" }
    'restart' { Stop-One $WebPidFile "web"; Stop-One $WatchPidFile "watch"; Start-Sleep -Seconds 1; Start-Web; Start-Watch }
    'status'  { Show-Status }
}
