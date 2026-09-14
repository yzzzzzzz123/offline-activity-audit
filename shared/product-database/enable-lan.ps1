#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
Start-Transcript -Path (Join-Path $PSScriptRoot 'runtime\enable-lan.log') -Append | Out-Null
trap {
    Write-Output ($_ | Out-String)
    Stop-Transcript | Out-Null
    exit 1
}
$serviceName = 'OfflineProductCatalog'
$ruleName = 'OfflineProductCatalog-MySQL-3307-LAN'
$config = Join-Path $PSScriptRoot 'my.ini'
$server = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqld.exe'
$client = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysql.exe'
$adminClient = 'C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqladmin.exe'
$credentialsPath = Join-Path $env:LOCALAPPDATA 'OfflineActivityAudit\ProductDatabase\credentials.json'
$credentials = Get-Content -LiteralPath $credentialsPath -Raw | ConvertFrom-Json
$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue

if ($service) {
    $existing = Get-CimInstance Win32_Service -Filter "Name='$serviceName'"
    if ($existing.PathName -notlike "*$config*") {
        throw 'The existing service uses another configuration. No changes were made.'
    }
} else {
    $previousPassword = $env:MYSQL_PWD
    try {
        $env:MYSQL_PWD = $credentials.admin_password
        $dataPath = & $client "--defaults-file=$config" --user=root --batch --raw --skip-column-names '--execute=SELECT @@datadir'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot verify the independent database on port 3307.' }
        $expected = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'runtime\data'))).Replace('\', '/').TrimEnd('/')
        if (($dataPath.Trim().Replace('\', '/').TrimEnd('/')) -ne $expected) {
            throw 'Port 3307 belongs to another database. No changes were made.'
        }
        $oldProcessId = [int](Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime\mysql.pid') -Raw)
        & $adminClient "--defaults-file=$config" --user=root shutdown
        if ($LASTEXITCODE -ne 0) { throw 'The independent database did not shut down cleanly.' }
        # mysqladmin may return before InnoDB has released its data file locks.
        if (Get-Process -Id $oldProcessId -ErrorAction SilentlyContinue) {
            Wait-Process -Id $oldProcessId -Timeout 30 -ErrorAction SilentlyContinue
        }
        if (Get-Process -Id $oldProcessId -ErrorAction SilentlyContinue) {
            throw 'The bootstrap process is still exiting. Retry after it has stopped.'
        }
        & $server --install $serviceName "--defaults-file=$config"
        if ($LASTEXITCODE -ne 0) {
            Start-Process -FilePath $server -ArgumentList ('--defaults-file="' + $config + '"') -WindowStyle Hidden
            throw 'Service installation failed. The independent database was restarted as a normal process.'
        }
    } finally {
        $env:MYSQL_PWD = $previousPassword
    }
}

Set-Service -Name $serviceName -StartupType Automatic
Start-Service -Name $serviceName
(Get-Service -Name $serviceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))

# This rule permits only this database port on the current LAN.
if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
    Remove-NetFirewallRule -Name $ruleName
}
New-NetFirewallRule -Name $ruleName -DisplayName 'Offline product catalog MySQL 3307 (LAN)' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort 3307 `
    -LocalAddress 192.0.0.148 -RemoteAddress 192.0.0.0/24 `
    -Program $server -Profile Any | Out-Null

Write-Host 'MySQL product_catalog is running at 192.0.0.148:3307.'
Write-Host 'Windows automatic startup and the LAN firewall rule are enabled.'
Write-Host 'The existing MySQL service on port 3306 and the audit application were not changed.'
Stop-Transcript | Out-Null
