$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Crie .venv e instale requirements.txt antes de inicializar o administrador."
}

$secure = Read-Host "Senha inicial do administrador (minimo 12 caracteres)" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $env:HIKVISION_INITIAL_ADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ($env:HIKVISION_INITIAL_ADMIN_PASSWORD.Length -lt 12) {
        throw "A senha inicial deve ter pelo menos 12 caracteres."
    }
    & $python -c "import sys; sys.path.insert(0, sys.argv[1]); import app; app.init_db()" (Join-Path $root "backend")
    if ($LASTEXITCODE -ne 0) { throw "Falha ao inicializar o banco ou o administrador." }
    Write-Host "Administrador preparado. Inicie o servidor e altere a senha no primeiro login."
} finally {
    Remove-Item Env:HIKVISION_INITIAL_ADMIN_PASSWORD -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
