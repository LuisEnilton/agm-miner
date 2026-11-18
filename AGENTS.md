# AGENTS.md — guia do `agm-miner` para agentes de código

Pipeline de mineração (estudo MSR) de artefatos de configuração de agentes de IA no GitHub:
`CLAUDE.md` / `CLAUDE.local.md`, `AGENTS.md` e `.claude/skills/**`. Ele coleta dados para
responder às questões de pesquisa do estudo:

| RQ | Pergunta | Tabelas gold que a respondem |
|---|---|---|
| RQ1 Conteúdo | Que categorias de instrução aparecem? | `rq1_sections` (seções H2/H3 do HEAD) |
| RQ2 Estrutura | Hierarquia, extensão, legibilidade, frontmatter YAML das Skills | `rq2_structure` |
| RQ3 Evolução | Frequência, o que muda, gatilhos, quem muda | `rq3_changes`, `rq3_author_roles` |
| Correlação | Atributos dos artefatos × métricas do repositório | `repo_features` |

Não há frontend nem API HTTP: tudo é executado pelo CLI `agm`.

## Setup e comandos

```bash
cp .env.sample .env                 # GITHUB_TOKEN (discover/select/add-repo) e DATABASE_URL
poetry env use /usr/bin/python3.11  # Python 3.11 obrigatório
poetry install
docker compose up -d db             # Postgres 15 em :5432
poetry run alembic upgrade head

poetry run agm discover             # 1. Code Search → repositories (DISCOVERED) + discovery_hits
poetry run agm select               # 2. GraphQL → metadados + critérios → SELECTED/REJECTED
poetry run agm add-repo owner/name  #    registra repositórios à mão (marcados como manual)
poetry run agm mine --workers 4     # 3. clone git parcial → commits, versões, mudanças
poetry run agm export               # 4. banco → data/bronze/<timestamp>/*.parquet + manifest.json
poetry run agm analyze              # 5. bronze → data/gold/<timestamp>/ (silver + gold por RQ)
poetry run agm audit                # bronze → data/audit/<export>/: vieses (json, md e LaTeX)
poetry run agm run --workers 4      # 1-3 em paralelo (processos filhos) até esvaziar, depois 4 e 5
poetry run agm help [comando...]    # lista todos os comandos e o que fazem
poetry run agm status [--watch]     # o que está rodando agora (lê o progresso salvo no banco)
poetry run agm runs list | funnel   # execuções registradas / funil de seleção
poetry run agm runs cleanup         # RUNNING sem heartbeat há > --minutes vira INTERRUPTED
poetry run agm rq1 sample           # RQ1: amostra para a criação de labels com LLM
poetry run agm rq1 propose | consolidate   # RQ1: rótulos livres → rascunho do codebook
poetry run agm rq1 classify | saturation   # RQ1: categorias de todas as seções / cobertura
poetry run agm rq1 validate export | llm | score   # RQ1: planilha cega, réplicas, kappa
poetry run agm rq1 compare --model p:m --model p:m # RQ1: dois classificadores comparados
poetry run agm rq2 summary [--compare DIR]  # RQ2: tabelas de estrutura (lado a lado com outro gold)
poetry run agm figures --model p:m --codebook F  # figuras do artigo (TikZ/pgfplots) em figuras.tex

poetry run pytest                   # precisa do Postgres (TEST_DATABASE_URL, criado sozinho)
poetry run ruff check . && poetry run ruff format .
poetry run alembic revision --autogenerate -m "..."   # models.py é a fonte da verdade
```

`agm mine` aceita `--repo NAME` (ignora o status), `--retry-failed`, `--refresh` (minera de novo
se o HEAD mudou), `--force` e `--keep-clones`.

## Arquitetura

