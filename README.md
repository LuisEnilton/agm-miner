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
   `study.yaml` (seção "Critérios de amostragem" abaixo). Os rejeitados guardam o motivo, o que
   gera o funil de seleção do artigo (`agm runs funnel`).
3. **mine**: faz um clone parcial (sem blobs) de cada repositório selecionado e extrai o histórico
   completo dos artefatos, o conteúdo de cada versão, renomeações/cópias, os arquivos alterados em
   cada commit, trailers `Co-authored-by` e o perfil de atividade de cada contribuidor. A
   mineração é idempotente, incremental, pode rodar em paralelo e registra falhas.
4. **export**: grava todas as tabelas em `data/bronze/<timestamp>/` em Parquet, com um
   `manifest.json` (versão do código, do schema e da configuração do estudo).
5. **analyze**: a partir de um export bronze, sem precisar do banco, gera em
   `data/gold/<timestamp>/` as tabelas `rq1_sections`, `rq2_structure`, `rq3_changes`,
   `rq3_author_roles` e `repo_features`. As tabelas gold contêm só a amostra do estudo (critérios
   que dependem do histórico minerado); as silver guardam tudo.

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

## Usando o CLI

### Onde o estado fica guardado

Todo o progresso fica **no Postgres**, não no processo. Um comando interrompido pode ser rodado
de novo a qualquer momento, inclusive em outro terminal, e continua de onde parou. O que marca o
progresso:

- o `status` de cada repositório: `DISCOVERED → SELECTED | REJECTED → MINED | FAILED`;
- a tabela `discovery_slices`, com o progresso da busca;
- a tabela `pipeline_runs`, com o histórico das execuções.

Só `export` e `analyze` escrevem arquivos (em `DATA_DIR`).

Os dados do Postgres ficam no volume Docker `pgdata`. `docker compose down` mantém o volume;
**`docker compose down -v` apaga tudo**.

### Posso parar a qualquer momento?

Sim. Ctrl+C e `kill <pid>` (SIGTERM) têm o mesmo efeito. O comando grava o que já terminou, marca
a execução como `INTERRUPTED` e sai com o código `130`, mostrando a mensagem
`interrupted: completed work is saved; run the same command again to continue`.

| Comando | O que é salvo durante a execução | Ao interromper | Para continuar |
|---|---|---|---|
| `discover` | Cada página de resultados e cada fatia da busca concluída (`discovery_slices`) | A fatia em andamento é refeita; as concluídas não | `agm discover` de novo (`--restart` recomeça do zero) |
| `select` | Cada lote de 25 repositórios | O lote em andamento é descartado | `agm select` de novo (processa o que ainda está `DISCOVERED`) |
| `mine` | Cada repositório, inteiro, numa única transação | **1º Ctrl+C**: nenhum repositório novo começa, os em andamento terminam e são salvos. **2º Ctrl+C**: aborta os em andamento, que voltam ao status anterior, sem virar `FAILED` | `agm mine` de novo |
| `export` | Nada até o fim (a pasta fica como `.partial`) | A pasta parcial é apagada; nenhum export incompleto fica visível | `agm export` de novo |
| `analyze` | Nada até o fim | A saída anterior continua intacta | `agm analyze` de novo |

Um repositório nunca fica pela metade no banco: ou todos os dados dele são gravados, ou nenhum.

Se o processo for morto sem chance de reagir (`kill -9`, queda de energia), os dados continuam
consistentes pelo mesmo motivo. A única diferença é que a execução fica como `RUNNING` em
`agm runs list`, com `"stale": true`, e o `agm status` a mostra como "no sign of life". Basta
rodar o comando de novo. Para tirar essas execuções do `agm status`, `agm runs cleanup` as registra
como `INTERRUPTED` (só as que estão sem sinal de vida há mais de `--minutes`, padrão 60; um
processo apenas suspenso, como num notebook que dormiu, também fica em silêncio).

### Posso rodar em dois terminais?

| Combinação | Pode? | Por quê |
|---|---|---|
| `mine` + `mine` (ou mais) | Sim | Cada repositório tem um lock no Postgres: os processos dividem o trabalho sem minerar o mesmo repositório duas vezes. Funciona também em máquinas diferentes apontando para o mesmo banco. |
| `discover` + `discover` | Não | Os dois disputariam a mesma quota do token. O segundo sai com `error: Another agm discover is already running`. |
| `select` + `select` | Não | Mesmo motivo; o segundo é recusado. |
| `discover` + `select` + `mine` | Sim | Usam cotas diferentes da API (Code Search × GraphQL × clone git) e formam um pipeline: o `select` processa o que o `discover` já encontrou, e o `mine` o que o `select` já aprovou. |
| `export` durante um `mine` | Sim | O export lê todas as tabelas de um único snapshot consistente. Repositórios minerados depois dele entram no próximo export. |
| `analyze` a qualquer momento | Sim | Lê apenas um export em disco, não o banco. |

