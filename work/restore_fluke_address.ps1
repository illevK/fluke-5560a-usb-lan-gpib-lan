$ErrorActionPreference = 'Stop'
$taskDir = 'C:\Users\HCT-2714\Documents\Codex\2026-10-29\fluke-5560a-usb-lan-gpib-lan\work'
try {
    $backup = Get-Content -LiteralPath (Join-Path $taskDir 'network_change_before.json') -Raw | ConvertFrom-Json
    if ($backup.addedAddress -ne '169.254.1.100' -or $backup.addedPrefix -ne 16) { throw 'Unexpected backup.' }
    $nic = Get-NetAdapter -Physical | Where-Object { [string]$_.InterfaceGuid -eq [string]$backup.adapterGuid }
    if (@($nic).Count -ne 1) { throw 'Original adapter not found.' }
    $alias = Get-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress '169.254.1.100' -ErrorAction SilentlyContinue
    if ($alias) {
        if ($alias.PrefixLength -ne 16 -or -not $alias.SkipAsSource) { throw 'Alias changed since the test. No changes made.' }
        Remove-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress '169.254.1.100' -Confirm:$false
    }
    @{success=$true; removed='169.254.1.100'; addresses=@(Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4 | Select-Object IPAddress,PrefixLength,SkipAsSource)} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskDir 'network_restore_result.json') -Encoding utf8
} catch {
    @{success=$false; error=$_.Exception.Message} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $taskDir 'network_restore_result.json') -Encoding utf8
    exit 1
}