```
src/agm_miner/
  cli.py              # comandos Typer; erros esperados viram mensagem de uma linha
  config.py           # Settings (.env: infra/segredos) + StudyConfig (study.yaml: metodologia)
  artifacts.py        # FONTE ÚNICA das regras de artefato (classify_path, GIT_PATHSPECS)
  file_categories.py  # categoria de qualquer arquivo (code, test, docs, ci, agent_artifact, ...)
  runs.py             # track_run(): toda execução vai para pipeline_runs (+ code_version, heartbeat)
  progress.py         # Progress: estado ao vivo de um estágio (fase, espera, contadores, workers)
  progress_view.py    # renderização (rich) do progresso: painel ao vivo e agm status
  orchestrator.py     # agm run: supervisor que lança `agm <estágio>` como processos filhos
  extraction.py       # clone git → registros brutos (sem banco); os 8 passes estão na docstring
  git/                # runner (subprocess -z, streaming, timeout), parsing (puro), repository
  github/client.py    # REST code search + GraphQL, com pacing, quota por bucket e retry limitado
  stages/             # discovery, selection, mining (persistência + concorrência), export
  analysis/           # markdown (AST), metrics, structure_diff, authors, stats, datasets,
                      # rq2_summary (tabelas da RQ2 a partir de rq2_structure; agm rq2 summary),
                      # history_cutoff (corta o histórico na reference_date antes do analyze;
                      # a auditoria mede o que ele remove), sampling_audit (cobertura da
                      # descoberta, efeito de cada critério, falhas e cobertura dos artefatos a
                      # partir do bronze; agm audit), audit_latex (macros, tabelas e rascunho
                      # em LaTeX da auditoria, para o artigo; toda macro usada no rascunho
                      # precisa existir no audit.tex), paper_figures (figuras do artigo em
                      # TikZ/pgfplots, uma macro por figura no figuras.tex; agm figures;
                      # porcentagens arredondadas uma vez, das contagens exatas)
  rq1/                # RQ1 com LLM: units (section_key), sampling, codebook, llm (SDK anthropic,
                      # Batches), store (cache + estado dos lotes), execution (lotes retomáveis),
                      # backends (PromptRequest neutro; Anthropic, Gemini e OpenRouter)
                      # agentreadmes (dataset do Agent READMEs como saída do analyze, para
                      # validação externa contra os rótulos humanos do artigo)
  database/           # models.py (schema), session.py (engine único por URL) e schema.py
                      # (todo comando do CLI recusa um banco fora da revisão head do Alembic)
```

Princípios que precisam ser mantidos:

1. **O minerador grava só fatos brutos**: metadados, commits, arquivos alterados e o conteúdo de
   cada versão. Métricas (palavras, legibilidade, seções, diffs, papéis de autor, bots/agentes) são
   calculadas em `analysis/` a partir do export bronze. **Não adicione colunas derivadas ao banco.**
2. **Decisões metodológicas ficam no `study.yaml`**, não no código (critérios de seleção, limites,
   padrões de bot/agente, janela de burst). Cada execução guarda um snapshot e o hash desse arquivo.
3. **A análise não acessa o banco**: ela lê apenas `data/bronze/<export>/`, que é a unidade de
   replicação. `analysis_manifest.json` registra o export de origem, a config e o `code_version`.
   **Memória do `analyze`:** o `content` das versões passa de 4 GB descomprimido, e estourar a
   RAM do WSL derruba o VS Code junto. O que mantém o pico em poucos GB:
   - `load_bronze` carrega `artifact_versions` sem `content`, que `build_version_tables` lê em
     fatias (`read_version_contents`: `scan_parquet` + `slice`; o `ParquetFile.iter_batches`
     do pyarrow retinha no pool tudo o que já tinha lido). `commit_files` (11 milhões de
     linhas) não é carregado: `co_changed_counts` o agrega de um scan em modo streaming.
   - `clear_syllable_cache` limpa a cada lote o cache do pyphen (usado pelo textstat), que
     guardava cada palavra distinta do corpus sem limite.
   - A RQ3 guarda só um `DocumentOutline` (digests das seções + frontmatter) por blob.
   - Saídas grandes vão para o disco em blocos (`_write_chunks`): `silver_version_sections`
     durante o parse, `rq1_sections` (fatias do arquivo de seções filtradas na consulta e
     ordenadas pelas chaves) e `rq3_changes` (a mensagem do commit entra bloco a bloco).
     As outras são gravadas e liberadas assim que ficam prontas.
   - Strings do polars compartilham buffers: um `filter`/`gather` sobre um frame coletado
     mantém o frame inteiro vivo. Filtre dentro da consulta lazy.

   Não carregue o conteúdo inteiro nem acumule linhas de tabelas grandes em listas Python
   (`_FrameChunks`). Para medir, rode com `systemd-run --user --scope -p MemoryMax=5G`.
