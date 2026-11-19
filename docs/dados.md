# Dicionário de dados

<!-- Gerado de src/agm_miner/replication/data_dictionary.yaml: não edite à mão. Para
     atualizar: python -m agm_miner.replication.data_dictionary > docs/dados.md -->

Todas as tabelas são arquivos Parquet. As camadas seguem o fluxo do pipeline:

- **bronze**: Fatos brutos exportados do banco por `agm export` (`data/bronze/<export>/`), sem nenhuma métrica derivada. É a unidade de replicação: o `analyze` lê só esta pasta.
- **silver**: Tabelas intermediárias do `agm analyze` (`data/gold/<export>/silver_*`), com todos os artefatos e versões, inclusive os que ficam fora da amostra.
- **gold**: Tabelas do `agm analyze` (`data/gold/<export>/`) que respondem às questões de pesquisa. Só `repo_features` traz todos os repositórios minerados (com `in_sample`); as demais contêm só a amostra do estudo.

Colunas marcadas como *dado pessoal* são pseudonimizadas no pacote de replicação.

## Bronze

- [`pipeline_runs`](#pipeline_runs)
- [`discovery_slices`](#discovery_slices)
- [`discovery_hits`](#discovery_hits)
- [`repositories`](#repositories)
- [`repository_activity`](#repository_activity)
- [`identities`](#identities)
- [`contributor_activity`](#contributor_activity)
- [`artifacts`](#artifacts)
- [`artifact_versions`](#artifact_versions)
- [`commits`](#commits)
- [`commit_coauthors`](#commit_coauthors)
- [`commit_files`](#commit_files)
- [`artifact_changes`](#artifact_changes)

<a id="pipeline_runs"></a>

### `pipeline_runs`

Uma linha por execução de um estágio do pipeline, com parâmetros, estatísticas e a versão do código e da configuração.

| Coluna | Descrição |
|---|---|
| `id` | Id da execução. |
| `stage` | Estágio: `DISCOVER`, `SELECT`, `MINE` ou `EXPORT`. |
| `status` | `RUNNING`, `SUCCEEDED`, `FAILED` ou `INTERRUPTED`. |
| `started_at` | Início (UTC). |
| `finished_at` | Fim (UTC). |
| `code_version` | Commit do código (`-dirty` quando havia mudanças não commitadas). |
| `study_config_hash` | SHA-256 do `study.yaml` usado. |
| `study_config` | O `study.yaml` usado, em JSON. |
| `params` | Parâmetros da execução, em JSON. |
| `stats` | Estatísticas da execução, em JSON. |
| `error` | Mensagem de erro (execuções `FAILED`). |
| `progress` | Último retrato do progresso, em JSON. |
| `heartbeat_at` | Último sinal de vida do processo (UTC). |

<a id="discovery_slices"></a>

### `discovery_slices`

Progresso da bisseção por tamanho de arquivo na busca de código do GitHub (uma linha por fatia).

| Coluna | Descrição |
|---|---|
| `slice_query` | Query enviada à API (query base + faixa `size:`). |
| `base_query` | Query do `study.yaml`. |
| `outcome` | `SPLIT` (mais de 1.000 resultados; as duas metades foram buscadas) ou `COLLECTED` (todas as páginas acessíveis foram gravadas). |
| `total_count` | Total de resultados estimado pela API. |
| `run_id` | Execução (`pipeline_runs.id`) que gravou a linha. |
| `completed_at` | Quando a fatia terminou (UTC). |

<a id="discovery_hits"></a>

### `discovery_hits`

Qual query encontrou qual arquivo de qual repositório (procedência da amostra).

| Coluna | Descrição |
|---|---|
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `query` | Query que encontrou o arquivo (`manual:add-repo` para repositórios adicionados à mão). |
| `path` | Caminho do arquivo encontrado. |
| `run_id` | Execução (`pipeline_runs.id`) que gravou a linha. |
| `found_at` | Quando foi encontrado (UTC). |

<a id="repositories"></a>

### `repositories`

Todos os repositórios descobertos, com os metadados do GitHub da seleção e o status no pipeline.

| Coluna | Descrição |
|---|---|
| `id` | Id numérico do repositório no GitHub (estável entre renomeações). |
| `full_name` | Nome `dono/repositório` no momento da coleta (não é único ao longo do tempo). |
| `node_id` | Id global do GraphQL. |
| `status` | `DISCOVERED`, `SELECTED`, `REJECTED`, `MINED` ou `FAILED`. |
| `rejection_reason` | Primeiro critério de seleção que falhou (repositórios `REJECTED`). |
| `last_error` | Último erro da mineração (repositórios `FAILED`). |
| `attempts` | Tentativas de mineração. |
| `discovered_at` | Quando foi descoberto (UTC). |
| `metadata_fetched_at` | Quando os metadados foram lidos (UTC). |
| `default_branch` | Branch default. |
| `gh_created_at` | Criação do repositório no GitHub (UTC). |
| `pushed_at` | Último push (UTC). |
| `stars` | Estrelas na seleção. |
| `forks` | Forks na seleção. |
| `watchers` | Watchers na seleção. |
| `disk_usage_kb` | Tamanho informado pelo GitHub (KB). |
| `primary_language` | Linguagem principal (GitHub Linguist). |
| `topics` | Tópicos do repositório. |
| `license_spdx` | Licença (identificador SPDX informado pelo GitHub). |
| `description` | Descrição do repositório. |
| `is_fork` | É fork. |
| `is_archived` | Está arquivado. |
| `is_template` | É template. |
| `is_mirror` | É espelho. |
| `commits_count` | Commits do branch default (GitHub). |
| `last_commit_at` | Data do commit HEAD do branch default (critério de atividade). |
| `mined_head_sha` | Commit HEAD minerado. |
| `mined_at` | Quando foi minerado (UTC). |
| `mined_run_id` | Execução que minerou o repositório. |

<a id="repository_activity"></a>

### `repository_activity`

Atividade mensal do branch default inteiro (normaliza as taxas de mudança dos artefatos).

| Coluna | Descrição |
|---|---|
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `month` | Mês (primeiro dia, UTC). |
| `commits` | Commits no mês. |
| `authors` | Autores distintos no mês. |

<a id="identities"></a>

### `identities`

Autores por repositório, agrupados pelo e-mail normalizado (depois do `.mailmap` do HEAD).

| Coluna | Descrição |
|---|---|
| `id` | Id da identidade. |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `identity_key` | E-mail normalizado (ou `name:<nome>` sem e-mail) que agrupa a identidade. Dado pessoal. |
| `name` | Nome do autor como aparece no git (depois do `.mailmap` do HEAD). Dado pessoal. |
| `email` | E-mail do autor como aparece no git (depois do `.mailmap` do HEAD). Dado pessoal. |
| `github_login` | Login do GitHub associado ao autor, quando o e-mail é do tipo `noreply` do GitHub. Dado pessoal. |
| `commits` | Commits da identidade no histórico inteiro do clone. |
| `first_commit_at` | Primeiro commit (UTC). |
| `last_commit_at` | Último commit (UTC). |

<a id="contributor_activity"></a>

### `contributor_activity`

Commits e arquivos alterados por categoria de arquivo, para cada identidade, no histórico inteiro.

| Coluna | Descrição |
|---|---|
| `identity_id` | Id da identidade (autor por repositório); chave de junção com `identities.id`. |
| `file_category` | Categoria do arquivo: `agent_artifact`, `agent_config`, `test`, `ci`, `docs`, `build_config`, `code`, `asset` ou `other` (`file_categories.py`). |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `commits` | Commits que alteraram arquivos dessa categoria. |
| `file_changes` | Arquivos dessa categoria alterados. |

<a id="artifacts"></a>

### `artifacts`

Um artefato por caminho e repositório (renomeações e cópias ligam caminhos por `artifact_changes`).

| Coluna | Descrição |
|---|---|
| `id` | Id do artefato. |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `path` | Caminho do arquivo no repositório. |
| `tool` | Ferramenta do artefato: `CLAUDE` (`CLAUDE.md`, `CLAUDE.local.md`, Skills em `.claude/skills/`) ou `AGENTS` (`AGENTS.md`). |
| `kind` | Tipo do artefato: `INSTRUCTION` (arquivo de instruções), `SKILL_MANIFEST` (`SKILL.md`) ou `SKILL_RESOURCE` (outro arquivo da pasta de uma Skill). |
| `scope` | `ROOT` (na raiz do repositório) ou `NESTED` (em uma subpasta). |
| `skill_name` | Nome da pasta da Skill (só para Skills). Repete-se entre pacotes de um monorepo; para identificar uma Skill, use a pasta. |
| `exists_at_head` | O artefato existe no HEAD minerado do branch default. |
| `head_blob_sha` | Blob do artefato no HEAD minerado (vazio se foi removido). |

<a id="artifact_versions"></a>

### `artifact_versions`

Cada conteúdo distinto (blob do git) que um artefato teve.

| Coluna | Descrição |
|---|---|
| `id` | Id da versão. |
| `artifact_id` | Id do artefato (um caminho num repositório); chave de junção com `artifacts.id`. |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `blob_sha` | Blob do git. |
| `file_mode` | Modo do arquivo no git (por exemplo `100644`, `120000` para symlink). |
| `is_symlink` | O arquivo é um link simbólico (o conteúdo é o destino do link). |
| `size_bytes` | Tamanho do blob em bytes. |
| `content` | Conteúdo do arquivo (texto UTF-8). O conteúdo segue a licença do repositório de origem. |
| `content_omitted` | Por que o conteúdo não foi guardado: `TOO_LARGE`, `BINARY` ou `NOT_UTF8` (vazio quando foi guardado). |

<a id="commits"></a>

### `commits`

Commits do branch default, exceto merges, que alteraram ao menos um artefato.

| Coluna | Descrição |
|---|---|
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `sha` | SHA do commit. |
| `author_identity_id` | Identidade do autor (`identities.id`). |
| `author_name` | Nome do autor no commit. Dado pessoal. |
| `author_email` | E-mail do autor no commit. Dado pessoal. |
| `committer_name` | Nome de quem fez o commit. Dado pessoal. |
| `committer_email` | E-mail de quem fez o commit. Dado pessoal. |
| `authored_at` | Data de autoria do commit (UTC). |
| `committed_at` | Data do commit (UTC). |
| `message` | Mensagem do commit. |
| `parents` | SHAs dos commits pais. |

<a id="commit_coauthors"></a>

### `commit_coauthors`

Trailers `Co-authored-by` dos commits (por exemplo, commits coescritos por agentes de código).

| Coluna | Descrição |
|---|---|
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `sha` | SHA do commit. |
| `position` | Ordem do trailer na mensagem. |
| `name` | Nome do coautor. Dado pessoal. |
| `email` | E-mail do coautor. Dado pessoal. |

<a id="commit_files"></a>

### `commit_files`

Todos os arquivos alterados pelos commits de `commits` (análise de coalteração e gatilhos), sem detecção de renomeação.

| Coluna | Descrição |
|---|---|
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `sha` | SHA do commit. |
| `path` | Caminho do arquivo no repositório. |
| `change_type` | Tipo da mudança do git: `A` (adição), `M` (modificação), `D` (remoção), `R` (renomeação), `C` (cópia) ou `T` (troca de tipo). |
| `file_category` | Categoria do arquivo: `agent_artifact`, `agent_config`, `test`, `ci`, `docs`, `build_config`, `code`, `asset` ou `other` (`file_categories.py`). |

<a id="artifact_changes"></a>

### `artifact_changes`

A mudança de cada commit em cada artefato.

| Coluna | Descrição |
|---|---|
| `artifact_id` | Id do artefato (um caminho num repositório); chave de junção com `artifacts.id`. |
| `sha` | SHA do commit. |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `change_type` | Tipo da mudança do git: `A` (adição), `M` (modificação), `D` (remoção), `R` (renomeação), `C` (cópia) ou `T` (troca de tipo). |
| `source_path` | Caminho de origem de uma renomeação ou cópia. |
| `similarity` | Similaridade (%) informada pelo git numa renomeação ou cópia. |
| `additions` | Linhas adicionadas (numstat do git; vazio para arquivos binários). |
| `deletions` | Linhas removidas (numstat do git; vazio para arquivos binários). |
| `blob_sha` | Blob do arquivo depois da mudança (vazio numa remoção). |
| `previous_blob_sha` | Blob antes da mudança (vazio numa adição; numa renomeação ou cópia, é o blob de `source_path`). |
| `version_id` | Versão do artefato depois da mudança (vazio numa remoção). |

## Silver

- [`silver_artifact_sample`](#silver_artifact_sample)
- [`silver_version_metrics`](#silver_version_metrics)
- [`silver_version_sections`](#silver_version_sections)

<a id="silver_artifact_sample"></a>

### `silver_artifact_sample`

Todos os artefatos dos repositórios minerados, com o critério de tamanho da amostra.

| Coluna | Descrição |
|---|---|
| `artifact_id` | Id do artefato (um caminho num repositório); chave de junção com `artifacts.id`. |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `path` | Caminho do arquivo no repositório. |
| `kind` | Tipo do artefato: `INSTRUCTION` (arquivo de instruções), `SKILL_MANIFEST` (`SKILL.md`) ou `SKILL_RESOURCE` (outro arquivo da pasta de uma Skill). |
| `skill_name` | Nome da pasta da Skill (só para Skills). Repete-se entre pacotes de um monorepo; para identificar uma Skill, use a pasta. |
| `exists_at_head` | O artefato existe no HEAD minerado do branch default. |
| `is_symlink` | O arquivo é um link simbólico (o conteúdo é o destino do link). |
| `size_bytes` | Tamanho da última versão (bytes). |
| `skill_dir` | Pasta da Skill (identifica a Skill dentro do repositório). |
| `meets_min_size` | A última versão tem ao menos `analysis.min_artifact_bytes` e não é symlink. |
| `in_sample` | O artefato entra na amostra (para recursos de Skill, quando o `SKILL.md` da mesma pasta entra). |

<a id="silver_version_metrics"></a>

### `silver_version_metrics`

Métricas de estrutura (RQ2) de cada versão de cada artefato.

| Coluna | Descrição |
|---|---|
| `version_id` | Id da versão (um blob distinto) do artefato; chave de junção com `artifact_versions.id`. |
| `artifact_id` | Id do artefato (um caminho num repositório); chave de junção com `artifacts.id`. |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `lines` | Linhas do arquivo. |
| `chars` | Caracteres do arquivo. |
| `words` | Palavras do arquivo. |
| `headings` | Títulos de qualquer nível. |
| `h1` | Títulos de nível 1. |
| `h2` | Títulos de nível 2. |
| `h3` | Títulos de nível 3. |
| `h4_plus` | Títulos de nível 4 ou mais. |
| `max_heading_depth` | Maior nível de título usado. |
| `sections` | Seções (blocos que começam num título, mais o texto antes do primeiro título). |
| `paragraphs` | Parágrafos (itens de lista compactos não contam). |
| `code_blocks` | Blocos de código, cercados (```) e indentados. |
| `code_languages` | Linguagens declaradas nos blocos de código cercados. |
| `inline_code` | Trechos de código em linha (`assim`). |
| `bullet_lists` | Listas com marcadores. |
| `ordered_lists` | Listas numeradas. |
| `list_items` | Itens de lista. |
| `task_items` | Itens de lista de tarefas (`- [ ]`). |
| `tables` | Tabelas. |
| `links` | Links, inclusive URLs soltas. |
| `images` | Imagens. |
| `blockquotes` | Citações (`>`). |
| `html_blocks` | Blocos de HTML. |
| `emphasis` | Trechos em itálico ou negrito. |
| `has_frontmatter` | O arquivo começa com um frontmatter YAML (`---`). |
| `frontmatter_valid` | O frontmatter é YAML válido e é um mapeamento. |
| `frontmatter_keys` | Chaves de primeiro nível do frontmatter. |
| `frontmatter_json` | O frontmatter em JSON. |
| `heading_tree_json` | Árvore de títulos em JSON: `[{"level", "title", "children"}]`. |
| `language` | Idioma detectado (`langdetect`), por exemplo `en`. |
| `flesch_reading_ease` | Índice de facilidade de leitura de Flesch, só para textos em inglês, calculado sobre parágrafos e itens de lista. |
| `flesch_kincaid_grade` | Grau de Flesch-Kincaid, nas mesmas condições do Flesch. |

<a id="silver_version_sections"></a>

### `silver_version_sections`

Seções (título e texto) de cada versão de cada artefato.

| Coluna | Descrição |
|---|---|
| `version_id` | Id da versão (um blob distinto) do artefato; chave de junção com `artifact_versions.id`. |
| `artifact_id` | Id do artefato (um caminho num repositório); chave de junção com `artifacts.id`. |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `position` | Ordem da seção no arquivo (0 é a primeira). |
| `level` | Nível do título da seção (1 = `#`, 2 = `##`...); 0 é o texto antes do primeiro título. |
| `heading` | Texto do título da seção (vazio no texto antes do primeiro título). |
| `heading_path` | Títulos da seção e dos seus ancestrais, do mais externo ao mais interno. |
| `h1` | Título de nível 1 ao qual a seção pertence. |
| `h2` | Título de nível 2 ao qual a seção pertence. |
| `h3` | Título de nível 3 ao qual a seção pertence. |
| `text` | Texto da seção, sem o título. |
| `words` | Palavras do texto da seção. |

## Gold

- [`rq1_sections`](#rq1_sections)
- [`rq2_structure`](#rq2_structure)
- [`rq3_changes`](#rq3_changes)
- [`rq3_author_roles`](#rq3_author_roles)
- [`repo_features`](#repo_features)

<a id="rq1_sections"></a>

### `rq1_sections`

RQ1: seções da versão no HEAD (ou na data de corte) de cada artefato da amostra, prontas para a classificação de conteúdo.

| Coluna | Descrição |
|---|---|
| `version_id` | Id da versão (um blob distinto) do artefato; chave de junção com `artifact_versions.id`. |
| `artifact_id` | Id do artefato (um caminho num repositório); chave de junção com `artifacts.id`. |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `position` | Ordem da seção no arquivo (0 é a primeira). |
| `level` | Nível do título da seção (1 = `#`, 2 = `##`...); 0 é o texto antes do primeiro título. |
| `heading` | Texto do título da seção (vazio no texto antes do primeiro título). |
| `heading_path` | Títulos da seção e dos seus ancestrais, do mais externo ao mais interno. |
| `h1` | Título de nível 1 ao qual a seção pertence. |
| `h2` | Título de nível 2 ao qual a seção pertence. |
| `h3` | Título de nível 3 ao qual a seção pertence. |
| `text` | Texto da seção, sem o título. |
| `words` | Palavras do texto da seção. |
| `full_name` | Nome `dono/repositório` no momento da coleta (não é único ao longo do tempo). |
| `path` | Caminho do arquivo no repositório. |
| `tool` | Ferramenta do artefato: `CLAUDE` (`CLAUDE.md`, `CLAUDE.local.md`, Skills em `.claude/skills/`) ou `AGENTS` (`AGENTS.md`). |
| `kind` | Tipo do artefato: `INSTRUCTION` (arquivo de instruções), `SKILL_MANIFEST` (`SKILL.md`) ou `SKILL_RESOURCE` (outro arquivo da pasta de uma Skill). |
| `scope` | `ROOT` (na raiz do repositório) ou `NESTED` (em uma subpasta). |
| `skill_name` | Nome da pasta da Skill (só para Skills). Repete-se entre pacotes de um monorepo; para identificar uma Skill, use a pasta. |

<a id="rq2_structure"></a>

### `rq2_structure`

RQ2: estrutura da versão no HEAD (ou na data de corte) de cada artefato da amostra.

| Coluna | Descrição |
|---|---|
| `artifact_id` | Id do artefato (um caminho num repositório); chave de junção com `artifacts.id`. |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `path` | Caminho do arquivo no repositório. |
| `tool` | Ferramenta do artefato: `CLAUDE` (`CLAUDE.md`, `CLAUDE.local.md`, Skills em `.claude/skills/`) ou `AGENTS` (`AGENTS.md`). |
| `kind` | Tipo do artefato: `INSTRUCTION` (arquivo de instruções), `SKILL_MANIFEST` (`SKILL.md`) ou `SKILL_RESOURCE` (outro arquivo da pasta de uma Skill). |
| `scope` | `ROOT` (na raiz do repositório) ou `NESTED` (em uma subpasta). |
| `skill_name` | Nome da pasta da Skill (só para Skills). Repete-se entre pacotes de um monorepo; para identificar uma Skill, use a pasta. |
| `exists_at_head` | O artefato existe no HEAD minerado do branch default. |
| `head_blob_sha` | Blob do artefato no HEAD minerado (vazio se foi removido). |
| `version_id` | Id da versão (um blob distinto) do artefato; chave de junção com `artifact_versions.id`. |
| `is_symlink` | O arquivo é um link simbólico (o conteúdo é o destino do link). |
| `size_bytes` | Tamanho do blob em bytes. |
| `content_omitted` | Por que o conteúdo não foi guardado: `TOO_LARGE`, `BINARY` ou `NOT_UTF8` (vazio quando foi guardado). |
| `full_name` | Nome `dono/repositório` no momento da coleta (não é único ao longo do tempo). |
| `lines` | Linhas do arquivo. |
| `chars` | Caracteres do arquivo. |
| `words` | Palavras do arquivo. |
| `headings` | Títulos de qualquer nível. |
| `h1` | Títulos de nível 1. |
| `h2` | Títulos de nível 2. |
| `h3` | Títulos de nível 3. |
| `h4_plus` | Títulos de nível 4 ou mais. |
| `max_heading_depth` | Maior nível de título usado. |
| `sections` | Seções (blocos que começam num título, mais o texto antes do primeiro título). |
| `paragraphs` | Parágrafos (itens de lista compactos não contam). |
| `code_blocks` | Blocos de código, cercados (```) e indentados. |
| `code_languages` | Linguagens declaradas nos blocos de código cercados. |
| `inline_code` | Trechos de código em linha (`assim`). |
| `bullet_lists` | Listas com marcadores. |
| `ordered_lists` | Listas numeradas. |
| `list_items` | Itens de lista. |
| `task_items` | Itens de lista de tarefas (`- [ ]`). |
| `tables` | Tabelas. |
| `links` | Links, inclusive URLs soltas. |
| `images` | Imagens. |
| `blockquotes` | Citações (`>`). |
| `html_blocks` | Blocos de HTML. |
| `emphasis` | Trechos em itálico ou negrito. |
| `has_frontmatter` | O arquivo começa com um frontmatter YAML (`---`). |
| `frontmatter_valid` | O frontmatter é YAML válido e é um mapeamento. |
| `frontmatter_keys` | Chaves de primeiro nível do frontmatter. |
| `frontmatter_json` | O frontmatter em JSON. |
| `heading_tree_json` | Árvore de títulos em JSON: `[{"level", "title", "children"}]`. |
| `language` | Idioma detectado (`langdetect`), por exemplo `en`. |
| `flesch_reading_ease` | Índice de facilidade de leitura de Flesch, só para textos em inglês, calculado sobre parágrafos e itens de lista. |
| `flesch_kincaid_grade` | Grau de Flesch-Kincaid, nas mesmas condições do Flesch. |

<a id="rq3_changes"></a>

### `rq3_changes`

RQ3: cada mudança de cada artefato da amostra, com intervalo, picos, diff estrutural, coalterações e autoria.

| Coluna | Descrição |
|---|---|
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `artifact_id` | Id do artefato (um caminho num repositório); chave de junção com `artifacts.id`. |
| `path` | Caminho do arquivo no repositório. |
| `tool` | Ferramenta do artefato: `CLAUDE` (`CLAUDE.md`, `CLAUDE.local.md`, Skills em `.claude/skills/`) ou `AGENTS` (`AGENTS.md`). |
| `kind` | Tipo do artefato: `INSTRUCTION` (arquivo de instruções), `SKILL_MANIFEST` (`SKILL.md`) ou `SKILL_RESOURCE` (outro arquivo da pasta de uma Skill). |
| `scope` | `ROOT` (na raiz do repositório) ou `NESTED` (em uma subpasta). |
| `skill_name` | Nome da pasta da Skill (só para Skills). Repete-se entre pacotes de um monorepo; para identificar uma Skill, use a pasta. |
| `sha` | SHA do commit. |
| `change_type` | Tipo da mudança do git: `A` (adição), `M` (modificação), `D` (remoção), `R` (renomeação), `C` (cópia) ou `T` (troca de tipo). |
| `source_path` | Caminho de origem de uma renomeação ou cópia. |
| `similarity` | Similaridade (%) informada pelo git numa renomeação ou cópia. |
| `additions` | Linhas adicionadas (numstat do git; vazio para arquivos binários). |
| `deletions` | Linhas removidas (numstat do git; vazio para arquivos binários). |
| `authored_at` | Data de autoria do commit (UTC). |
| `committed_at` | Data do commit (UTC). |
| `change_index` | Posição da mudança no histórico do artefato (1 é a primeira). |
| `hours_since_previous_change` | Horas desde a mudança anterior do mesmo artefato (pela data de autoria; vazio na primeira). |
| `in_burst` | A mudança veio até `analysis.burst_window_hours` horas depois da anterior (pico). |
| `diff_available` | Os dois lados da mudança tinham conteúdo para comparar. |
| `sections_added` | Seções (caminhos de títulos) que apareceram. |
| `sections_removed` | Seções que desapareceram. |
| `sections_modified` | Seções cujo texto mudou. |
| `frontmatter_added` | Chaves do frontmatter que apareceram. |
| `frontmatter_removed` | Chaves do frontmatter que desapareceram. |
| `frontmatter_modified` | Chaves do frontmatter cujo valor mudou. |
| `structural_change` | Seções ou chaves do frontmatter apareceram ou desapareceram. |
| `co_changed_files` | Outros arquivos alterados no mesmo commit. |
| `co_changed_agent_artifact` | Arquivos coalterados da categoria `agent_artifact`. |
| `co_changed_agent_config` | Arquivos coalterados da categoria `agent_config`. |
| `co_changed_test` | Arquivos coalterados da categoria `test`. |
| `co_changed_ci` | Arquivos coalterados da categoria `ci`. |
| `co_changed_docs` | Arquivos coalterados da categoria `docs`. |
| `co_changed_build_config` | Arquivos coalterados da categoria `build_config`. |
| `co_changed_code` | Arquivos coalterados da categoria `code`. |
| `co_changed_asset` | Arquivos coalterados da categoria `asset`. |
| `co_changed_other` | Arquivos coalterados da categoria `other`. |
| `author_identity_id` | Identidade do autor (`identities.id`). |
| `author_role` | Papel do autor (ver `rq3_author_roles.role`). |
| `author_is_bot` | O autor é um bot. |
| `agents_involved` | Agentes de código detectados no autor, no committer ou nos coautores (`analysis.agent_patterns`). |
| `agent_involved` | Algum agente de código participou do commit. |
| `message_subject` | Primeira linha da mensagem do commit. |
| `message` | Mensagem do commit. |

<a id="rq3_author_roles"></a>

### `rq3_author_roles`

RQ3: papel de cada autor dos repositórios da amostra, pelo perfil de arquivos alterados no histórico inteiro.

| Coluna | Descrição |
|---|---|
| `identity_id` | Id da identidade (autor por repositório); chave de junção com `identities.id`. |
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `name` | Nome do autor como aparece no git (depois do `.mailmap` do HEAD). Dado pessoal. |
| `github_login` | Login do GitHub associado ao autor, quando o e-mail é do tipo `noreply` do GitHub. Dado pessoal. |
| `commits` | Commits da identidade no histórico inteiro. |
| `artifact_commits` | Commits da identidade que alteraram artefatos. |
| `is_bot` | O nome ou e-mail bate com `analysis.bot_patterns`. |
| `agent` | Agente de código detectado (`analysis.agent_patterns`), por exemplo `claude`. |
| `role` | `agent`, `bot`, `docs_focused`, `code_focused` ou `mixed` (só os três últimos são humanos; limites em `analysis`). |
| `file_changes` | Arquivos alterados no histórico inteiro. |
| `share_agent_artifact` | Fração dos arquivos alterados da categoria `agent_artifact`. |
| `share_agent_config` | Fração dos arquivos alterados da categoria `agent_config`. |
| `share_test` | Fração dos arquivos alterados da categoria `test`. |
| `share_ci` | Fração dos arquivos alterados da categoria `ci`. |
| `share_docs` | Fração dos arquivos alterados da categoria `docs`. |
| `share_build_config` | Fração dos arquivos alterados da categoria `build_config`. |
| `share_code` | Fração dos arquivos alterados da categoria `code`. |
| `share_asset` | Fração dos arquivos alterados da categoria `asset`. |
| `share_other` | Fração dos arquivos alterados da categoria `other`. |

<a id="repo_features"></a>

### `repo_features`

Correlação: todos os repositórios minerados, com metadados, atributos agregados dos artefatos da amostra e o critério de amostra.

| Coluna | Descrição |
|---|---|
| `repo_id` | Id numérico do repositório no GitHub (estável entre renomeações); chave de junção com `repositories.id`. |
| `full_name` | Nome `dono/repositório` no momento da coleta (não é único ao longo do tempo). |
| `node_id` | Id global do GraphQL. |
| `status` | Status no pipeline (sempre `MINED` aqui). |
| `rejection_reason` | Primeiro critério de seleção que falhou (vazio aqui). |
| `last_error` | Último erro da mineração. |
| `attempts` | Tentativas de mineração. |
| `discovered_at` | Quando foi descoberto (UTC). |
| `metadata_fetched_at` | Quando os metadados foram lidos (UTC). |
| `default_branch` | Branch default. |
| `gh_created_at` | Criação do repositório no GitHub (UTC). |
| `pushed_at` | Último push (UTC). |
| `stars` | Estrelas na seleção. |
| `forks` | Forks na seleção. |
| `watchers` | Watchers na seleção. |
| `disk_usage_kb` | Tamanho informado pelo GitHub (KB). |
| `primary_language` | Linguagem principal (GitHub Linguist). |
| `topics` | Tópicos do repositório. |
| `license_spdx` | Licença (identificador SPDX informado pelo GitHub). |
| `description` | Descrição do repositório. |
| `is_fork` | É fork. |
| `is_archived` | Está arquivado. |
| `is_template` | É template. |
| `is_mirror` | É espelho. |
| `commits_count` | Commits do branch default (GitHub). |
| `last_commit_at` | Data do commit HEAD do branch default (critério de atividade). |
| `mined_head_sha` | Commit HEAD minerado. |
| `mined_at` | Quando foi minerado (UTC). |
| `mined_run_id` | Execução que minerou o repositório. |
| `has_claude_md` | Tem um arquivo de instruções do Claude na amostra. |
| `has_agents_md` | Tem um `AGENTS.md` na amostra. |
| `instruction_files` | Arquivos de instruções na amostra. |
| `nested_instruction_files` | Arquivos de instruções da amostra fora da raiz. |
| `skills` | Skills distintas (pastas) na amostra. |
| `skill_files` | Arquivos de Skills (`SKILL.md` e recursos) na amostra. |
| `instruction_words` | Palavras somadas dos arquivos de instruções da amostra. |
| `instruction_reading_ease` | Flesch médio dos arquivos de instruções da amostra. |
| `artifact_changes` | Mudanças nos artefatos da amostra. |
| `artifact_commits` | Commits que alteraram artefatos da amostra. |
| `first_artifact_change_at` | Primeira mudança num artefato da amostra (data de autoria). |
| `last_artifact_change_at` | Última mudança num artefato da amostra (data de autoria). |
| `artifact_burst_share` | Fração das mudanças em pico (`in_burst`). |
| `artifact_agent_share` | Fração das mudanças com agente de código envolvido. |
| `total_commits` | Commits somados de todas as identidades. |
| `contributors` | Contribuidores humanos. |
| `artifact_authors` | Identidades que alteraram algum artefato. |
| `active_months` | Meses com ao menos um commit. |
| `manual_only` | O repositório só entrou por `agm add-repo`. |
| `sample_artifacts` | Arquivos de instruções e `SKILL.md` da amostra (critério de tamanho). |
| `human_contributors` | Autores humanos com commits (critério de contribuidores). |
| `in_sample` | O repositório pertence à amostra do estudo. |
| `exclusion_reason` | Primeiro critério da análise que falhou: `min_artifact_bytes` ou `min_human_contributors`. |
| `commits_per_active_month` | `total_commits` ÷ `active_months`. |
