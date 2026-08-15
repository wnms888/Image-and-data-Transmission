[CmdletBinding()]
param(
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$ruleName = 'TC4 WiFi Assistant TCP 8086'

if($Elevated)
{
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if($existing)
    {
        $existing | Remove-NetFirewallRule
    }

    New-NetFirewallRule `
        -DisplayName $ruleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalAddress 192.168.137.1 `
        -LocalPort 8086 `
        -Profile Any | Out-Null
    exit 0
}

$rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if(-not $rule)
{
    Write-Host '首次启动需要管理员授权，以放行 192.168.137.1:8086 的 TCP 入站连接。'
    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"' + $PSCommandPath + '"'),
        '-Elevated'
    )
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -ArgumentList $arguments
    $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if(-not $rule)
    {
        throw '未创建防火墙规则。请在 UAC 提示中选择“是”，再重新启动软件。'
    }
}

Set-Location -LiteralPath $PSScriptRoot
& py -3 monitor.py
exit $LASTEXITCODE
