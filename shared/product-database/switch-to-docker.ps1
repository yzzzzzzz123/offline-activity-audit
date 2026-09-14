#Requires -RunAsAdministrator
# One-time Windows cutover of this project's existing native instance.
param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [string]$LanAddress = '192.0.0.148',
    [string]$LanSubnet = '192.0.0.0/24'
)
$ErrorActionPreference = 'Stop'
$serviceName = 'OfflineProductCatalog'
$ruleName = 'OfflineProductCatalog-MySQL-3307-LAN'
$config = Join-Path $PSScriptRoot 'my.ini'
$envPath = Join-Path $PSScriptRoot '.env'
$control = Join-Path $PSScriptRoot 'dbctl.py'
$mysql = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'
$mysqld = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe'
$log = Join-Path $PSScriptRoot 'runtime\docker-cutover.log'

function Set-LanRule([string]$Program = 'Any') {
    if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -Name $ruleName
    }
    New-NetFirewallRule -Name $ruleName -DisplayName 'Offline product catalog MySQL 3307 (LAN)' `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort 3307 `
        -LocalAddress $LanAddress -RemoteAddress $LanSubnet -Program $Program -Profile Any | Out-Null
}

Start-Transcript -Path $log -Append | Out-Null
$stoppedSource = $false
$proxyCreated = $false
$savedPassword = $env:MYSQL_PWD
try {
    $source = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
    if (!$source -or $source.PathName -notlike "*$config*" -or $source.State -ne 'Running') {
        throw 'Expected native source service is not running with this project configuration.'
    }
    if (!(Get-NetIPAddress -IPAddress $LanAddress -ErrorAction SilentlyContinue)) {
        throw 'The requested LAN IP is not assigned to this computer.'
    }
    $beforeEnv = [IO.File]::ReadAllText($envPath)
    if ($beforeEnv -notmatch '(?m)^MYSQL_PORT=13307\r?$') {
        throw 'Expected the verified staging container on port 13307.'
    }
    if ($beforeEnv -notmatch '(?m)^MYSQL_BIND_ADDRESS=127\.0\.0\.1\r?$') {
        throw 'The Windows Docker container must listen only on loopback port 13307.'
    }
    $proxyKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\PortProxy\v4tov4\tcp'
    if (Get-ItemProperty -Path $proxyKey -Name "$LanAddress/3307" -ErrorAction SilentlyContinue) {
        throw 'A port proxy already owns this address; no existing proxy was changed.'
    }
    $baseline = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime\migration-baseline.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $verified = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime\restore-test-verified.json') -Raw | ConvertFrom-Json
    if (!$verified.all_fields_equal -or !$verified.recreate_persists -or !$verified.failed_import_rolled_back) {
        throw 'Backup/restore and persistence verification has not completed.'
    }
    if (!(Test-Path -LiteralPath $baseline.backup) -or
        (Get-FileHash -LiteralPath $baseline.backup -Algorithm SHA256).Hash.ToLowerInvariant() -ne $baseline.sha256) {
        throw 'The source backup is missing or has a different checksum.'
    }
    & $PythonPath -B $control check
    if ($LASTEXITCODE -ne 0) { throw 'Staging container check failed.' }
    Start-Service -Name iphlpsvc

    Stop-Service -Name $serviceName
    $stoppedSource = $true
    (Get-Service -Name $serviceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    Set-Service -Name $serviceName -StartupType Disabled
    Set-LanRule
    # Windows/WSL mirrored networking cannot loop back through the Windows LAN IP.
    # Keep Docker private on localhost and expose the existing LAN port through Windows.
    & netsh.exe interface portproxy add v4tov4 "listenaddress=$LanAddress" listenport=3307 `
        connectaddress=127.0.0.1 connectport=13307 protocol=tcp
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the Windows LAN port forward.' }
    $proxyCreated = $true
    Start-Sleep -Seconds 2

    $credentialsPath = Join-Path $env:LOCALAPPDATA 'OfflineActivityAudit\ProductDatabase\credentials.json'
    $credentials = Get-Content -LiteralPath $credentialsPath -Raw | ConvertFrom-Json
    $env:MYSQL_PWD = $credentials.viewer_password
    $count = & $mysql --no-defaults --protocol=TCP "--host=$LanAddress" --port=3307 `
        --user=product_viewer --ssl-mode=REQUIRED --connect-timeout=5 --batch --skip-column-names `
        '--execute=SELECT COUNT(*) FROM product_catalog.products;'
    if ($LASTEXITCODE -ne 0 -or [int]$count -ne $baseline.rows.Count) {
        throw 'Final LAN viewer connection or row count verification failed.'
    }
    Write-Host "Docker MySQL is active at ${LanAddress}:3307 with $count products."
    Write-Host 'The native source service is stopped and disabled; its data is retained for rollback.'
    Write-Host 'The audit service and other MySQL instances were not changed.'
} catch {
    Write-Host 'Cutover failed; restoring the native source when it was stopped.'
    if ($stoppedSource) {
        if ($proxyCreated) {
            & netsh.exe interface portproxy delete v4tov4 "listenaddress=$LanAddress" listenport=3307 protocol=tcp
            if ($LASTEXITCODE -ne 0) {
                Write-Host 'Could not remove the port forward. Resolve port ownership before starting the native source.'
                throw
            }
        }
        Set-LanRule -Program $mysqld
        $startupMode = if ($source.StartMode -eq 'Auto') { 'Automatic' } else { 'Manual' }
        Set-Service -Name $serviceName -StartupType $startupMode
        Start-Service -Name $serviceName
        Write-Host 'Native source restored on port 3307.'
    }
    throw
} finally {
    $env:MYSQL_PWD = $savedPassword
    Stop-Transcript | Out-Null
}
