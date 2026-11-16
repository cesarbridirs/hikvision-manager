$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = $env:HIKVISION_MANAGER_PYTHON

if (-not $python) {
    $python = Join-Path $root ".venv\Scripts\python.exe"
}

if (-not (Test-Path $python)) {
    throw "Python nao encontrado. Crie .venv e instale requirements.txt, ou defina HIKVISION_MANAGER_PYTHON."
}

Write-Host "Iniciando Hikvision Manager App..."
& $python -c "from cryptography.fernet import Fernet"
if ($LASTEXITCODE -ne 0) { throw "Dependencia de criptografia ausente. Instale requirements.txt no ambiente Python." }
& $python (Join-Path $root "backend\app.py")
if ($LASTEXITCODE -ne 0) { throw "O servidor encerrou com erro. Consulte a mensagem acima." }
