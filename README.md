# agm-miner

FastAPI service for mining GitHub repositories and exporting analytical datasets.

## Como rodar localmente

### 1. Pré-requisitos

- Python 3.11+
- Poetry
- Docker e Docker Compose
- Um token do GitHub com acesso de leitura

### 2. Configure o ambiente

Copie o arquivo de exemplo e ajuste os valores necessários:

```bash
cp .env.sample .env
```

Preencha pelo menos estas variáveis em `.env`:

- `GITHUB_TOKEN`
- `DATABASE_URL`

### 3. Suba o banco local

```bash
docker compose up -d db
```

Se quiser checar se o Postgres ficou pronto:

```bash
docker compose ps
```

### 4. Instale as dependências

Garanta que o Poetry esteja usando Python 3.11 antes de instalar:

```bash
poetry env use /usr/bin/python3.11
```

Use o Poetry para instalar as dependências do projeto:

```bash
poetry install --no-root
```

### 5. Rode as migrations

Quando houver migrations criadas em `alembic/versions`, aplique o schema no banco:

```bash
poetry run alembic upgrade head
```

### 6. Inicie a aplicação

```bash
poetry run agm-miner
```

Se preferir rodar o servidor diretamente com Uvicorn:

```bash
poetry run uvicorn agm_miner.main:app --reload --host 0.0.0.0 --port 8000
```

### 7. Verifique se está no ar

Abra a rota de saúde:

```bash
curl http://localhost:8000/health
```

### 8. Próximos endpoints úteis

- `POST /api/v1/jobs/discover`
- `POST /api/v1/jobs/mine-pending`
- `GET /api/v1/dataset/export`

### Observações

- O repositório foi configurado para usar o pacote importável `agm_miner` sob `src/`.
- Os diretórios `data/` e `dataset/` são criados em tempo de execução e ficam fora do controle de versão.
- Se o Poetry tentar criar o ambiente com Python 3.13, force o uso de `/usr/bin/python3.11` antes de instalar.