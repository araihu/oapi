# Araihu OpenAPI

Catálogos das APIs da organização em **https://oapi.araihu.com**, gerados com
[Manja](https://github.com/araihu/manja). Cada projeto tem um catálogo; APIs
públicas, internas e locais são specs independentes dentro dele.

## Convenção de publicação

O arquivo [`catalogs.json`](catalogs.json) declara os repositórios, a branch
de desenvolvimento, a raiz dos contratos e os entrypoints de cada spec.

| Publicação | Origem | Nome na interface |
| --- | --- | --- |
| Desenvolvimento | `developmentRef`, por exemplo `refs/heads/main` | Nome real da branch; `developmentName` opcional por projeto ou spec |
| Release | `refs/tags/openapi/<spec>/vX.Y.Z` | Versão SemVer da spec |
| Prerelease | `refs/tags/openapi/<spec>/vX.Y.Z-rc.1` | Versão identificada como prerelease |
| Latest | Maior release estável da mesma spec | Marcador na release; sem cópia do documento |

`info.version` precisa corresponder à versão da tag, sem o `v`. Tags gerais do
produto e branches não declaradas não publicam versões. Tags e bundles publicados
são imutáveis: mover ou excluir uma tag interrompe a atualização e preserva o
site anterior. Releases públicas e internas evoluem separadamente.

Exemplo, a partir de um commit cujo contrato público declara `info.version: 1.2.3`:

```sh
git tag -a openapi/public/v1.2.3 COMMIT -m 'Public API 1.2.3'
git push origin refs/tags/openapi/public/v1.2.3
```

SemVer com metadados de build (`+...`) ainda não faz parte desta convenção.
IDs de projeto/spec usam letras minúsculas, números e hífen, até 25 caracteres.
Um nome de versão deve caber na chave de documento do Manja, de até 64 caracteres.

## Projetos publicados

| Projeto | Specs | Raiz | Branch |
| --- | --- | --- | --- |
| Balemoh | `public` | `api` | `main` |
| Manja | `management` (internal) | `api` | `main` |
| Mocó | `public`, `internal` | `openapi` | `main` |
| Talaria-Mem | `control` (local) | `api/openapi` | `main` |
| Xisnove | `public` | `api` | `main` |

Os contratos de demonstração da PoC não são publicados neste domínio.
Para adicionar um projeto ou spec, altere o manifesto e abra um PR.

## Atualização automática

O workflow [Publish OpenAPI catalogs](.github/workflows/publish.yml):

1. Consulta refs Git em pushes neste repositório, por `workflow_dispatch` ou
   `repository_dispatch` do tipo `specs-changed`. Um Cron Trigger do Cloudflare
   solicita reconciliações a cada cinco minutos, independentemente do scheduler
   do GitHub.
2. Compara as refs, configuração e ferramentas com a publicação persistida.
   Quando nada mudou, atualiza a saúde sem instalar Go/Node ou gerar o site.
3. Fixa objetos Git e commits antes de capturar os arquivos declarados.
   Resolve apenas referências locais dentro da raiz de contratos do projeto.
4. Executa os testes, gera bundles com Redocly e exporta com Manja.
5. Executa `manja export verify` e persiste os registros de releases e os assets
   imutáveis antes de promover a nova versão do Worker.
6. Confere páginas, busca e downloads no domínio público, verifica o fingerprint
   e registra o resultado. Um erro de aquisição, validação ou exportação mantém
   a publicação anterior. Se a validação depois do deploy falhar, restaura as
   versões anteriores do Worker. As refs aceitas continuam persistidas e a
   próxima execução tenta publicar novamente.

Os gatilhos diretos dos produtores são o caminho principal de atualização.
O cron do Cloudflare reconcilia as refs periodicamente mesmo quando uma
notificação do produtor não chega. O workflow de publicação não contém mais
um agendamento do GitHub.

### Notificação pelo repositório produtor

Manja, Balemoh, Mocó, Talaria-Mem e Xisnove notificam o catálogo quando a branch
`main` altera a raiz de contratos declarada no manifesto, uma tag `openapi/**`
é enviada ou uma release de spec é publicada. O workflow também aceita disparo
manual para retries. Releases de produto sem o prefixo `openapi/` são ignoradas.

Os quatro novos produtores chamam o
[notificador reutilizável](.github/workflows/notify-producer.yml), fixado por SHA.
Ele envia `workflow_dispatch` com `force: false`: o publicador reconsulta as refs
canônicas do manifesto e decide se precisa publicar. O Manja mantém seu
notificador equivalente já validado em produção.

O token é emitido pelo GitHub App de distribuição e dura apenas a execução do
notificador. Seu escopo é restrito a Actions write em `araihu/oapi`; a notificação
não exige credenciais do Cloudflare. A instalação do App deve incluir `oapi`,
e o produtor deve disponibilizar `ARAIHU_ASSETS_APP_ID` e
`ARAIHU_ASSETS_APP_PRIVATE_KEY` como secrets. Os secrets de organização usados
por Balemoh, Mocó, Talaria-Mem e pelo publicador `oapi` têm acesso limitado
a esses quatro repositórios.

Para adicionar um produtor, copie seu `notify-oapi.yml` de um desses projetos,
ajuste a branch e o filtro de paths para a raiz declarada em `catalogs.json` e
mantenha o workflow reutilizável fixado por SHA. Mudanças na própria definição
do notificador também disparam reconciliação. Instale o workflow antes de criar
novas tags; uma reconciliação inicial importa tags anteriores à instalação.

A [validação de produção de 2026-11-01](validation/2026-11-01-management-release.json)
comprovou atualizações por branch e tag, promoção do `latest`, imutabilidade da
release anterior e retenção dos arquivos históricos. O cron permaneceu ativo,
sem execução `schedule` observada nessa janela; o bootstrap de `0.1.0` foi manual
e os fluxos seguintes foram disparados automaticamente pelo App.

### Fallback no Cloudflare

O `scheduled` do Worker `araihu-oapi` usa `*/5 * * * *`, definido no
[`wrangler.jsonc`](wrangler.jsonc). O timer é do Cloudflare; a execução de Go,
Redocly e Manja permanece no GitHub Actions. Uma indisponibilidade da API ou
execução do GitHub será registrada e tentada novamente no próximo intervalo.
Os gatilhos diretos dos produtores continuam ativos.

Cada execução emite um token temporário do GitHub App, restrito a Actions write
em `araihu/oapi`, e consulta publicações ativas ou na fila. Se houver alguma,
registra `busy` e deixa essa execução terminar. Caso contrário, dispara
`publish.yml` na `main` com `force: false` e um identificador
`cloudflare-cron-<scheduledTime>`, visível no nome da execução do GitHub.
O token é revogado ao terminar. Quando as refs não mudaram, a reconciliação
apenas atualiza a saúde, sem gerar outra versão do site.

[`/reconciliation.json`](https://oapi.araihu.com/reconciliation.json) mostra a
última tentativa, seu horário agendado, resultado (`dispatched`, `busy` ou
`failed`) e o último disparo aceito. `dispatched` confirma o aceite pelo GitHub;
o resultado da publicação continua em
[`/status.json`](https://oapi.araihu.com/status.json). Ambos são somente leitura
e usam `Cache-Control: no-store`. Logs do Worker registram os mesmos eventos
sem tokens ou chaves.

O deploy de produção converte a chave RSA do App para PKCS#8 em um arquivo
privado de `RUNNER_TEMP` e entrega os dois secrets ao Cloudflare junto com o
código, usando `wrangler deploy --secrets-file`. O arquivo é removido mesmo em
caso de falha. `GITHUB_APP_ID` e `GITHUB_APP_PRIVATE_KEY` são bindings secretos;
o ID da instalação é uma variável declarada. A chave não é necessária para
servir as páginas e não há endpoint HTTP para disparar reconciliações.
Após rotacionar os secrets do App, execute o workflow com `force: true` para
entregar as novas credenciais ao Worker.

Para pausar o fallback, altere `triggers.crons` para `[]` e publique pelo
workflow; restaure `*/5 * * * *` para retomá-lo. Mudanças de Cron Triggers podem
levar até 15 minutos para propagar no Cloudflare. Os testes locais usam
`native_smoke.py` para invocar o handler real sem credenciais de produção.

A [investigação anterior](validation/2026-11-01-producer-notifications.json)
registrou 45 minutos sem eventos no controle do GitHub e ausência de runs do
publicador após reativação às 14:12 UTC. Mais tarde, houve um disparo tardio do
GitHub às 18:56 UTC. O fallback foi transferido para o Cloudflare e o workflow
de diagnóstico antigo permanece desabilitado.

As execuções são serializadas e não cancelam uma publicação em andamento.
Credenciais de publicação só são usadas pelo workflow de produção em `main`.
PRs executam os mesmos testes sem acesso a credenciais de infraestrutura.

## Hospedagem e persistência

Cloudflare Workers Static Assets serve o site atual. O Worker resolve aliases,
expõe a saúde e atende arquivos imutáveis antigos quando uma aba aberta ainda
referencia um snapshot anterior. Nenhum servidor Manja é necessário em runtime.

O banco D1 `araihu-oapi-publications` mantém:

- `metadata`: registros aceitos, publicação efetiva e estado de saúde;
- `paths` e `blobs`: snapshots, índices de busca e marcas anteriores, com bytes
  comprimidos em gzip e deduplicados pelo SHA-256 original;
- uma restrição de imutabilidade que impede substituir o conteúdo de um caminho.

O histórico só atende caminhos imutáveis reconhecidos. HTML mutável não pode ser
recuperado de uma publicação antiga. O site atual usa diretamente os assets
estáticos; o D1 atende apenas saúde e fallback histórico. Não há coleta automática
do histórico. O limite de armazenamento do plano D1 deve ser acompanhado no
Cloudflare; exceder a capacidade bloqueia novas publicações sem apagar a atual.
Cada arquivo histórico comprimido/base64 deve caber em 1,8 MB.

A branch `publication-state` recebe um backup dos registros após cada alteração
publicada e, sem alterações, a cada 25 dias. Esses commits mantêm um backup
periódico mesmo quando as refs dos projetos não mudam.
O cache do CI não é fonte de verdade para releases nem histórico.

O export original em `.state/current/public` permanece intacto e verificável.
A árvore `.state/deploy` acrescenta apenas os metadados públicos e `_headers`
necessários à hospedagem.

### URLs

- `/catalogs/<projeto>/`: catálogo do projeto;
- `/catalogs/<projeto>/specs/<spec>/branches/main/`: alias para a branch declarada;
- `/catalogs/<projeto>/specs/<spec>/latest/`: alias da maior release estável;
- `/catalogs/<projeto>/specs/<spec>/versions/1.2.3/`: release específica;
- `/catalogs/<projeto>/documents/<spec>-next/`: rota legada de desenvolvimento,
  preservada por compatibilidade; a interface mostra `main`, `master` ou o nome declarado;
- `/publications.json`: refs, commits, digests e aliases da publicação;
- `/status.json`: última tentativa, saúde e indicação de publicação anterior preservada;
- `/reconciliation.json`: última tentativa e último disparo do fallback do Cloudflare.

Aliases de branch/latest não são cacheados. Snapshots e índices com hash recebem
cache imutável; páginas atuais revalidam conforme o padrão dos Static Assets.

### Regras de entrega no Cloudflare

[`cloudflare-rules.json`](cloudflare-rules.json) registra as regras aplicadas à zona.
Para `GET`/`HEAD` neste hostname, Browser Integrity Check é desativado para permitir
clientes OpenAPI automatizados; o limite legado de renderização dinâmica de
catálogos também exclui essas leituras estáticas. As demais regras e domínios
preservam a configuração anterior.

RUM, ofuscação de email, reescrita de URLs e minificação automática são desativados
nessas respostas para preservar os bytes verificados pelo Manja. Os headers
`no-transform` reforçam esse contrato. O smoke de produção compara os hashes do
HTML, dos índices de busca e das fontes com o export original. Aplique essas
regras na provisão da zona; o token do publicador não recebe permissão para alterar
as políticas globais de segurança.

## Ferramentas fixadas

- Manja: `56318e36ec4d1afff6bf4592e38645ed75df78be`;
- Go: `1.27.3`, `GOEXPERIMENT=jsonv2`, build com `-trimpath`;
- Redocly: `2.65.0`;
- Wrangler: `4.156.0`;
- Compatibilidade Workers: `2026-10-29`, suportada pelo runtime local fixado;
- PyYAML: `6.0.3`;
- Node: linha `24`; Python: linha `3.13` no CI.

### Execução local

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
bash setup.sh
printf '{}\n' > aliases.json
python3 -m unittest -v
npm test
python3 native_smoke.py
```

O teste nativo inicia um Worker com um D1 temporário e verifica os assets atuais,
o fallback histórico, aliases, caminhos Unicode e respostas `HEAD`/`304`. Ele não
acessa o banco de produção e também é executado pelo CI antes da publicação.

Para gerar e servir localmente, sem escrever no Cloudflare:

```sh
python3 publisher.py serve --port 8787
```

Para publicar, use o workflow ou configure `CLOUDFLARE_ACCOUNT_ID` e
`CLOUDFLARE_API_TOKEN` em um ambiente confiável e execute `python3 deploy.py deploy`.
O token precisa publicar Workers e domínios personalizados e ler/escrever D1.
Não armazene tokens em arquivos versionados. A variável de organização contém a
conta. O segredo `CLOUDFLARE_API_TOKEN` é próprio deste repositório, com acesso
a Workers/D1 na conta de publicação e rotas na zona `araihu.com`.

### Recuperação

Corrija o contrato ou a configuração e reexecute o workflow. Não mova uma tag
publicada: publique outra versão. Se o deploy falhou depois de aceitar as refs,
a próxima execução retoma a publicação automaticamente.

Para inspecionar a última falha, consulte `/status.json` e a execução do workflow.
Para forçar uma nova geração após uma mudança operacional, use o input `force`
do `workflow_dispatch`. Restaurações de banco devem preservar os registros das
tags já publicadas; o backup em `publication-state` e o Time Travel do D1 auxiliam
a recuperação. Não use limpeza do banco ou do histórico como procedimento de retry.