4. **Mineração idempotente**: `persist_extraction` apaga e reinsere os dados do repositório numa
   única transação. Cada repositório é protegido por `pg_try_advisory_lock(repo_id)`. Erros viram
   `FAILED` + `last_error` + `attempts`.
5. **O git roda isolado**: `GIT_CONFIG_GLOBAL=/dev/null`, saída `-z`, `LC_ALL=C`. Nunca faça parse
   de saída sem `-z`.
6. **Clone parcial (`--filter=blob:none`)**: o histórico completo (`--raw --no-renames`) não
   precisa de blobs. Os blobs de artefatos são baixados em lote (`prefetch_blobs`); não dispare lazy
   fetch objeto por objeto. Renomeação/cópia (`-M -C`) só roda nos commits que adicionaram artefatos.
7. **Pacing da API do GitHub**: toda chamada passa por `GitHubClient._request` (intervalo mínimo
   de 6,5s na code search, quota lida dos headers, retry com limite para 403 de rate limit, 408,
   429, 5xx e erros de rede). O PyGithub roda com `retry=0` (`default_requester`): a política de
   retry dele faria o urllib3 levantar `RetryError` no primeiro 5xx. Não chame o PyGithub direto.
8. **Todo estágio pode ser interrompido e retomado** (seção "Usando o CLI" do README):
   - o estado fica só no Postgres, e cada unidade de trabalho (página/fatia, lote, repositório) é
     commitada inteira;
   - `track_run` registra `INTERRUPTED`, e o CLI instala os tratadores de SIGINT/SIGTERM
     (`runs.interrupt_signals`) e sai com o código 130;
   - no `mine`, os sinais só escalam `Cancellation`: o 1º para de agendar, o 2º mata os processos
     git (`terminate_active_processes`) sem marcar `FAILED`. O git roda em sessão própria para o
     Ctrl+C do terminal não atingi-lo;
   - `export` e `analyze` escrevem em `.partial` e renomeiam no fim.

   Um código novo que grave estado precisa manter essas propriedades.

   **Progresso:** estágios reportam o que fazem pelo `run.progress` (`progress.Progress`: fase,
   espera, contadores, workers), nunca imprimindo direto. Esperas da API passam por
   `GitHubClient.observe_waits`; funções sem banco recebem callbacks (`extract_repository(...,
   on_phase)`, `prefetch_blobs(..., on_batch)`, `export_table(..., on_rows)`). `track_run`
   publica o progresso no processo (`progress.publishing`, lido pelo painel do CLI) e grava o
   snapshot, as stats e `heartbeat_at` a cada `HEARTBEAT_SECONDS` (lido pelo `agm status`). As
   duas visões usam `progress_view`. Execução `RUNNING` com heartbeat antigo é `stale`.
9. **`agm run` só agenda**: cada estágio roda como processo `agm <estágio>` (sessão própria),
   e o supervisor repassa cada sinal recebido uma única vez (`_Child.deliver_signals`). Não mova
   a lógica de estágio para dentro do orquestrador; garantias novas vão para os comandos. Os
   filhos rodam com `AGM_PROGRESS=off`; o supervisor mostra o progresso deles lendo do banco
   (`child_runs`, casando pelo pid salvo no snapshot), no painel (`PipelinePanel`) ou nas linhas
   de status.
10. **Concorrência**: `discover` e `select` são de instância única (`runs.exclusive_stage`, lock
   consultivo de duas chaves). `mine` aceita vários processos (lock por repositório). `export` lê
   num único snapshot `REPEATABLE READ`.

