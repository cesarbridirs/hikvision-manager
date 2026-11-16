$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$certPath = Join-Path $root "certs\server.crt"

if (-not (Test-Path $certPath)) {
    throw "Certificado nao encontrado: $certPath. Execute primeiro .\gerar-certificado-local.ps1"
}

Write-Host "Importando certificado local como confiavel para o usuario atual..."
certutil.exe -user -addstore Root $certPath | Out-Null

Write-Host "Certificado importado."
Write-Host "Feche e abra novamente o navegador, depois acesse:"
Write-Host "  https://localhost:8443"
