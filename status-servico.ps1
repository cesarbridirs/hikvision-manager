$serviceName = "HikvisionManagerApp"
$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue

if (-not $service) {
    Write-Host "Servico nao instalado."
    exit 1
}

$service | Format-List Name,DisplayName,Status,StartType
