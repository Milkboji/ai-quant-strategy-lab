# 安装后台自启：登录自动启动 + 每 5 分钟保活 + 桌面 / 开始菜单图标。不需要管理员权限，不修改系统设置。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$vbs = Join-Path $root "tools\run_hidden.vbs"
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"
$name = "AIQuantLab-KeepAlive"

# 1) 计划任务：每 5 分钟检查一次，没在运行就后台启动（电池供电也运行；错过的执行在唤醒后补上）
$action = New-ScheduledTaskAction -Execute $wscript -Argument "`"$vbs`" ensure" -WorkingDirectory $root
$triggers = @(New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5))
try {   # 登录时立即启动（需要管理员权限；没有权限时靠每 5 分钟的保活，登录后最多 5 分钟内启动）
    $triggers += New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
} catch { }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$desc = "AI Quant Lab: keep the local dashboard and paper-trading loop running (127.0.0.1:8501)"
try {
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers -Settings $settings -Description $desc -Force | Out-Null
    Write-Host "[OK] 计划任务 $name：登录时启动 + 每 5 分钟保活"
} catch {
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers[0] -Settings $settings -Description $desc -Force | Out-Null
    Write-Host "[OK] 计划任务 $name：每 5 分钟保活（登录后最多 5 分钟内自动启动）"
}
$ErrorActionPreference = "Continue"

# 2) 快捷方式：开机登录自启、桌面图标、开始菜单
$ws = New-Object -ComObject WScript.Shell
function New-Link($path, $arg, $desc) {
    try {
        $l = $ws.CreateShortcut($path)
        $l.TargetPath = $wscript
        $l.Arguments = "`"$vbs`" $arg"
        $l.WorkingDirectory = $root
        $l.Description = $desc
        $l.IconLocation = "$env:WINDIR\System32\imageres.dll,144"
        $l.Save()
        return $true
    } catch {
        Write-Host "[WARN] 无法创建快捷方式（可能被安全软件拦截）：$path"
        return $false
    }
}
$startup = [Environment]::GetFolderPath("Startup")
if (New-Link (Join-Path $startup "AI量化交易-后台启动.lnk") "ensure" "登录后在后台启动 AI 量化交易系统") {
    Write-Host "[OK] 登录自启快捷方式：$startup" }
$desktop = [Environment]::GetFolderPath("Desktop")
if (New-Link (Join-Path $desktop "AI量化交易.lnk") "open" "打开 AI 量化交易系统（没在运行会先自动启动）") {
    Write-Host "[OK] 桌面图标：AI量化交易" }
$programs = [Environment]::GetFolderPath("Programs")
if (New-Link (Join-Path $programs "AI量化交易.lnk") "open" "打开 AI 量化交易系统") {
    Write-Host "[OK] 开始菜单：AI量化交易" }
exit 0
