# agm-miner

Pipeline de mineração de repositórios do GitHub para o estudo *Investigação Empírica de Artefatos
de Configuração para Agentes de IA*. Ele coleta `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md` e
Skills do Claude (`.claude/skills/**`) com todo o histórico de cada arquivo, e gera datasets
Parquet prontos para as análises das questões de pesquisa.

| Questão | O que o pipeline produz |
|---|---|
| **RQ1 — Conteúdo** | Seções (com títulos H1/H2/H3 e texto) de cada artefato no HEAD, prontas para codificação qualitativa |
| **RQ2 — Estrutura** | Palavras, profundidade de headings, blocos de código, listas, tabelas, links, legibilidade (Flesch), idioma e chaves do frontmatter YAML |
| **RQ3 — Evolução** | Todas as mudanças de cada artefato (inclusive criação, remoção e renomeação), intervalo entre mudanças, picos (<24h), seções e campos adicionados/removidos, arquivos alterados no mesmo commit (gatilhos), commits coescritos por agentes e perfil de quem alterou |
| **Correlação** | Métricas do repositório (stars, forks, commits, contribuidores, atividade mensal) junto com atributos agregados dos artefatos |

## Como funciona

```
agm discover → agm select → agm mine → agm export → agm analyze
 Code Search    GraphQL      git clone   Parquet       tabelas por RQ
 (candidatos)   (critérios)  (histórico) (bronze)      (silver/gold)
```

1. **discover**: busca arquivos na GitHub Code Search. O limite de 1000 resultados por query é
   contornado com bisseção do intervalo de tamanho (`size:`), e cada achado é registrado com a
   query que o encontrou.
2. **select**: busca os metadados dos repositórios via GraphQL e aplica os critérios do
   `study.yaml` (estrelas, commits, atividade, forks, arquivados, templates, tutoriais/listas). Os
   rejeitados guardam o motivo, o que gera o funil de seleção do artigo (`agm runs funnel`).
3. **mine**: faz um clone parcial (sem blobs) de cada repositório selecionado e extrai o histórico
   completo dos artefatos, o conteúdo de cada versão, renomeações/cópias, os arquivos alterados em
   cada commit, trailers `Co-authored-by` e o perfil de atividade de cada contribuidor. A
   mineração é idempotente, incremental, pode rodar em paralelo e registra falhas.
4. **export**: grava todas as tabelas em `data/bronze/<timestamp>/` em Parquet, com um
   `manifest.json` (versão do código, do schema e da configuração do estudo).
5. **analyze**: a partir de um export bronze, sem precisar do banco, gera em
   `data/gold/<timestamp>/` as tabelas `rq1_sections`, `rq2_structure`, `rq3_changes`,
   `rq3_author_roles` e `repo_features`.

Toda execução fica registrada em `pipeline_runs`, com parâmetros, estatísticas, o snapshot do
`study.yaml` e o commit do código. Isso garante a rastreabilidade do dataset.

## Como rodar localmente

Pré-requisitos: Python 3.11, Poetry, Docker, git ≥ 2.44 e um token do GitHub com acesso de
leitura.

```bash
cp .env.sample .env                  # preencha GITHUB_TOKEN e DATABASE_URL
poetry env use /usr/bin/python3.11   # evita que o Poetry tente usar Python 3.13
poetry install
docker compose up -d db
poetry run alembic upgrade head
```

Executando o pipeline:

```bash
poetry run agm discover
poetry run agm select
poetry run agm mine --workers 4
poetry run agm export
poetry run agm analyze
poetry run agm runs funnel           # funil de seleção
poetry run agm runs list             # histórico de execuções
```

Para testar com poucos repositórios, registre-os à mão e minere só eles:

```bash
poetry run agm add-repo openai/codex anthropics/claude-code-action
poetry run agm mine --repo openai/codex --repo anthropics/claude-code-action
```

## Configuração do estudo

Os parâmetros metodológicos ficam em [study.yaml](study.yaml): queries, critérios de seleção,
limites de mineração, padrões de bots e agentes, janela de pico. Mude os valores ali, não no
código. **Antes da coleta final, fixe `selection.reference_date`**, senão o critério de atividade
depende da data em que o comando rodou.

## Testes

```bash
poetry run pytest
poetry run ruff check . && poetry run ruff format --check .
```

Os testes de integração usam o Postgres do `docker compose`. Eles criam e recriam sozinhos o
banco `agm_test` (configurável por `TEST_DATABASE_URL`) e mineram repositórios git sintéticos
criados durante o teste. O CI (GitHub Actions) roda lint e testes em cada PR.

## Limitações

- A Code Search do GitHub indexa só o branch default e arquivos menores que 384 KB, e seus
  resultados não são determinísticos.
- As heurísticas de tutorial/lista, bots, agentes e papéis de autor devem ser validadas
  manualmente numa amostra.
- A legibilidade (Flesch) só é calculada para textos em inglês.

Mais detalhes técnicos estão em [AGENTS.md](AGENTS.md).
