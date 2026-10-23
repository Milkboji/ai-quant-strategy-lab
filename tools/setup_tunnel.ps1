# 配置 Cloudflare Tunnel：把本机 127.0.0.1:8501 安全地映射到你的域名（不开放任何端口，不需要公网 IP）。
# 需要先在 config/tunnel.json 里写好 {"hostname": "quant.你的域名"}。
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "tools\cloudflared.exe"
$cfgJson = Join-Path $root "config\tunnel.json"
$yml = Join-Path $root "config\cloudflared.yml"
if (-not (Test-Path $cfgJson)) { throw "缺少 config\tunnel.json（需要写明 hostname）" }
$cfg = Get-Content $cfgJson -Raw -Encoding UTF8 | ConvertFrom-Json
$hostname = $cfg.hostname
$name = if ($cfg.tunnel_name) { $cfg.tunnel_name } else { "aiquant" }
Write-Host "外网地址：https://$hostname"

# 1) 下载 Cloudflare 官方 cloudflared（GitHub 官方发布页）
if (-not (Test-Path $exe)) {
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Write-Host "[..] 下载 cloudflared：$url"
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
}
$sig = Get-AuthenticodeSignature $exe
Write-Host "[OK] cloudflared 签名：$($sig.Status) $($sig.SignerCertificate.Subject)"
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Cloudflare") {
    Remove-Item $exe -Force
    throw "cloudflared 数字签名校验失败，已删除，请重试"
}
& $exe --version

# 2) 授权：会打开浏览器，选择你的域名并点 Authorize（只需一次）
$cert = Join-Path $env:USERPROFILE ".cloudflared\cert.pem"
if (-not (Test-Path $cert)) {
    Write-Host "[..] 浏览器将打开 Cloudflare 授权页面：请选择域名并点击 Authorize"
    & $exe tunnel login
    if (-not (Test-Path $cert)) { throw "没有完成授权" }
}

# 3) 创建（或复用）隧道
$list = & $exe tunnel list --output json | ConvertFrom-Json
$t = $list | Where-Object { $_.name -eq $name } | Select-Object -First 1
if (-not $t) {
    & $exe tunnel create $name | Out-Host
    $list = & $exe tunnel list --output json | ConvertFrom-Json
    $t = $list | Where-Object { $_.name -eq $name } | Select-Object -First 1
}
$id = $t.id
$cred = Join-Path $env:USERPROFILE ".cloudflared\$id.json"
if (-not (Test-Path $cred)) { throw "找不到隧道凭证文件 $cred（如果隧道是在别的电脑上创建的，请在 Cloudflare 后台删除后重试）" }
Write-Host "[OK] 隧道 $name ($id)"

# 4) 写配置：只把这个域名转发到本机 8501，其他一律 404
$content = @"
tunnel: $id
credentials-file: $cred
protocol: quic
edge-ip-version: auto
ingress:
  - hostname: $hostname
    service: http://127.0.0.1:8501
  - service: http_status:404
"@
[IO.File]::WriteAllText($yml, $content, (New-Object System.Text.UTF8Encoding($false)))
& $exe tunnel ingress validate --config $yml | Out-Host

# 5) DNS：把域名指向这个隧道
& $exe tunnel route dns --overwrite-dns $name $hostname | Out-Host
Write-Host "[OK] 完成。服务启动时会自动带上外网通道：https://$hostname"
exit 0
