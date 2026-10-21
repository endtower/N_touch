# Requires administrator rights only with -Apply or -RestoreFrom.
# Default: inspect configuration without changing it.
[CmdletBinding(DefaultParameterSetName = 'Inspect')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Apply')]
    [switch]$Apply,
    [Parameter(Mandatory, ParameterSetName = 'Restore')]
    [string]$RestoreFrom
)

$ErrorActionPreference = 'Stop'
$netshPath = Join-Path $env:SystemRoot 'System32\netsh.exe'

function Get-TcpRange([string]$Family) {
    $output = & $netshPath interface $Family show dynamicport tcp 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Cannot read $Family TCP range: $output" }
    $numbers = @($output | ForEach-Object {
        if ("$_" -match '^[^:]*:\s*(\d+)\s*$') { [int]$Matches[1] }
    })
    if ($numbers.Count -ne 2) { throw "Cannot parse $Family TCP range: $output" }
    return [pscustomobject]@{ Start = $numbers[0]; Count = $numbers[1] }
}

function Set-TcpRange([string]$Family, $Range) {
    $start = [int]$Range.Start
    $count = [int]$Range.Count
    if ($start -lt 1024 -or $count -lt 255 -or ($start + $count - 1) -gt 65535) {
        throw "Invalid $Family range: $start / $count"
    }
    $output = & $netshPath interface $Family set dynamicport tcp "start=$start" "num=$count" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Cannot set $Family TCP range: $output" }
    $actual = Get-TcpRange $Family
    if ($actual.Start -ne $start -or $actual.Count -ne $count) {
        throw "$Family TCP range verification failed"
    }
}

$before = [pscustomobject]@{
    CapturedAt = (Get-Date).ToString('o')
    IPv4 = Get-TcpRange 'ipv4'
    IPv6 = Get-TcpRange 'ipv6'
}
if ($PSCmdlet.ParameterSetName -eq 'Inspect') {
    $before | ConvertTo-Json
    exit 0
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an administrator PowerShell. No settings were changed.'
}

$desired = if ($Apply) {
    [pscustomobject]@{
        IPv4 = [pscustomobject]@{ Start = 49152; Count = 16384 }
        IPv6 = [pscustomobject]@{ Start = 49152; Count = 16384 }
    }
} else {
    Get-Content -LiteralPath $RestoreFrom -Raw | ConvertFrom-Json
}
if (-not $desired.IPv4 -or -not $desired.IPv6) { throw 'Backup must include IPv4 and IPv6 ranges.' }

$backupDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'logs\diagnostics'
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$backupPath = Join-Path $backupDir ("tcp-range-{0}.json" -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
$before | ConvertTo-Json | Set-Content -LiteralPath $backupPath -Encoding UTF8
Write-Output "Backup: $backupPath"
try {
    Set-TcpRange 'ipv4' $desired.IPv4
    Set-TcpRange 'ipv6' $desired.IPv6
} catch {
    $failure = $_
    # Restore both families if either update or verification failed.
    foreach ($family in 'ipv4', 'ipv6') {
        try { Set-TcpRange $family $before.$family } catch { Write-Warning "Rollback $family failed: $_" }
    }
    throw $failure
}
[pscustomobject]@{ IPv4 = Get-TcpRange 'ipv4'; IPv6 = Get-TcpRange 'ipv6' } | ConvertTo-Json
