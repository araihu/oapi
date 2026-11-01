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

1. Consulta refs Git a cada cinco minutos, em pushes neste repositório, por
   `workflow_dispatch` ou `repository_dispatch` do tipo `specs-changed`.
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

O agendamento do GitHub é best effort, portanto cinco minutos não é um SLA.
Um fluxo upstream pode enviar `repository_dispatch` para reduzir a latência;
o polling funciona sem modificar os repositórios dos projetos.

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
publicada e, sem alterações, a cada 25 dias. Esses commits também mantêm a
atividade do repositório para o GitHub não desativar o agendamento por inatividade.
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
- `/status.json`: última tentativa, saúde e indicação de publicação anterior preservada.

Aliases de branch/latest não são cacheados. Snapshots e índices com hash recebem
cache imutável; páginas atuais revalidam conforme o padrão dos Static Assets.

## Ferramentas fixadas

- Manja: `56318e36ec4d1afff6bf4592e38645ed75df78be`;
- Go: `1.27.3`, `GOEXPERIMENT=jsonv2`, build com `-trimpath`;
- Redocly: `2.65.0`;
- Wrangler: `4.156.0`;
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
```

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
