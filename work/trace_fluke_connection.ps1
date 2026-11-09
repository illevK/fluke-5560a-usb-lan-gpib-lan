$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$taskDir = 'C:\Users\HCT-2714\Documents\Codex\2026-10-29\fluke-5560a-usb-lan-gpib-lan\work'
$taskLog = Join-Path $taskDir 'pktmon_capture_log.txt'
$taskTrace = Join-Path $taskDir 'fluke_target.etl'
$taskStarted = $false
$taskFilterAdded = $false
try {
    $filterList = & pktmon filter list
    $filterList | Out-File -LiteralPath $taskLog -Encoding utf8
    if (-not ($filterList | Where-Object { $_.Trim() -match '^(없음|None)$' })) {
        throw 'Packet monitor filters are not empty; existing diagnostics were preserved.'
    }
    & pktmon filter add Fluke5560Task -i 169.254.1.1 | Out-File -LiteralPath $taskLog -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw 'Unable to set target-only capture filter.' }
    $taskFilterAdded = $true
    & pktmon start --capture --pkt-size 128 --file-size 8 --file-name $taskTrace | Out-File -LiteralPath $taskLog -Append -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw 'Packet capture did not start.' }
    $taskStarted = $true
    $taskResults = @()
    foreach ($sourceAddress in @('169.254.1.100','192.168.100.100')) {
        $row = @{ source = $sourceAddress }
        $endpoint = [Net.IPEndPoint]::new([Net.IPAddress]::Parse($sourceAddress),0)
        $client = [Net.Sockets.TcpClient]::new($endpoint)
        try {
            $pendingConnect = $client.ConnectAsync('169.254.1.1',3490)
            if (-not $pendingConnect.Wait(3000)) { throw 'TCP timeout' }
            $row.tcp = 'connected'
            $stream = $client.GetStream()
            $stream.ReadTimeout = 2000
            $queryBytes = [Text.Encoding]::ASCII.GetBytes("*IDN?`r`n")
            $stream.Write($queryBytes,0,$queryBytes.Length)
            $receiveBuffer = New-Object byte[] 2048
            $received = $stream.Read($receiveBuffer,0,$receiveBuffer.Length)
            $row.response = [Text.Encoding]::ASCII.GetString($receiveBuffer,0,$received)
        } catch {
            $row.tcpError = $_.Exception.Message
        } finally {
            $client.Close()
        }
        $row.ping = (& ping -n 1 -w 1000 -S $sourceAddress 169.254.1.1) -join "`n"
        $taskResults += $row
    }
    $taskResults | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $taskDir 'trace_probe_result.json') -Encoding utf8
} catch {
    $_.Exception.Message | Out-File -LiteralPath $taskLog -Append -Encoding utf8
} finally {
    if ($taskStarted) {
        & pktmon stop | Out-File -LiteralPath $taskLog -Append -Encoding utf8
        & pktmon etl2txt $taskTrace --out (Join-Path $taskDir 'fluke_target_trace.txt') --verbose | Out-File -LiteralPath $taskLog -Append -Encoding utf8
    }
    if ($taskFilterAdded) {
        & pktmon filter remove | Out-File -LiteralPath $taskLog -Append -Encoding utf8
    }
    'complete' | Set-Content -LiteralPath (Join-Path $taskDir 'trace_complete.txt')
}
