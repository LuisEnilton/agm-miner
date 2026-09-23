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
`agm runs list`. Basta rodar o comando de novo.

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
poetry run agm runs list      # execuções, status (RUNNING/SUCCEEDED/INTERRUPTED/FAILED) e estatísticas
poetry run agm runs funnel    # quantos repositórios em cada status e por que foram rejeitados
```

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

A saída de cada estágio aparece com um prefixo (`[discover]`, `[select]`, `[mine]`...). A cada
`--status-interval` segundos, uma linha `[run] status: ...` mostra quantos repositórios há em cada
status e quais estágios estão rodando. No fim, é impresso um resumo em JSON.

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
