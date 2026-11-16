$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
foreach ($path in @($python, (Join-Path $root "data\hikvision.db"), (Join-Path $root "certs\server.crt"), (Join-Path $root "certs\server.key"))) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Arquivo necessario ausente: $path" }
}

& $python -c "import sys; sys.path.insert(0, sys.argv[1]); import app; app.save_settings({'https_enabled': True})" (Join-Path $root "backend")
if ($LASTEXITCODE -ne 0) { throw "Falha ao habilitar HTTPS." }
Write-Host "HTTPS habilitado. Reinicie o servidor ou servico."
