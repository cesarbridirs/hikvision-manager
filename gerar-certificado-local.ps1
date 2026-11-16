$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$certDir = Join-Path $root "certs"
$certPath = Join-Path $certDir "server.crt"
$keyPath = Join-Path $certDir "server.key"
$dnsNames = @("localhost", $env:COMPUTERNAME)

New-Item -ItemType Directory -Force -Path $certDir | Out-Null

Write-Host "Gerando certificado local para HTTPS..."
$cert = New-SelfSignedCertificate `
    -DnsName $dnsNames `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -KeyAlgorithm RSA `
    -KeyLength 2048 `
    -KeyExportPolicy Exportable `
    -NotAfter (Get-Date).AddYears(3)

$certBytes = $cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert)
$certPem = "-----BEGIN CERTIFICATE-----`n"
$certPem += [Convert]::ToBase64String($certBytes, [Base64FormattingOptions]::InsertLineBreaks)
$certPem += "`n-----END CERTIFICATE-----`n"
Set-Content -Path $certPath -Value $certPem -Encoding ASCII

$rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($cert)
if (-not $rsa) {
    throw "Nao foi possivel exportar a chave privada do certificado."
}

function ConvertTo-DerLength {
    param([int]$Length)
    if ($Length -lt 128) {
        return [byte[]]@($Length)
    }
    $bytes = New-Object System.Collections.Generic.List[byte]
    $value = $Length
    while ($value -gt 0) {
        $bytes.Insert(0, [byte]($value -band 0xff))
        $value = $value -shr 8
    }
    $result = New-Object System.Collections.Generic.List[byte]
    $result.Add([byte](0x80 -bor $bytes.Count))
    $result.AddRange($bytes)
    return $result.ToArray()
}

function ConvertTo-DerInteger {
    param([byte[]]$Value)
    $start = 0
    while ($start -lt ($Value.Length - 1) -and $Value[$start] -eq 0) {
        $start++
    }
    [byte[]]$trimmed = $Value[$start..($Value.Length - 1)]
    if (($trimmed[0] -band 0x80) -ne 0) {
        [byte[]]$trimmed = [byte[]]@([byte]0) + $trimmed
    }
    $result = New-Object System.Collections.Generic.List[byte]
    $result.Add([byte]0x02)
    [byte[]]$lenBytes = ConvertTo-DerLength $trimmed.Length
    $result.AddRange($lenBytes)
    $result.AddRange($trimmed)
    return $result.ToArray()
}

function ConvertTo-DerSequence {
    param([byte[][]]$Parts)
    $body = New-Object System.Collections.Generic.List[byte]
    foreach ($part in $Parts) {
        $body.AddRange($part)
    }
    $result = New-Object System.Collections.Generic.List[byte]
    $result.Add([byte]0x30)
    [byte[]]$lenBytes = ConvertTo-DerLength $body.Count
    $result.AddRange($lenBytes)
    $result.AddRange($body)
    return $result.ToArray()
}

$p = $rsa.ExportParameters($true)
$zero = [byte[]]@(0)
$keyBytes = ConvertTo-DerSequence @(
    (ConvertTo-DerInteger $zero),
    (ConvertTo-DerInteger $p.Modulus),
    (ConvertTo-DerInteger $p.Exponent),
    (ConvertTo-DerInteger $p.D),
    (ConvertTo-DerInteger $p.P),
    (ConvertTo-DerInteger $p.Q),
    (ConvertTo-DerInteger $p.DP),
    (ConvertTo-DerInteger $p.DQ),
    (ConvertTo-DerInteger $p.InverseQ)
)
$keyPem = "-----BEGIN RSA PRIVATE KEY-----`n"
$keyPem += [Convert]::ToBase64String($keyBytes, [Base64FormattingOptions]::InsertLineBreaks)
$keyPem += "`n-----END RSA PRIVATE KEY-----`n"
Set-Content -Path $keyPath -Value $keyPem -Encoding ASCII

Write-Host "Certificado criado:"
Write-Host "  $certPath"
Write-Host "Chave privada criada:"
Write-Host "  $keyPath"
Write-Host ""
Write-Host "Reinicie o servidor/servico e acesse:"
Write-Host "  https://localhost:8443"
Write-Host ""
Write-Host "Observacao: por ser certificado local, o navegador pode pedir confirmacao na primeira abertura."
Write-Host "Para confiar no certificado no usuario atual, execute:"
Write-Host "  .\confiar-certificado-local.ps1"
