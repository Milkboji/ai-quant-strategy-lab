# 防睡眠 / 防自动重启设置（2026-10-30）。默认只显示当前设置；加 -Apply 才修改（需要管理员权限）。
# 结果写到 logs\power_setup.log。
#   不睡眠、不休眠（插电和电池都不睡）；关闭休眠文件（同时关闭"快速启动"）；合盖不做任何操作；
#   Windows 更新：活动时段 12:00~次日 6:00（覆盖美股交易时段 21:30~次日 5:00），不自动调整活动时段，
#   登录状态下不自动重启（策略 NoAutoRebootWithLoggedOnUsers；家庭版可能忽略这条策略）。
param([switch]$Apply)
$ErrorActionPreference = "Continue"
$log = Join-Path (Split-Path $PSScriptRoot -Parent) "logs\power_setup.log"
function Out-Log($s) { $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $s"; Write-Host $line; Add-Content -Path $log -Value $line -Encoding UTF8 }

function Show-State($title) {
  Out-Log "==== $title ===="
  Out-Log ("电源计划：" + (powercfg /getactivescheme))
  foreach ($q in @("SUB_SLEEP STANDBYIDLE", "SUB_SLEEP HIBERNATEIDLE", "SUB_BUTTONS LIDACTION")) {
    $r = powercfg /query SCHEME_CURRENT $q.Split(" ")[0] $q.Split(" ")[1] 2>$null | Select-String "当前交流电源设置索引|当前直流电源设置索引|Current AC Power Setting Index|Current DC Power Setting Index"
    Out-Log ("$q : " + (($r | ForEach-Object { $_.ToString().Trim() }) -join " ; "))
  }
  $hib = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Power" -ErrorAction SilentlyContinue).HibernateEnabled
  Out-Log "休眠文件 HibernateEnabled = $hib"
  $ux = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings" -ErrorAction SilentlyContinue
  Out-Log "更新活动时段 = $($ux.ActiveHoursStart) ~ $($ux.ActiveHoursEnd)，自动调整 SmartActiveHoursState = $($ux.SmartActiveHoursState)"
  $au = Get-ItemProperty "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" -ErrorAction SilentlyContinue
  Out-Log "登录时不自动重启 NoAutoRebootWithLoggedOnUsers = $($au.NoAutoRebootWithLoggedOnUsers)"
  Out-Log ("Windows 版本：" + (Get-CimInstance Win32_OperatingSystem).Caption)
}

Show-State "当前设置"
if (-not $Apply) { Out-Log "只查看，未修改（确认后用 -Apply 运行）"; exit 0 }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Out-Log "需要管理员权限：请右键 power_setup.bat → 以管理员身份运行"; exit 1 }

powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
powercfg /hibernate off

$ux = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
New-Item -Path $ux -Force | Out-Null
Set-ItemProperty -Path $ux -Name ActiveHoursStart -Value 12 -Type DWord
Set-ItemProperty -Path $ux -Name ActiveHoursEnd -Value 6 -Type DWord
Set-ItemProperty -Path $ux -Name SmartActiveHoursState -Value 0 -Type DWord
$wu = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
New-Item -Path "$wu\AU" -Force | Out-Null
Set-ItemProperty -Path "$wu\AU" -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord
Set-ItemProperty -Path $wu -Name SetActiveHours -Value 1 -Type DWord
Set-ItemProperty -Path $wu -Name ActiveHoursStart -Value 12 -Type DWord
Set-ItemProperty -Path $wu -Name ActiveHoursEnd -Value 6 -Type DWord

Show-State "修改后"
Out-Log "已应用。笔记本请保持插电；关机 / 重启前请先在网页上点「安全重启」或确认没有美股交易时段。"