Exemplo de coleta longa em três terminais:

```bash
# Terminal 1: busca (pode levar horas; cada requisição de busca espera 6,5 s)
poetry run agm discover

# Terminal 2: repita de tempos em tempos para aprovar o que já foi encontrado
poetry run agm select

# Terminal 3: minera em paralelo o que já foi aprovado
poetry run agm mine --workers 4

# Em qualquer terminal: acompanhe o andamento
poetry run agm status --watch # o que está rodando agora e em que ponto (atualiza ao vivo)
poetry run agm runs list      # execuções, status (RUNNING/SUCCEEDED/INTERRUPTED/FAILED) e estatísticas
poetry run agm runs funnel    # quantos repositórios em cada status e por que foram rejeitados
poetry run agm runs cleanup   # registra como INTERRUPTED as execuções mortas (sem sinal de vida)
```

### Acompanhando o progresso

**No terminal do comando.** Num terminal interativo, todo comando de estágio (`discover`,
`select`, `mine`, `export`, `analyze`) mostra um painel que se atualiza no lugar, com as linhas de
log rolando acima dele:

```
mine  running for 1m14s  pid 43079@meu-pc
  mining 3 repositories with 2 worker(s)
  repositories  ━━━━━━━━━━━━━━━━━━╸━━━━━━━━━  2/3 (67%)  1.6/min  eta 37s
  mined                                       2          1.6/min
    worker      item                          phase             detail          time
    worker_0    acme/api                      prefetch blobs    500/698 blobs   1m14s
    worker_1    acme/web                      history           12,000 commits  20s
```

O que cada estágio mostra:

| Estágio | Contadores | Fase / workers |
|---|---|---|
| `discover` | queries; `matches (est.)` = resultados percorridos ÷ total estimado da query (o `total_count` da faixa de tamanho inteira); `unreachable`; repositórios novos | fatia e página atuais; a espera da API em amarelo (pacing de 6,5 s, quota esgotada, retry), com o tempo que falta |
| `select` | repositórios avaliados / pendentes, selecionados, rejeitados | lote atual; espera da API |
| `mine` | repositórios concluídos / candidatos, e cada desfecho (`mined`, `failed`, `unchanged`...) | cada worker com o repositório, o passo (`clone`, `authors`, `history`, `prefetch blobs`, `rename lineage`, `line counts`, `commit metadata`, `contents`, `saving`), o detalhe (commits lidos, blobs baixados) e o tempo |
| `export` | tabelas e linhas (com total, contado no início) | tabela atual |
| `analyze` | passos (8) e versões analisadas | passo atual |

Quando a saída não é um terminal (redirecionada para um arquivo, `nohup`, CI), não há painel: a
cada 30 s sai uma linha de resumo no log, por exemplo
`[mine] · repositories 2/3 · working on acme/api (prefetch blobs)`. A variável `AGM_PROGRESS`
(no `.env`) força o modo: `auto` (padrão), `live`, `plain` ou `off`.

**De outro terminal.** Enquanto roda, todo estágio que grava em `pipeline_runs` (`discover`, `select`, `mine`, `export`)
salva no banco, a cada 5 segundos, o seu progresso, as estatísticas parciais e um sinal de vida
(`heartbeat_at`). Por isso dá para acompanhar de qualquer terminal, ou de outra máquina que use o
mesmo banco:

```bash
poetry run agm status            # mostra uma vez
poetry run agm status --watch    # atualiza a cada 2 s até Ctrl+C (--interval muda o intervalo)
poetry run agm status --json     # o mesmo em JSON, para scripts
```

Para cada execução em andamento, o `agm status` mostra:
- o estágio, há quanto tempo roda e o pid/host do processo;
- o que está fazendo e, quando está parado, por quê (por exemplo, esperando a quota da API);
- contadores com total, porcentagem, taxa por minuto e tempo estimado;
- o que cada worker está processando, em que fase e há quanto tempo.

No fim aparecem as contagens de repositórios por status.

Uma execução `RUNNING` sem sinal de vida há mais de 15 segundos aparece como **"no sign of life:
the process was most likely killed"**. Isso acontece depois de um `kill -9`, de uma queda de
energia ou de um processo que travou. O `agm runs list` mostra o mesmo com o campo `stale`. Os
dados continuam consistentes; basta rodar o comando de novo.

O `analyze` não aparece no `agm status`, porque não usa o banco.

### Coleta automática com `agm run`

`agm run` faz a coleta da seção acima sozinho, num único terminal:

```bash
poetry run agm run --workers 4
```

Ele executa cada estágio como um processo `agm <estágio>` separado e decide quando lançar cada
um a partir dos status no banco:

1. `discover` roda uma vez;
2. `select` é relançado, no máximo a cada `--select-interval` segundos (padrão 60), enquanto
   houver repositórios `DISCOVERED`;
3. `mine --workers N` é relançado, no máximo a cada `--mine-interval` segundos (padrão 60),
   enquanto houver repositórios `SELECTED`;
