$ErrorActionPreference = "Stop"

$serviceName = "HikvisionManagerApp"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Execute este script em um PowerShell aberto como Administrador."
}

$existing = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Servico nao encontrado."
    exit 0
}

Write-Host "Parando servico..."
Stop-Service -Name $serviceName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Host "Removendo servico..."
sc.exe delete $serviceName | Out-Null

Write-Host "Servico removido."
