$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$serviceName = "HikvisionManagerApp"
$displayName = "Hikvision Manager App"
$serviceDir = Join-Path $root "ServicoWindows"
$source = Join-Path $serviceDir "HikvisionManagerService.cs"
$exe = Join-Path $serviceDir "HikvisionManagerService.exe"
$config = "$exe.config"
$logs = Join-Path $root "logs"
$python = Join-Path $root ".venv\Scripts\python.exe"
$csc = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Execute este script em um PowerShell aberto como Administrador."
}

if (-not (Test-Path $python)) {
    throw "Python nao encontrado: $python. Crie .venv e instale requirements.txt antes de instalar o servico."
}

New-Item -ItemType Directory -Force -Path $serviceDir, $logs | Out-Null
& $python -c "from cryptography.fernet import Fernet"
if ($LASTEXITCODE -ne 0) { throw "Dependencias ausentes. Instale requirements.txt no ambiente Python." }

$existing = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Parando servico antes de atualizar o executavel..."
    Stop-Service -Name $serviceName -ErrorAction Stop
    $existing.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(30))
}

if (Test-Path $csc) {
    Write-Host "Compilando servico..."
    & $csc /nologo /target:exe /out:$exe /reference:System.ServiceProcess.dll /reference:System.Configuration.dll $source
    if ($LASTEXITCODE -ne 0) { throw "Falha ao compilar servico. O servico permanece parado." }
} elseif (-not (Test-Path $exe)) {
    throw "Compilador nao encontrado e executavel do servico nao existe: $exe"
} else {
    Write-Host "Usando executavel do servico ja existente."
}

$document = New-Object System.Xml.XmlDocument
$configuration = $document.AppendChild($document.CreateElement("configuration"))
$settings = $configuration.AppendChild($document.CreateElement("appSettings"))
$values = @{
    RootPath = $root
    PythonPath = $python
    AppPath = (Join-Path $root "backend\app.py")
    LogPath = (Join-Path $logs "hikvision-manager-service.log")
}
foreach ($key in $values.Keys) {
    $entry = $document.CreateElement("add")
    $entry.SetAttribute("key", $key)
    $entry.SetAttribute("value", $values[$key])
    $settings.AppendChild($entry) | Out-Null
}
$document.Save($config)

if ($existing) {
    sc.exe config $serviceName binPath= "`"$exe`"" start= auto | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Falha ao atualizar configuracao do servico." }
} else {
    Write-Host "Criando servico Windows..."
    New-Service -Name $serviceName -DisplayName $displayName -BinaryPathName "`"$exe`"" -StartupType Automatic | Out-Null
}
sc.exe failure $serviceName reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Falha ao configurar recuperacao do servico." }

Write-Host "Iniciando servico..."
Start-Service -Name $serviceName

Write-Host "Servico instalado e iniciado."
Write-Host "Confira no log a URL efetiva e se o servidor iniciou sem erros."
Write-Host "Log: $(Join-Path $logs "hikvision-manager-service.log")"