4. quando a busca termina e não sobra nada para selecionar nem minerar, roda `export` e depois
   `analyze`.

A saída de cada estágio aparece com um prefixo (`[discover]`, `[select]`, `[mine]`...).

- **Num terminal interativo**, embaixo dessa saída fica um painel ao vivo, com o progresso de cada
  estágio em andamento (o mesmo do `agm status`: contadores, espera da API, workers do `mine`) e as
  contagens de repositórios por status. Os estágios gravam o progresso no banco, e o painel lê de lá.
  O `analyze` aparece no painel, mas sem progresso, porque não usa o banco.
- **Sem terminal** (saída redirecionada para um arquivo, por exemplo), a cada `--status-interval`
  segundos sai uma linha `[run] status: ...` com as contagens e os estágios rodando, seguida de uma
  linha `[run] progress [mine] · repositories 12/40 · working on ...` por estágio. Essas linhas
  também saem durante o export e o analyze.

No fim, é impresso um resumo em JSON.

Como tudo é feito pelos comandos individuais, todas as garantias acima continuam valendo:

- **Ctrl+C / `kill`**: o sinal é repassado uma vez a cada estágio em execução. 1× para com calma
  (o `mine` termina os repositórios em andamento), 2× aborta. O `agm run` sai com o código `130`,
  e basta rodá-lo de novo para continuar: a busca retoma pelas fatias pendentes, e os repositórios
  já minerados são pulados.
- **Falha de um estágio** (ex.: token inválido): nada novo é iniciado, os estágios em execução
  terminam, e o `agm run` sai com o código `1`, sem exportar. O resumo indica o estágio em
  `failed_stage`.
- **Instância única**: um segundo `agm run` no mesmo banco falha logo no início, porque o
  `discover` dele é recusado. Para acelerar a mineração, abra `agm mine` em outros terminais: eles
  dividem o trabalho com o `agm run`.

| Opção | Efeito |
|---|---|
| `--workers N` | Repositórios minerados em paralelo por cada `mine` (padrão 4). |
| `--no-discover` | Não busca; só processa o que já está `DISCOVERED`/`SELECTED` (esvaziar a fila). |
| `-q/--query "..."` | Usa outra query no `discover` no lugar das do `study.yaml`. |
| `--no-export` | Termina sem exportar. Nesse caso o `analyze` também não roda, para não analisar um export antigo. |
| `--no-analyze` | Exporta, mas não analisa. |
| `--select-interval`, `--mine-interval`, `--status-interval` | Intervalos em segundos (veja acima). |

### Opções úteis

| Comando | Opções |
|---|---|
| `discover` | `-q/--query "..."` usa outra query no lugar das do `study.yaml`. `--restart` esquece o progresso salvo. |
| `select` | `--limit N` processa só N repositórios. `--reevaluate` reaplica os critérios atuais a todos os repositórios, inclusive os minerados (use depois de mudar o `study.yaml`). |
| `mine` | `--workers N` minera N repositórios em paralelo. `--limit N` limita a quantidade. `--repo owner/name` minera só os repositórios indicados, com qualquer status. `--retry-failed` tenta de novo os `FAILED`. `--refresh` minera de novo os `MINED` cujo HEAD mudou. `--force` minera de novo mesmo sem mudança. `--keep-clones` mantém os clones em `CLONE_DIR`. |
| `export` | `--output DIR` define a pasta de destino (padrão: `DATA_DIR/bronze`). |
| `analyze` | `--bronze DIR` escolhe o export (padrão: o mais recente). `--output DIR` define a saída. |

Todos aceitam `--study caminho.yaml` para usar outra configuração do estudo.

Códigos de saída: `0` sucesso; `1` erro esperado, com mensagem de uma linha (token inválido,
repositório desconhecido, estágio já em execução); `130` interrompido.

Repositórios `FAILED` guardam o motivo em `last_error` e quantas tentativas já houve em
`attempts`. Eles só são tentados de novo com `--retry-failed`.

### Banco criado por uma versão antiga

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

## Configuração do estudo

Os parâmetros metodológicos ficam em [study.yaml](study.yaml): queries, critérios de seleção,
limites de mineração, padrões de bots e agentes, janela de pico. Mude os valores ali, não no
código. Depois de alterar os critérios do `select`, rode `agm select --reevaluate` para reaplicá-los
a todos os repositórios, inclusive os já minerados. Os critérios da análise só exigem rodar
`agm analyze` de novo. `selection.reference_date` fica fixa durante toda a coleta, senão o critério
de atividade depende da data em que o comando rodou.

### Critérios de amostragem

Aplicados em ordem; o primeiro que falha é o motivo gravado.

