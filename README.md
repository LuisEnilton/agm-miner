# agm-miner

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.24401086.svg)](https://doi.org/10.5281/zenodo.24401086)

Ferramenta de mineração de repositórios do GitHub do estudo *O que os desenvolvedores dizem aos
seus agentes de código? Um estudo empírico do conteúdo e da estrutura de `CLAUDE.md`, `AGENTS.md`
e Skills*. Ela coleta `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md` e Skills do Claude
(`.claude/skills/**`), com todo o histórico de cada arquivo, e gera datasets Parquet para as
questões de pesquisa:

| Questão | O que a ferramenta produz |
|---|---|
| **RQ1: conteúdo** | Seções de cada artefato, classificadas nas 16 categorias do *Agent READMEs* por um LLM e validadas contra rótulos humanos |
| **RQ2: estrutura** | Extensão, títulos, blocos de código, listas, tabelas, links, legibilidade (Flesch), idioma e frontmatter YAML das Skills |
| **RQ3: evolução** | Todas as mudanças de cada artefato, intervalos e picos, seções adicionadas e removidas, arquivos alterados junto, commits coescritos por agentes e perfil de quem alterou |
| **Correlação** | Métricas de cada repositório (estrelas, commits, contribuidores, atividade) com os atributos agregados dos artefatos |

## Como citar

O DOI da ferramenta e o do pacote de replicação, com os dados e os resultados do artigo, entram
aqui quando forem publicados no Zenodo.

## Como funciona

```
agm discover → agm select → agm mine → agm export → agm analyze → agm rq1 / rq2 / audit / figures
 Code Search    GraphQL      git clone   Parquet       tabelas       resultados do artigo
 (candidatos)   (critérios)  (histórico) (bronze)      (silver/gold) (sem banco)
```

A coleta (`discover`, `select`, `mine`) grava fatos brutos num Postgres e pode ser interrompida e
retomada a qualquer momento. O `export` gera um retrato do banco em Parquet (o *bronze*), que é a
unidade de replicação: tudo o que vem depois lê só esses arquivos, sem banco e sem rede. Toda
execução registra a versão do código e um retrato do [study.yaml](study.yaml), onde ficam todas as
decisões metodológicas (queries, critérios de seleção, padrões de bots e agentes, modelos da RQ1).

## Instalação rápida

Com Docker (só o Docker na máquina):

```bash
AGM_CODE_VERSION=$(git rev-parse HEAD) docker compose build agm
docker compose run --rm agm help
```

Com Poetry (Python 3.11 e git 2.44 ou mais novo):

```bash
cp .env.sample .env                  # preencha GITHUB_TOKEN
poetry install
docker compose up -d db
poetry run alembic upgrade head
poetry run agm help
```

Detalhes, variáveis de ambiente e testes em [docs/instalacao.md](docs/instalacao.md).

## Primeiros passos

Minerar um repositório escolhido à mão, do registro até as tabelas de análise:

```bash
poetry run agm add-repo anthropics/claude-code-action   # metadados e critérios (precisa do token)
poetry run agm mine --repo anthropics/claude-code-action
poetry run agm export                                   # data/bronze/<timestamp>/
poetry run agm analyze                                  # data/gold/<timestamp>/
```

A coleta completa do estudo é `poetry run agm run --workers 4`, que busca, seleciona e minera até
esvaziar a fila e depois exporta e analisa ([docs/pipeline.md](docs/pipeline.md)).

## Reproduzir os resultados do artigo

Com os dados publicados, as tabelas e figuras do artigo são refeitas em minutos, sem banco e sem
chave de API: a RQ1 sai das respostas do LLM guardadas em cache
(`agm rq1 classify --offline`). O passo a passo, nos três níveis (dos resultados, do export bronze
ou coletando de novo), está em [docs/replicacao.md](docs/replicacao.md).

## Documentação

| Documento | Conteúdo |
|---|---|
| [docs/instalacao.md](docs/instalacao.md) | Docker, Poetry, variáveis de ambiente, banco e testes |
| [docs/pipeline.md](docs/pipeline.md) | Os estágios; parar e retomar; vários terminais; progresso; `agm run` |
| [docs/cli.md](docs/cli.md) | Todos os comandos, por painel |
| [docs/configuracao.md](docs/configuracao.md) | O `study.yaml`: critérios de amostragem, datas de corte, licenças |
| [docs/rq1.md](docs/rq1.md) | Classificação de conteúdo com LLM e validação externa |
| [docs/rq2.md](docs/rq2.md) | Tabelas de estrutura |
| [docs/auditoria-e-figuras.md](docs/auditoria-e-figuras.md) | Auditoria dos vieses da amostra e figuras do artigo em LaTeX |
| [docs/dados.md](docs/dados.md) | Dicionário de dados de todas as tabelas |
| [docs/replicacao.md](docs/replicacao.md) | Como reproduzir os resultados |
| [docs/limitacoes.md](docs/limitacoes.md) | Ameaças à validade |
| [AGENTS.md](AGENTS.md) | Arquitetura, princípios de projeto e guia para desenvolver (inclusive com agentes de código) |

## Licença e citação

O código está sob a licença MIT ([LICENSE](LICENSE)). Cada versão é arquivada no Zenodo: o DOI
[10.5281/zenodo.24401086](https://doi.org/10.5281/zenodo.24401086) aponta sempre para a mais
recente, e a página do registro lista o DOI de cada versão. Para citar a ferramenta, use os
metadados de [CITATION.cff](CITATION.cff) (o GitHub mostra a referência em "Cite this
repository"). As mudanças de cada versão estão em [CHANGELOG.md](CHANGELOG.md).
