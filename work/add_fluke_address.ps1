$ErrorActionPreference = 'Stop'
$taskResult = Join-Path $PSScriptRoot 'network_change_result.json'
$taskBackup = Join-Path $PSScriptRoot 'network_change_before.json'
$taskAddressAdded = $false
try {
    $nic = Get-NetAdapter -Physical | Where-Object { $_.InterfaceGuid -eq '{A86017AC-4FFD-4CA1-A2A0-803022EBBE2A}' }
    if (@($nic).Count -ne 1 -or $nic.Status -ne 'Up' -or $nic.InterfaceDescription -ne 'Realtek PCIe GbE Family Controller') {
        throw 'The verified Realtek Ethernet adapter is no longer available or connected.'
    }
    $ipInterface = Get-NetIPInterface -InterfaceIndex $nic.ifIndex -AddressFamily IPv4
    if ($ipInterface.Dhcp -ne 'Disabled') { throw 'DHCP state changed. No changes made.' }
    $addresses = @(Get-NetIPAddress -InterfaceIndex $nic.ifIndex -AddressFamily IPv4)
    if (-not ($addresses | Where-Object { $_.IPAddress -eq '192.168.100.100' -and $_.PrefixLength -eq 24 })) {
        throw 'The original Ethernet configuration changed. No changes made.'
    }
    $existing = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -eq '169.254.1.100' })
    if ($existing.Count -gt 0) { throw '169.254.1.100 already exists on this PC. No changes made.' }
    @{
        adapterGuid = [string]$nic.InterfaceGuid
        interfaceIndex = $nic.ifIndex
        addresses = @($addresses | Select-Object IPAddress,PrefixLength,PrefixOrigin,SkipAsSource)
        addedAddress = '169.254.1.100'
        addedPrefix = 16
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $taskBackup -Encoding UTF8
    New-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress '169.254.1.100' -PrefixLength 16 -SkipAsSource $true | Out-Null
    $taskAddressAdded = $true
    $status = $null
    for ($attempt = 0; $attempt -lt 16; $attempt++) {
        Start-Sleep -Milliseconds 500
        $status = Get-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress '169.254.1.100'
        if ($status.AddressState -eq 'Preferred') { break }
        if ($status.AddressState -eq 'Duplicate') { throw 'Windows detected a duplicate address.' }
    }
    if ($status.AddressState -ne 'Preferred') { throw "Address is not ready: $($status.AddressState)" }
    @{ success = $true; adapter = $nic.Name; address = '169.254.1.100'; prefix = 16; state = [string]$status.AddressState } |
        ConvertTo-Json | Set-Content -LiteralPath $taskResult -Encoding UTF8
} catch {
    $taskFailure = $_.Exception.Message
    if ($taskAddressAdded) {
        Remove-NetIPAddress -InterfaceIndex $nic.ifIndex -IPAddress '169.254.1.100' -Confirm:$false -ErrorAction SilentlyContinue
    }
    @{ success = $false; error = $taskFailure } | ConvertTo-Json |
        Set-Content -LiteralPath $taskResult -Encoding UTF8
    exit 1
}