| Critério | Valor | Estágio | Motivo de exclusão |
|---|---|---|---|
| Não vazio, não fork, não arquivado, não espelho, não template | — | select | `empty`, `fork`, `archived`, `mirror`, `template` |
| Criado antes do corte | 2026-08-01 | select | `created_after_cutoff` |
| Licença aprovada pela OSI | lista em `config.py` | select | `license` |
| Linguagem principal é de programação (não Markdown, HTML ou prosa) | — | select | `no_code_language` |
| Estrelas | ≥ 50 | select | `min_stars` |
| Commits no branch default | ≥ 10 | select | `min_commits` |
| Último commit do branch default | até 365 dias antes de `reference_date` | select | `inactive` |
| Nome, descrição ou tópicos sem palavras de tutorial/lista | `exclude_keywords` | select | `keyword:<palavra>` |
| Arquivo de instrução ou `SKILL.md` com tamanho mínimo (última versão, sem symlinks) | ≥ 2048 bytes | analyze | `min_artifact_bytes` |
| Autores humanos (sem bots nem agentes de código) no histórico inteiro | ≥ 2 | analyze | `min_human_contributors` |

Origem das escolhas:
- as licenças OSI, os ≥ 2 contribuidores, a atividade medida por commits (e não por push) e a
  existência de um corte de criação seguem Mohsenimofidi et al. (*Context Engineering for AI
  Agents in Open-Source Software*, 2025) e Galster et al. (*Harness Engineering for Agentic AI
  Coding Tools*, 2026);
- a data do corte é diferente da desses artigos. Eles usam 2024-01-01, para estudar só projetos
  anteriores às ferramentas de agentes. Este estudo usa 2026-08-01: inclui projetos criados já com
  agentes e deixa de fora só os criados nas últimas semanas antes da `reference_date`, cujo
  histórico é curto demais para a RQ3;
- a exclusão de tutoriais, listas e templates segue Santos et al. (*Decoding the
  Configuration of AI Coding Agents*, 2025).

Os critérios da análise saem no `analysis_manifest.json` (`sample`) e nas colunas `in_sample` e
`exclusion_reason` de `repo_features`, continuando o funil do `select`. O `discover` já começa a
busca em `size_min: 2048`, porque arquivos menores nunca entram na amostra.

### Como configurar cada parâmetro

Todos os parâmetros ficam no [study.yaml](study.yaml), na raiz do repositório. Uma chave ausente
usa o valor padrão de [config.py](src/agm_miner/config.py), e uma chave desconhecida (por exemplo,
um erro de digitação) faz o comando parar com erro. Para testar outra configuração sem mexer no
arquivo principal, copie-o e passe a cópia com `--study` (`agm select --study outro.yaml`) ou pela
variável `STUDY_CONFIG_PATH` no `.env`.

Depois de mudar um parâmetro, rode de novo o estágio dele:

| Seção do `study.yaml` | O que rodar depois da mudança |
|---|---|
| `selection` | `poetry run agm select --reevaluate` (busca os metadados de novo e reaplica os critérios a todos os repositórios, inclusive os já minerados) |
| `analysis` | `poetry run agm analyze` (não precisa minerar nem exportar de novo) |
| `discovery.size_min` | `poetry run agm discover` (o progresso salvo é por intervalo de tamanho, então os intervalos novos são buscados; o que já foi encontrado continua no banco) |

#### As duas datas de corte

O estudo usa duas datas diferentes, ambas na seção `selection`:

```yaml
selection:
  created_before: 2026-08-01    # data de corte de CRIAÇÃO do repositório
  reference_date: 2026-09-23    # data de referência da ATIVIDADE (início da coleta)
  active_within_days: 365
```

- **`created_before`** é a data de corte de criação: só entram repositórios **criados antes**
  dela (a data em si já fica de fora). Para mudar o corte, troque o valor, por exemplo
  `created_before: 2024-01-01` para usar o corte dos artigos relacionados. Para desligar o
  critério, use `created_before: null`. Sem a chave no `study.yaml`, vale o padrão de `config.py`
  (2024-01-01).
- **`reference_date`** é a data a partir da qual a atividade é contada para trás: um repositório é
  ativo quando o último commit do branch default é, no máximo, `active_within_days` dias anterior
  a ela. Com os valores acima, ele precisa de um commit a partir de 2025-09-23. Use a data de início
  da coleta final e **não a mude durante a coleta**. Com `null`, vale a data do dia em que o `select`
  roda, e o resultado muda de um dia para o outro (a data efetiva fica em `pipeline_runs.params`).
- **`active_within_days`** é o tamanho da janela, em dias. Com `null`, o critério de atividade é
  desligado.

As datas são escritas no formato `AAAA-MM-DD`, sem aspas.

#### Licenças

```yaml
selection:
  allowed_licenses: [MIT, Apache-2.0, BSD-3-Clause, GPL-3.0]
```

Os valores são identificadores SPDX, como o GitHub os informa (maiúsculas não importam). Declarar a
chave **substitui** a lista padrão inteira (34 licenças aprovadas pela OSI, em `config.py`), então
liste todas as que devem ser aceitas. `allowed_licenses: null` aceita qualquer repositório, inclusive
os sem licença. Repositórios cuja licença o GitHub não reconhece aparecem como `NOASSERTION` e são
rejeitados, a menos que você inclua esse valor na lista.