## Modelo de dados ([database/models.py](src/agm_miner/database/models.py))

- `pipeline_runs`: estágio, status (`RUNNING`/`SUCCEEDED`/`FAILED`/`INTERRUPTED`), params, stats,
  `study_config` (JSONB), `code_version`, `progress` (snapshot JSONB) e `heartbeat_at`, os dois
  atualizados durante a execução.
- `repositories`: id numérico do GitHub + `node_id` (GraphQL), metadados da seleção
  (`metadata_fetched_at`; `last_commit_at` é a data do commit HEAD do branch default, usada no
  critério de atividade), status `DISCOVERED → SELECTED|REJECTED → MINED|FAILED` e
  `rejection_reason` (primeiro critério que falhou). A identidade é o `id`. O `full_name` **não é
  único**, porque o GitHub pode reaproveitar o nome de um repositório renomeado ou apagado.
  `agm select --reevaluate` aplica os critérios atuais a todos os repositórios: `MINED`/`FAILED` que
  continuam qualificados mantêm o status, e os que não se qualificam mais viram `REJECTED` e saem das
  tabelas de análise.
- `discovery_hits`: qual query encontrou qual arquivo (procedência; `manual:add-repo` para adições
  manuais). `discovery_slices`: progresso da bisseção (`SPLIT`/`COLLECTED` por fatia) para retomar.
- `artifacts` (repo, path, tool, kind, scope, skill_name, `exists_at_head`, `head_blob_sha`).
  `skill_name` é só o nome da pasta e se repete entre pacotes de um monorepo: para identificar
  uma skill, use a pasta dela (`analysis.datasets.SKILL_DIR`).
- `artifact_versions` (um blob por artefato; `content`, `is_symlink`, `content_omitted`) e
  `artifact_changes` (PK artifact+sha; A/M/D/T/R/C, `source_path` de renomeação/cópia, numstat,
  blob antes/depois).
- `commits` (só commits não-merge que tocaram artefatos; PK `(repo_id, sha)`),
  `commit_files` (todos os arquivos desses commits, com categoria; sem detecção de renomeação),
  `commit_coauthors` (trailers `Co-authored-by`).
- `identities` (autor por repositório, com `.mailmap` do HEAD), `contributor_activity` (commits e
  arquivos por categoria no histórico inteiro) e `repository_activity` (commits/autores por mês).

Enums são `VARCHAR + CHECK` (`native_enum=False`). O autogenerate **não** detecta valores novos
num enum: escreva a troca da constraint CHECK à mão, como em `0003`. `tests/test_migrations.py`
falha se a migração divergir dos modelos.

## RQ1 com LLM ([src/agm_miner/rq1/](src/agm_miner/rq1/))

- Lê só a saída do `analyze` (`rq1_sections`) e grava em `DATA_DIR/rq1/<gold>/` com manifesto, sem
  banco.
- Etapas montam `backends.PromptRequest` neutros e chamam o `LLMBackend` de
  `make_backend(settings, study.rq1, fase)`, escolhido por `rq1.provider`:
  - `anthropic` (coleta final): o Claude **só** pelo SDK oficial, via `rq1.llm.LLMClient`; trabalho
    em massa pela Batches API (`execution.execute_batched`), que responde do cache, termina lotes
    de uma execução interrompida (`BatchState`) e só envia o que falta;
  - `gemini` (testes com o nível gratuito, configuração atual) e `openrouter` (alternativa): um
    pedido por vez (`execute_one_by_one`), no ritmo de `rq1.*_requests_per_minute`, cada resposta
    no cache assim que chega; o limite diário levanta `DailyLimitError` sem perder nada. O Gemini
    usa o SDK oficial `google-genai` (cota diária: `quotaId` com `PerDay`; por minuto: espera o
    `retryDelay`), o OpenRouter usa HTTP com `requests`.
  - Os dois expõem as esperas por `observe_waits`. Testes usam `FakeSession`/SDK falso.
