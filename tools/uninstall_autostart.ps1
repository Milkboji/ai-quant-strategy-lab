# 移除后台自启（计划任务与快捷方式）。不会停止正在运行的服务，也不会删除任何数据。
$name = "AIQuantLab-KeepAlive"
Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
foreach ($p in @(
    (Join-Path ([Environment]::GetFolderPath("Startup")) "AI量化交易-后台启动.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "AI量化交易.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "AI量化交易.lnk"))) {
    if (Test-Path $p) { Remove-Item $p }
}
Write-Host "[OK] 已移除后台自启、桌面图标和开始菜单项"