#### Linguagem principal

```yaml
selection:
  require_code_language: true
  non_code_languages: [Markdown, MDX, HTML, reStructuredText, AsciiDoc, Text]
```

A linguagem principal é a que o GitHub mostra no repositório. Ela é rejeitada quando falta ou
está em `non_code_languages` (nomes como o GitHub os escreve; maiúsculas não importam). Para
também excluir, por exemplo, repositórios de CSS ou de TeX, acrescente `CSS` ou `TeX` à lista.
`require_code_language: false` desliga o critério.

#### Palavras de tutorial/lista

```yaml
selection:
  exclude_keywords: [awesome, tutorial, course, boilerplate, cheatsheet, starter, dotfiles]
```

As palavras são procuradas inteiras, no singular ou no plural, no nome, na descrição e nos tópicos
do repositório. Declarar a chave substitui a lista padrão. Evite palavras ambíguas, como
`example` ou `template`, que também rejeitam software real.

#### Tamanho mínimo do artefato e contribuidores humanos

```yaml
analysis:
  min_artifact_bytes: 2048
  min_human_contributors: 2
```

- **`min_artifact_bytes`**: tamanho mínimo, em bytes, da última versão de um arquivo de
  instrução (`CLAUDE.md`, `AGENTS.md`) ou de um `SKILL.md`. Com `0`, todos os artefatos entram.
  Os demais arquivos de uma skill entram quando o `SKILL.md` da **mesma pasta** entra; duas skills
  com o mesmo nome em pastas diferentes (por exemplo, pacotes de um monorepo) são tratadas
  separadamente.
  Se você **diminuir** esse valor, diminua também `discovery.size_min`, senão a busca não encontra
  os arquivos menores.
- **`min_human_contributors`**: mínimo de autores humanos no histórico inteiro do repositório.
  Bots (`analysis.bot_patterns`) e agentes de código que aparecem como autores dos commits
  (`analysis.agent_patterns`, por exemplo `Claude <noreply@anthropic.com>`) não contam. Em
  `rq3_author_roles`, os agentes recebem o papel `agent`, e `repo_features.contributors` também só
  conta humanos. Com `1`, o critério só exclui repositórios sem nenhum autor humano.

Como esses dois critérios são aplicados na análise, dá para medir o efeito de outro limite
(análise de sensibilidade) rodando `agm analyze --study outro.yaml --output <outra pasta>`
sobre o mesmo export.

## RQ1: categorias de conteúdo

A RQ1 é respondida em duas fases sobre as seções da tabela `rq1_sections` (saída do `agm analyze`):

1. **Criação dos labels** numa amostra estratificada: o Claude Opus 5.5 propõe rótulos livres para
   cada seção e depois os consolida num rascunho de codebook, usando como referência os 16 rótulos
   do *Agent READMEs* (Chatlatanagulchai et al., 2025, em `codebooks/seed_agent_readmes.yaml`). Você
   revisa o rascunho e salva a versão final em `codebooks/rq1.yaml`.
2. **Classificação** de todas as seções com o Claude Sonnet 5.5 contra o codebook final, validada
   numa amostra cega rotulada à mão (kappa de Cohen).

Os comandos ficam em `agm rq1 ...` e usam a seção `rq1:` do `study.yaml`. Só um comando por vez
trabalha em cada diretório da RQ1: um segundo é recusado, para não enviar os mesmos pedidos (e
pagar) duas vezes. A amostra depende só do conteúdo, da semente e da configuração, então é a mesma
em qualquer export dos mesmos dados. Assim como a análise, eles
não usam o banco: leem a saída do `analyze` e gravam em `DATA_DIR/rq1/<nome da saída>/`, com um
manifesto ao lado de cada resultado. As respostas do LLM ficam em cache em disco, e os lotes da
Batches API ficam registrados, então um comando interrompido continua de onde parou sem pagar de
novo pelo que já foi respondido.

### Provedores: testar de graça, coletar com o Claude

O provedor dos modelos é escolhido em `rq1.provider` no `study.yaml`:

