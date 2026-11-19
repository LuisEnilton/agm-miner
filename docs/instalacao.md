# Instalação

Há dois caminhos: **Docker** (nada além do Docker na máquina) ou **Poetry** (Python local, para
desenvolver). Os dois usam o mesmo Postgres do `docker-compose.yml`.

O que cada tarefa precisa:

| Tarefa | Postgres | Token do GitHub | Chave de LLM |
|---|---|---|---|
| Coletar (`discover`, `select`, `add-repo`, `mine`, `run`) | sim | sim (`discover`, `select`, `add-repo`) | não |
| Exportar (`export`), acompanhar (`status`, `runs`) | sim | não | não |
| Analisar (`analyze`, `audit`, `rq2 summary`, `figures`) | não | não | não |
| Reproduzir a RQ1 a partir do cache (`rq1 classify --offline`) | não | não | não |
| Classificar de novo com o LLM (`rq1 classify`) | não | não | sim (`ANTHROPIC_API_KEY`) |

Para só reproduzir os resultados do artigo a partir dos dados publicados, veja
[replicacao.md](replicacao.md): não é preciso banco nem chave.

## Com Docker

Pré-requisito: Docker com o plugin Compose (v2.24 ou mais novo).

```bash
cp .env.sample .env                                # opcional: só a coleta precisa do GITHUB_TOKEN
AGM_CODE_VERSION=$(git rev-parse HEAD) docker compose build agm
docker compose run --rm agm help                   # lista os comandos
```

A imagem traz o código, o `study.yaml`, os codebooks e as migrações. A pasta `./data` do
repositório aparece como `/data` no container, e os arquivos gravados nela ficam com o seu usuário
(`AGM_UID`/`AGM_GID`, padrão 1000). O commit passado em `AGM_CODE_VERSION` fica registrado em cada
execução e manifesto, porque a imagem não tem `.git`.

Para coletar, suba o banco e crie o schema antes:

```bash
docker compose up -d db
docker compose run --rm --entrypoint alembic agm upgrade head
docker compose run --rm agm add-repo openai/codex
```

Os comandos de análise não precisam do banco:

```bash
docker compose run --rm agm analyze
docker compose run --rm agm figures
```

O Gemini (usado só no piloto) não vem na imagem: construa com
`docker compose build --build-arg EXTRAS=gemini agm` para incluí-lo.

## Com Poetry

Pré-requisitos: Python 3.11, [Poetry](https://python-poetry.org/) 2, Docker (para o Postgres) e
git 2.44 ou mais novo (o minerador usa `--no-lazy-fetch`).

```bash
cp .env.sample .env                  # preencha GITHUB_TOKEN; DATABASE_URL já aponta para o compose
poetry env use /usr/bin/python3.11   # evita que o Poetry tente usar Python 3.13
poetry install                       # -E gemini acrescenta o SDK do Gemini (piloto)
docker compose up -d db
poetry run alembic upgrade head
poetry run agm help
```

Os dados do Postgres ficam no volume Docker `pgdata`. `docker compose down` mantém o volume;
**`docker compose down -v` apaga tudo**.

## Variáveis de ambiente

Ficam no `.env` (modelo em [.env.sample](../.env.sample)) ou no ambiente. Infraestrutura e
segredos ficam aqui; as decisões metodológicas ficam no [study.yaml](../study.yaml)
([configuracao.md](configuracao.md)).

| Variável | Para quê | Padrão |
|---|---|---|
| `GITHUB_TOKEN` | Busca e metadados (`discover`, `select`, `add-repo`); leitura pública basta | — |
| `DATABASE_URL` | Postgres; só os comandos que usam o banco exigem | — |
| `ANTHROPIC_API_KEY` | RQ1 com o Claude (ou `ant auth login`) | — |
| `GEMINI_API_KEY`, `OPENROUTER_API_KEY` | Provedores do piloto | — |
| `DATA_DIR` | Exports, análises e resultados | `data` |
| `CLONE_DIR` | Clones temporários da mineração | `data/clones` |
| `STUDY_CONFIG_PATH` | Configuração do estudo | `study.yaml` |
| `GIT_TIMEOUT_SECONDS` | Limite de cada comando git | `1800` |
| `AGM_PROGRESS` | Painel de progresso: `auto`, `live`, `plain` ou `off` | `auto` |
| `TEST_DATABASE_URL` | Banco da suíte de testes (recriado pelos testes) | `agm_test` |

As demais (`*_RATE_LIMIT_*`, `GITHUB_MAX_RETRIES`, `LOG_LEVEL`) estão comentadas no
`.env.sample`.

## Banco criado por uma versão antiga

Todo comando confere, antes de começar, se o banco está na revisão de schema que o código espera.
Se não estiver, o comando para com uma mensagem como:

```
error: Cannot use postgresql+psycopg2://...: the database is at schema revision 20260719_0001,
which this code does not know (it was created by an incompatible version of agm-miner)...
```

- **Revisão conhecida, mas antiga** (ex.: `0002` com o código em `0003`): basta
  `poetry run alembic upgrade head`.
- **Revisão desconhecida** (banco da versão anterior à refatoração, com status `PENDING` e
  `COMPLETED`): o schema é incompatível e não há migração. Preserve o banco antigo com outro nome e
  crie um novo:

  ```bash
  docker exec msr_research_db psql -U research_user -d postgres \
    -c "ALTER DATABASE agent_artifacts_research RENAME TO agent_artifacts_research_legacy" \
    -c "CREATE DATABASE agent_artifacts_research"
  poetry run alembic upgrade head
  ```

  O banco `*_legacy` continua disponível para consulta. Para apagá-lo de vez:
  `DROP DATABASE agent_artifacts_research_legacy`.

## Testes

```bash
poetry run pytest
poetry run ruff check . && poetry run ruff format --check .
```

Os testes de integração usam o Postgres do `docker compose`. Eles criam e recriam sozinhos o
banco `agm_test` (configurável por `TEST_DATABASE_URL`) e mineram repositórios git sintéticos
criados durante o teste. O CI (GitHub Actions) roda lint e testes em cada PR.

Para trabalhar em duas branches ao mesmo tempo (por exemplo, duas sessões de agente), use um
`git worktree` por branch, cada um com o seu `.env` e o seu `TEST_DATABASE_URL`. O teste lê essa
variável do ambiente ou, se não estiver lá, do `.env` da própria pasta; com o mesmo banco, duas
suítes rodando juntas apagam as tabelas uma da outra. Se o terminal tiver um ambiente virtual
ativado (`VIRTUAL_ENV`), rode `env -u VIRTUAL_ENV poetry install` no worktree novo, senão o Poetry
instala o pacote no ambiente da outra pasta.
