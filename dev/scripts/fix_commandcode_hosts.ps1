#Requires -Version 5.1
<#
.SYNOPSIS
    一键修复 api.commandcode.ai 直连链路（重新优选 IP 并写入 hosts）。

.DESCRIPTION
    该域名的 IPv4 在国内被 DNS 污染，只能靠 hosts 钉死真 IP；而 hosts 一次只能钉
    一个 IP、没有故障转移，国际出口拥塞时该 IP 会整段劣化，导致 commandcode 无法接入。

    本脚本自动完成：提权 -> 调 test_llm_connectivity.py 优选 -> 写入 hosts -> 刷新 DNS。

.PARAMETER NoPause
    执行完直接退出，不停留等待回车（供自动化调用）。

.EXAMPLE
    右键「使用 PowerShell 运行」，或在任意终端执行：
    powershell -ExecutionPolicy Bypass -File scripts\fix_commandcode_hosts.ps1
#>
param(
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'

$HostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$RepoRoot  = Split-Path -Parent $PSScriptRoot
$PyScript  = Join-Path $RepoRoot 'scripts\test_llm_connectivity.py'
$Domain    = 'api.commandcode.ai'

# --- 1. 提权：写入 hosts 需要管理员权限，未提权则自提权重跑 ---
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host '写入 hosts 需要管理员权限，正在请求提权（请在弹出的 UAC 窗口点「是」）…' -ForegroundColor Yellow
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($NoPause) { $argList += '-NoPause' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -ArgumentList $argList
    exit
}

# --- 2. 优选 Cloudflare 边缘 IP（复用连通性测试脚本的 DoH + 多轮实测逻辑） ---
if (-not (Test-Path -LiteralPath $PyScript)) {
    throw "找不到优选脚本：$PyScript"
}
Write-Host '正在优选 Cloudflare 边缘 IP…' -ForegroundColor Cyan

$picked = $null
$median = $null
foreach ($line in (& python $PyScript 'pick')) {
    if ($line -match '^PICKED=(\d{1,3}(?:\.\d{1,3}){3})\s+HOST=\S+\s+MEDIAN=([\d.]+)') {
        $picked = $Matches[1]
        $median = $Matches[2]
        break
    }
}
if (-not $picked) {
    throw '优选失败：所有候选 IP 均不可用。直连链路当前不可用，需换接入点或走代理。'
}

# --- 3. 只替换该域名对应的行，hosts 其余内容原样保留 ---
$pattern  = "^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+$([regex]::Escape($Domain))\s*$"
$oldIp    = $null
$replaced = $false
$newLines = New-Object 'System.Collections.Generic.List[string]'

foreach ($line in [System.IO.File]::ReadAllLines($HostsPath)) {
    if ($line -match $pattern) {
        $oldIp = $Matches[1]
        if (-not $replaced) {
            $newLines.Add("$picked $Domain")
            $replaced = $true
        }
        continue
    }
    $newLines.Add($line)
}

if (-not $replaced) {
    $newLines.Add("# $Domain : IPv4 DNS is poisoned in CN; pinned to the fastest Cloudflare edge.")
    $newLines.Add("$picked $Domain")
}

if ($oldIp -eq $picked) {
    Write-Host "无需修改：hosts 已是 $picked $Domain（中位 $median s）" -ForegroundColor Green
} else {
    [System.IO.File]::WriteAllLines($HostsPath, $newLines, [System.Text.Encoding]::ASCII)
    Write-Host "已写入：$oldIp ->  $picked $Domain（中位 $median s）" -ForegroundColor Green
}

# --- 4. 刷新 DNS 缓存，让 hosts 立即生效 ---
& ipconfig /flushdns | Out-Null
Write-Host 'DNS 缓存已刷新。' -ForegroundColor Green

if (-not $NoPause) {
    Write-Host ''
    Read-Host '按回车键关闭'
}