| | `gemini` (fase de testes, configuração atual) | `openrouter` (alternativa para testes) | `anthropic` (coleta final) |
|---|---|---|---|
| Modelos | `gemini-3.5-flash-lite` (nível gratuito) | modelos gratuitos, ex. `nvidia/nemotron-3.5-super-vl:free` | Claude Opus 5.5 (labels) e Claude Sonnet 5.5 (classificação) |
| Credencial | `GEMINI_API_KEY` ([AI Studio](https://aistudio.google.com/apikey)) | `OPENROUTER_API_KEY` | `ANTHROPIC_API_KEY` ou `ant auth login` |
| Muitos pedidos | um a um, `rq1.gemini_requests_per_minute` por minuto | um a um, 20 por minuto | Batches API (metade do preço, assíncrona) |
| Limite | por projeto, visto em https://aistudio.google.com/rate-limit; o diário renova à meia-noite do Pacífico | 50 requisições por dia (1.000 depois de comprar 10 créditos) | o da conta |

Ao atingir o limite diário, o comando para com uma mensagem e mantém tudo o que já foi
respondido; basta rodar o mesmo comando depois que o limite renovar.

**Uso dos dados nos níveis gratuitos:** o Google usa prompts e respostas do nível gratuito para
melhorar seus produtos, e revisores humanos podem lê-los (desvinculados da conta). Os modelos
gratuitos do OpenRouter também podem registrar os prompts; se ele responder que não há endpoint
compatível com a sua política de dados, libere os modelos gratuitos em
https://openrouter.ai/settings/privacy. O conteúdo enviado vem de repositórios públicos, mas não
use esses provedores com dados privados.

**Para passar à coleta final**, troque `provider` para `anthropic` e use os modelos indicados nos
comentários do `study.yaml`. O codebook deve ser refeito com o Claude, porque os labels do artigo
precisam vir dos modelos finais. As respostas ficam separadas por provedor e modelo no cache e nas
pastas de saída, então os testes nunca se misturam com a coleta.

```bash
poetry run agm rq1 models            # modelos do provedor configurado (--provider gemini|openrouter)
poetry run agm rq1 smoke             # 1 requisição de teste: confere chave, modelo e schema
                                     # (--phase propose|classify, --model provedor:modelo)
poetry run agm rq1 sample            # amostra para a criação dos labels (rq1.sample_size seções)
poetry run agm rq1 propose           # rótulos livres para cada seção da amostra
poetry run agm rq1 consolidate       # rascunho do codebook a partir dos rótulos propostos
poetry run agm rq1 classify          # categorias de todas as seções (codebook de rq1.codebook)
poetry run agm rq1 saturation        # amostra nova: quantas seções não cabem em nenhuma categoria
poetry run agm rq1 validate export   # planilha cega para rotular à mão
poetry run agm rq1 validate llm      # rótulos do LLM na amostra de validação (--replicate 2)
poetry run agm rq1 validate score    # concordância humano × LLM e LLM × LLM (--human planilha.csv)
poetry run agm rq1 compare --model A --model B   # dois classificadores comparados (provedor:modelo)
```

### Criação dos labels

1. **`agm rq1 sample`** sorteia a amostra (estratificada por ferramenta, tipo e tamanho, com no
   máximo `rq1.per_repo_cap` seções por repositório). Trocar uma amostra que já tem propostas exige
   `--force`, porque as propostas deixariam de corresponder a ela.
2. **`agm rq1 propose`** envia as seções em grupos de `rq1.propose_chunk_sections` (10). Para cada
   seção o modelo propõe de 1 a 3 rótulos com definição e diz se ela instrui o agente ou só descreve
   o projeto. Saída: `propose/<provedor>/<modelo>/proposals.parquet` e `proposals.json`.
3. **`agm rq1 consolidate`** transforma os rótulos (normalizados, com contagens e definições) num
   rascunho de codebook, em três etapas:
   - **induzir:** o modelo agrupa os rótulos em `rq1.target_categories` categorias, com definição e
     critérios de inclusão e exclusão, **sem ver** a taxonomia de referência. No piloto, quando a
     referência aparecia junto com os rótulos, o modelo simplesmente a copiava;
   - **alinhar:** numa requisição separada, cada categoria induzida é associada à categoria
     correspondente do *Agent READMEs* (`rq1.seed_codebook`) ou marcada como nova;
   - **mapear:** cada rótulo proposto é atribuído a uma categoria (ou a `other`), em lotes de
     `rq1.map_chunk_labels`.

   Saídas em `consolidate/<provedor>/<modelo>/`: `codebook.draft.yaml` (categorias, com a
   correspondência na literatura) e `label_map.parquet` (rótulo → categoria). O comando recusa
   propostas de outra amostra e propostas que cobrem menos de `rq1.min_proposal_coverage` da
   amostra (use `--partial` para aceitar).
4. **Curadoria (manual):** revise o rascunho (categorias redundantes, definições vagas, rótulos que
   caíram em `other`) e salve a versão final em `codebooks/rq1.yaml`, com um `version` próprio. É
   esse arquivo, versionado no repositório, que a classificação usa.

Respostas que não seguem o formato pedido (seção faltando, sem rótulo, JSON inválido) não ficam no
cache e são pedidas de novo na próxima execução. Depois de `rq1.max_attempts` falhas, o pedido é
abandonado e aparece em `given_up` no manifesto, para não consumir a cota todo dia. Os prompts têm
versão (`propose-v2`, `induce-v1`, `align-v1`, `map-v1`): mudar o texto de um prompt exige mudar a versão.

### Classificação e validação

5. **`agm rq1 classify`** classifica todas as seções com texto contra o codebook de
   `rq1.codebook` (`--codebook` para outro). Seções idênticas são classificadas uma vez só. Cada
   requisição leva até `rq1.chunk_sections` seções de um mesmo arquivo; o `system` traz as
   instruções e o codebook inteiro (igual em todas as requisições) e avisa que o conteúdo das
   seções é dado a classificar, não instrução a seguir. Cada seção recebe uma ou mais categorias,
   ou `other`. `--limit N` faz um piloto com N requisições. Saídas em
   `classify/<codebook>/<provedor>/<modelo>/`:
   - `section_labels.parquet`: uma linha por seção e categoria;
   - `artifact_labels.parquet`: por arquivo e categoria, seções, palavras e fração das palavras;
   - `repo_labels.parquet`: o mesmo por repositório, para juntar ao `repo_features` pelo
     `repo_id` na análise de correlação;
   - `label_prevalence.parquet`: fração de arquivos e de seções com cada categoria, por
     ferramenta e tipo de arquivo.
6. **`agm rq1 saturation`** classifica `rq1.saturation_size` seções que não estavam na amostra de
   criação dos labels e mede quantas caem em `other` e quais categorias não aparecem.
7. **Validação:**
   - `agm rq1 validate export` gera `validation/<codebook>/sheet.csv`, uma amostra nova de
     `rq1.validation_size` seções (`--size` para outro tamanho) com uma coluna por categoria.
     Ela não mostra as respostas do LLM. Marque `1` (ou `x`) em cada categoria que se aplica e
     `other` só quando nenhuma se aplica; linhas em branco são ignoradas. O comando nunca
     sobrescreve uma planilha existente sem `--force`;
   - `agm rq1 validate llm` classifica a mesma amostra; `--replicate 2` repete a classificação
     de forma independente, para medir a estabilidade (as APIs não aceitam fixar a amostragem);
   - `agm rq1 validate score --human planilha.csv` calcula, por categoria, kappa de Cohen,
     precisão, revocação e F1, com médias e intervalos de confiança de 95% por bootstrap, para
     humano × LLM, LLM × LLM e, com `--retest`, humano × humano (você rotula de novo parte da
     planilha depois de alguns dias). Sem `--human`, mede só a estabilidade. Saídas:
     `scores.json` e `scores.md`, na pasta do modelo (`validation/<codebook>/llm/<provedor>/
     <modelo>/`), ao lado dos rótulos dele; o score de um modelo nunca apaga o de outro.

### Comparar dois modelos

`classify`, `saturation`, `validate llm` e `validate score` aceitam `--model provedor:modelo`
(por exemplo `--model gemini:gemini-3.5-flash-lite`), que troca `rq1.provider` e
`rq1.classify_model` só naquela execução, sem outro `study.yaml`. A troca fica registrada no
manifesto de cada saída. As respostas e as saídas de cada modelo ficam em pastas separadas.

```bash
# Modelo A: o do study.yaml; modelo B: outro, de outra família
poetry run agm rq1 validate llm
poetry run agm rq1 validate llm --model gemini:gemini-3.5-flash-lite
poetry run agm rq1 classify
poetry run agm rq1 classify --model gemini:gemini-3.5-flash-lite

poetry run agm rq1 compare --model anthropic:claude-sonnet-5-5 \
  --model gemini:gemini-3.5-flash-lite --human planilha.csv
```

O `compare` usa o que existir para os dois modelos:

- **amostra de validação** (`validate llm --model`): humano × A e humano × B (com `--human`) e
  A × B, com kappa, precisão, revocação e F1 por categoria e intervalos de confiança;
- **todas as seções** (`classify --model`): A × B sobre todas as seções (sem bootstrap: com
  milhares de seções os intervalos seriam mínimos) e as prevalências dos dois lado a lado, por
  ferramenta, tipo de arquivo e categoria, com a diferença (`prevalence.parquet`). Ela mostra se as
  conclusões da RQ1 dependem do modelo.

Saídas em `compare/<codebook>/<A>__vs__<B>/`: `compare.json`, `compare.md` e
`prevalence.parquet`. O A é sempre o primeiro `--model`.

### Validação externa com o dataset do *Agent READMEs*

O *Agent READMEs* (Chatlatanagulchai et al., 2025) publicou 2.303 arquivos de contexto
(922 `CLAUDE.md`, 694 `AGENTS.md` e 687 `copilot-instructions.md`) e, no pacote de replicação,
os rótulos que os autores deram à mão a 332 deles, com as 16 categorias do artigo, junto com os
rótulos do GPT-5 para os mesmos arquivos. Classificar esses arquivos com o nosso método e o
codebook deles (`codebooks/seed_agent_readmes.yaml`) mede o classificador contra rótulos humanos
que não são nossos, com o GPT-5 do artigo como linha de base.

```bash
# Fontes, fixadas nas revisões usadas (Hugging Face e GitHub)
mkdir -p data/sources/agentreadmes && cd data/sources/agentreadmes
curl -LO https://huggingface.co/datasets/hao-li/AgentREADMEs/resolve/e5842f4eceb11e61f247212b04b692bfb397f8b7/data/train-00000-of-00001.parquet
P=https://raw.githubusercontent.com/woraamy/Agent-Context-File-Analysis/77b16a9a53c99efcda1c98c98c4afcd807f7227c/datasets/derived/classification/inputs
curl -LO $P/final_am_classification_3.csv
curl -LO $P/gpt_5_file_classifications.csv
cd -

# 1. Importa os arquivos e os rótulos para data/external/agentreadmes (formato da saída do analyze)
poetry run agm rq1 agentreadmes import data/sources/agentreadmes/train-00000-of-00001.parquet \
  --human-labels data/sources/agentreadmes/final_am_classification_3.csv \
  --gpt5-labels data/sources/agentreadmes/gpt_5_file_classifications.csv
# 2. Classifica com o codebook do artigo (qualquer comando agm rq1 aceita --gold). --only-labelled
#    classifica só os 325 arquivos rotulados (cerca de 1/7 das seções), que é o que o score usa;
#    --limit 5 antes faz um piloto para conferir o custo
poetry run agm rq1 classify --gold data/external/agentreadmes \
  --codebook codebooks/seed_agent_readmes.yaml --only-labelled --model anthropic:claude-sonnet-5-5
# 3. Concordância por arquivo: humano × nosso classificador, humano × GPT-5, GPT-5 × nosso
poetry run agm rq1 agentreadmes score --model anthropic:claude-sonnet-5-5
poetry run agm rq1 agentreadmes score --model anthropic:claude-sonnet-5-5 \
  --tool CLAUDE --tool AGENTS   # só as ferramentas do estudo
```

- **O que a importação grava** (`data/external/agentreadmes/`): `rq1_sections.parquet`, com os
  arquivos quebrados em seções pelo mesmo parser do `analyze`; `files.parquet`, com URL e
  metadados de cada arquivo; `reference_labels.parquet`, com os rótulos humanos e do GPT-5 já nos
  ids do `seed_agent_readmes.yaml`; e um `analysis_manifest.json` com o SHA-256 de cada fonte. Os
  ids são derivados do conteúdo (URLs e nomes ordenados), então a mesma fonte gera sempre os
  mesmos ids. Um rótulo que não está na tabela de correspondência faz a importação parar.
- **Os 16 rótulos são aplicados por arquivo no artigo.** O nosso classificador rotula seções, e um
  arquivo recebe todas as categorias das suas seções (`other` não conta). Essa união tende a
  marcar mais categorias que um humano lendo o arquivo inteiro (mais revocação, menos precisão).
  Um arquivo sem nenhuma seção com texto fica sem categoria.
- **`--only-labelled`** filtra só a classificação; a importação continua com tudo. As requisições
  são montadas por arquivo, então classificar o dataset inteiro depois reaproveita do cache quase
  todas as respostas já pagas (só muda a requisição de um arquivo rotulado que tem seção idêntica
  à de um arquivo não rotulado, que passa a representá-la).
- **Arquivos ficam de fora da comparação** quando não estão no dataset (7 dos 332 rotulados) ou
  quando a classificação não terminou (`unclassified_files`, por exemplo depois de um
  `classify --limit`). As três comparações usam sempre o mesmo conjunto de arquivos.
- **O `copilot-instructions.md` é importado** (ferramenta `COPILOT`), porque a validação mede o
  classificador e independe da ferramenta: tirá-lo deixaria 202 arquivos rotulados em vez de 325 e
  as categorias raras quase sem exemplos. O resultado sai também por ferramenta, e `--tool`
  restringe o score às ferramentas do estudo, para comparar com o nosso corpus.
- **Conferência da correspondência dos rótulos:** com os 332 arquivos, o `agreement` deste projeto
  aplicado a humano × GPT-5 reproduz o relatório do artigo (precisão micro 0,7326, revocação micro
  0,8632, F1 micro 0,7925 e F1 macro 0,7270).
- Saídas em `DATA_DIR/rq1/agentreadmes/agentreadmes/<codebook>/<provedor>/<modelo>/`:
  `scores.json`, `scores.md` e o manifesto (ou `scores-AGENTS-CLAUDE.*` com `--tool`).
- O dataset e o pacote de replicação não declaram licença: cite o artigo e não redistribua os
  arquivos.

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
- O corte de criação (2026-08-01) é mais tardio que o dos artigos relacionados (2024-01-01), então
  a amostra mistura projetos anteriores às ferramentas de agentes e projetos criados já com elas.
  Para comparar com esses artigos, separe os dois grupos por `gh_created_at` em `repo_features`
  ou repita a seleção com `created_before: 2024-01-01`.
- A linguagem principal vem do GitHub Linguist, que não conta Markdown. Um repositório de
  documentação com um script pequeno ainda passa.
- A legibilidade (Flesch) só é calculada para textos em inglês.

Mais detalhes técnicos estão em [AGENTS.md](AGENTS.md).
