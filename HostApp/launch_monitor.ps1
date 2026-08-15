[CmdletBinding()]
param(
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$ruleName = 'TC4 WiFi Assistant TCP 8086'
$logPath = Join-Path $PSScriptRoot 'firewall_rule_setup.log'

if($Elevated)
{
    try
    {
        $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
        if(-not $existing)
        {
            New-NetFirewallRule `
                -DisplayName $ruleName `
                -Direction Inbound `
                -Action Allow `
                -Protocol TCP `
                -LocalAddress 192.168.137.1 `
                -LocalPort 8086 `
                -Profile Any `
                -ErrorAction Stop | Out-Null
        }
        "SUCCESS: $ruleName" | Set-Content -LiteralPath $logPath -Encoding utf8
        exit 0
    }
    catch
    {
        $_ | Out-String | Set-Content -LiteralPath $logPath -Encoding utf8
        exit 1
    }
}

$rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if(-not $rule)
{
    Write-Host 'Administrator approval is required once to allow TCP 8086.'
    $argumentLine = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Elevated"
    $elevatedProcess = $null
    try
    {
        $elevatedProcess = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') `
            -Verb RunAs `
            -Wait `
            -PassThru `
            -ArgumentList $argumentLine
    }
    catch
    {
        Write-Warning "Firewall elevation was not completed: $($_.Exception.Message)"
    }
    $rule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if(-not $rule)
    {
        $exitCode = if($elevatedProcess) { $elevatedProcess.ExitCode } else { 'not started' }
        $details = if(Test-Path -LiteralPath $logPath) { Get-Content -LiteralPath $logPath -Raw } else { 'No elevated-process log was written.' }
        Write-Warning "Firewall rule was not created (exit code $exitCode). The monitor will still start. Details: $details"
    }
}

Set-Location -LiteralPath $PSScriptRoot
& py -3 monitor.py
exit $LASTEXITCODE