- Todo comando que chama o LLM ou grava no diretório da RQ1 roda dentro de `store.run_lock(dir)`:
  um segundo comando no mesmo diretório falha com `Rq1BusyError` em vez de enviar os mesmos
  pedidos de novo. A amostra depende só do conteúdo, da semente e da configuração, nunca de ids do
  banco (`sampling` ordena por `section_key` antes de embaralhar).
- Prompts e validação das respostas ficam em `rq1/prompts.py` (puro). Etapas passam por
  `stages._run_requests`/`_settle`: resposta fora do formato (`InvalidAnswer`) sai do cache e conta
  no `FailureLog`; com `rq1.max_attempts` falhas o pedido é abandonado (`given_up`).
- A chave do cache (`store.request_key`) precisa cobrir tudo o que determina a resposta (fase,
  provedor, modelo, versão do prompt, hash do codebook, `section_key`). Ao mudar um prompt, mude a
  versão.
- Dois classificadores: `--model provedor:modelo` (`Rq1Config.with_model`) troca provedor e
  `classify_model` numa execução de `classify`, `saturation`, `validate llm/score` e
  `agentreadmes score` (no `smoke`, o modelo da fase de `--phase`). Tudo o que um
  modelo gera fica em pastas por provedor/modelo (o score de validação inclusive), e
  `stages.compare_models` compara dois modelos na amostra de validação e em todas as seções.
- Validação externa (`rq1/agentreadmes.py`): `agm rq1 agentreadmes import` grava o dataset do
  *Agent READMEs* em `DATA_DIR/external/agentreadmes/` no formato da saída do `analyze`
  (`rq1_sections` + `analysis_manifest.json`), então todos os comandos da RQ1 rodam nele com
  `--gold`. Também grava `rq2_structure`, montado pelo próprio `build_rq2_structure` (mesmas
  colunas e tipos da tabela gold; `version_id` = `artifact_id`), para comparar a RQ2 com os
  arquivos do artigo. Os rótulos do artigo são mapeados por tabela explícita para o
  `seed_agent_readmes.yaml`; um rótulo desconhecido faz a importação falhar. O `score` agrega as
  categorias das seções por arquivo e compara com os rótulos humanos, com o GPT-5 como linha de
  base. Nada é filtrado na importação (o `COPILOT` fica); `--tool` filtra no score e
  `classify --only-labelled` classifica só os arquivos com rótulos de referência.
- `classify --unit file` (`stages.classify_files`, prompt `classify-file-v1`) classifica cada
  arquivo inteiro numa requisição (estratégia do artigo), lendo o `file_contents.parquet` que só
  a importação grava. A chave do cache usa o hash do conteúdo, e a saída fica em
  `classify-file/`. `agentreadmes score --unit file` pontua essa saída e a compara com a
  classificação por seção do mesmo modelo (`section_vs_file`).
- Modelos, effort e tamanhos de amostra ficam em `rq1:` no `study.yaml`; o codebook curado fica em
  `codebooks/` (versionado). Testes usam SDKs/clientes falsos, nunca a API real.

## Estilo de código

- Python 3.11, `from __future__ import annotations`, type hints completos, `StrEnum`,
  `@dataclass(frozen=True, slots=True)` para registros.
- Funções puras separadas de I/O (`git/parsing.py`, `analysis/*`, `selection.evaluate`), para
  poderem ser testadas sem rede nem banco.
- Dependências injetáveis: `session_factory`, `client`, `clone_url`, e `sleep`/`clock` no cliente
  GitHub.
- Logging com `logger = logging.getLogger(__name__)` e argumentos `%s`. Sem `print` fora do CLI.
- Helpers privados com `_`; constantes em `UPPER_SNAKE`; linhas de até 100 colunas; Ruff com
  `E,F,I,B,UP,SIM`.
- Código, docstrings e logs em inglês; documentação do projeto em português.
- Commits no padrão Conventional Commits, em inglês.

## Testes

Toda mudança vem com teste. Marcadores: `db` (Postgres, pulado se não houver servidor) e `git`.

