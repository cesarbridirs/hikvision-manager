# Hikvision Manager

Aplicacao web para inventario de NVRs Hikvision, descoberta de canais, verificacao
de gravacoes e discos, alertas e historico. O backend usa Python e SQLite; a
interface e servida pelo proprio backend.

Este repositorio contem somente codigo e testes. Nao inclui NVRs cadastrados,
enderecos da rede, bancos, senhas, chaves, certificados, logs ou resultados reais.

## Instalacao local (Windows)

Requer Python 3.12 e PowerShell. Na pasta do projeto:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\inicializar-admin.ps1
.\iniciar-servidor.ps1
```

Abra `http://127.0.0.1:8088`. O preparo do administrador pede uma senha inicial
sem grava-la em script ou passar pela linha de comando. O primeiro login exige
troca dessa senha. Nao exponha o servidor na rede antes de concluir esse passo.

O servidor escuta apenas em `127.0.0.1` por padrao. Para acesso remoto, prefira
um proxy reverso com HTTPS e controle de acesso; use `HIKVISION_BIND_HOST` para
alterar o endereco de escuta somente depois de configurar TLS e a senha admin.
Em PowerShell, um exemplo para a sessao atual e
`$env:HIKVISION_BIND_HOST = '0.0.0.0'`. Proteja o acesso com firewall.

## Funcionalidades

- Cadastro e importacao de NVRs pela interface.
- Descoberta de canais, IPs de cameras e estado online/offline.
- Verificacao de gravacoes recentes e HDDs, com agenda automatica.
- Alertas de camera online sem gravacao, alteracao de IP, erro de consulta e HDD.
  Offline e visivel no inventario, mas nao gera item em Alertas Atuais.
- Usuarios de visualizacao limitados as cidades escolhidas pelo administrador.

As credenciais dos NVRs sao cifradas em `data/hikvision.db` com a chave local
`secrets/credentials.key`. Preserve banco, arquivos WAL/SHM e chave juntos em
backups protegidos. A chave nao esta neste repositorio e nao pode ser recriada
para decifrar um banco existente.

## HTTPS embutido (opcional)

Em Windows, para uma instalacao nova e com o servidor parado:

```powershell
.\gerar-certificado-local.ps1
.\habilitar-https.ps1
.\iniciar-servidor.ps1
```

O servidor passa a usar `https://localhost:8443`. O certificado local e
autoassinado e pode nao cobrir o nome publico do servidor. Para producao, use
um certificado emitido para o nome correto ou um proxy TLS confiavel. Nunca
publique a chave privada do certificado.

## Servico Windows (opcional)

Depois de criar `.venv`, instalar as dependencias e inicializar o administrador,
execute como Administrador:

```powershell
.\instalar-servico.ps1
.\status-servico.ps1
```

O instalador compila `ServicoWindows/HikvisionManagerService.cs` com o compilador
.NET Framework do Windows, cria a configuracao local e inicia o servico. O
executavel e a configuracao gerados ficam fora do Git. Para remover, use
`.\remover-servico.ps1`. Nao execute uma segunda instancia do backend contra
o mesmo banco.

## Testes

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Os testes Python usam bancos temporarios e nao acessam NVRs reais. Os testes de
navegador em `tests/workspace.test.cjs` requerem Node.js e Playwright instalados
separadamente.

## Antes de publicar

Confira `git status --short` e `git ls-files`. Nao adicione `data`, `secrets`,
`certs`, `logs`, `runtime`, exports ou configuracoes do servico. A pasta de
producao nunca deve ser enviada ao Git; atualize-a com arquivos de codigo,
preservando seus dados e chaves no servidor.

## Licenca

MIT em [LICENSE](LICENSE). O bundle de icones Lucide tem sua propria licenca em
`frontend/lucide.LICENSE`.