- `tests/gitrepo.py::RepoBuilder` cria repositórios git determinísticos (autor, data, merge,
  symlink, renomeação). O minerador os clona via `file://` com filtro de blobs, o mesmo caminho da
  produção.
- `tests/db_fixtures.py` recria o schema com `alembic upgrade head` a cada sessão e trunca as
  tabelas entre testes.
- Clientes GitHub falsos (`FakeRequester`, `FakeSearch`, `FakeGraphQL`) simulam paginação, o limite
  de 1000 resultados, 403/408/429/5xx, erros de rede e `NOT_FOUND`; `github_stub` sobe um servidor
  HTTP local para testar o `default_requester` real.
- `tests/test_end_to_end.py` roda minerar → exportar → analisar e verifica as tabelas gold.

## Limitações conhecidas (ameaças à validade)

- A Code Search indexa só o branch default e arquivos menores que 384 KB, e não é determinística.
  Há mais de 1000 `AGENTS.md` numa faixa de 20 bytes, então até fatias de 1 byte podem estourar o
  limite. `unreachable_matches` nas estatísticas da execução mede quantos resultados ficaram
  inacessíveis.
- A REST devolve `node_id` legado (`MDEw...`) para repositórios antigos, e o GraphQL responde com o
  id novo. `fetch_metadata` casa as respostas por posição e grava o id canônico.
  `stars:` não funciona nela, por isso o filtro de estrelas fica no estágio `select`.
- As heurísticas de "tutorial/lista" (`exclude_keywords`), de bots, de agentes e dos papéis de
  autor precisam de validação manual numa amostra antes da coleta final.
- `hours_since_previous_change` usa a data de autoria (rebases podem distorcer).
- Flesch só é calculado para textos detectados como inglês, e sobre `readability_text`
  (`analysis/markdown.py`): parágrafos e itens de lista, sem títulos, tabelas e código. Cada
  bloco é tratado como uma frase e termina em `.!?` (o textstat só separa frases por esses três
  sinais; `:;,` no fim viram ponto). Blocos de 1 ou 2 palavras (rótulos, links soltos) ficam de
  fora, porque o textstat não os conta como frases mas contaria as palavras. Sem isso, um arquivo
  só de bullets virava uma frase única (grau acima de 20). Ainda distorcem o índice os blocos
  formados por caminhos ou URLs sem espaços (muitas sílabas por "palavra").
- Itens de lista tight não contam como parágrafos, URLs soltas contam como links e o BOM UTF-8 é
  ignorado antes do frontmatter.
- A mineração leva semanas; com `analysis.truncate_at_reference_date` o `analyze` corta o
  histórico no fim da `reference_date` (`analysis/history_cutoff.py`, aplicado às tabelas do
  bronze logo depois de carregá-las). Não dá para cortar `identities.commits` nem
  `contributor_activity` (agregados do clone inteiro), nem os metadados da seleção.
- `reference_date` no `study.yaml` fica fixa durante a coleta. O corte `created_before` do estudo
  (2026-08-01) é mais tardio que o dos artigos relacionados (2024-01-01, o padrão de `config.py`),
  então a amostra inclui projetos criados já com agentes.

## Como estender

- **Nova ferramenta (ex.: `GEMINI.md`)**: adicione o valor em `ArtifactTool`, trate em
  `classify_path`, inclua o pathspec em `GIT_PATHSPECS` e a query em `study.yaml`, e gere uma
  migração (a constraint CHECK muda). Cubra com casos em `tests/test_artifacts.py`.
- **Nova métrica**: implemente em `analysis/` e acrescente ao schema da tabela silver/gold
  correspondente. Não mexa no minerador.
- **Novo critério de amostragem**: se depende de metadados do GitHub, entra em
  `selection.evaluate` (motivo gravado no banco, funil via `agm runs funnel`); se depende do
  histórico minerado, entra em `build_repository_sample`/`build_artifact_sample` de
  `analysis/datasets.py` (motivo em `repo_features.exclusion_reason`). O limite vai para o
  `study.yaml`. As tabelas silver nunca são filtradas; as gold contêm só a amostra.
