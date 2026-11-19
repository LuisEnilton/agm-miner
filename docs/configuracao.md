# Configuração do estudo

Os parâmetros metodológicos ficam em [study.yaml](../study.yaml): queries, critérios de seleção,
limites de mineração, padrões de bots e agentes, janela de pico. Mude os valores ali, não no
código. Depois de alterar os critérios do `select`, rode `agm select --reevaluate` para reaplicá-los
a todos os repositórios, inclusive os já minerados. Os critérios da análise só exigem rodar
`agm analyze` de novo. `selection.reference_date` fica fixa durante toda a coleta, senão o critério
de atividade depende da data em que o comando rodou.

## Critérios de amostragem

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

## Como configurar cada parâmetro

Todos os parâmetros ficam no [study.yaml](../study.yaml), na raiz do repositório. Uma chave ausente
usa o valor padrão de [config.py](../src/agm_miner/config.py), e uma chave desconhecida (por exemplo,
um erro de digitação) faz o comando parar com erro. Para testar outra configuração sem mexer no
arquivo principal, copie-o e passe a cópia com `--study` (`agm select --study outro.yaml`) ou pela
variável `STUDY_CONFIG_PATH` no `.env`.

Depois de mudar um parâmetro, rode de novo o estágio dele:

| Seção do `study.yaml` | O que rodar depois da mudança |
|---|---|
| `selection` | `poetry run agm select --reevaluate` (busca os metadados de novo e reaplica os critérios a todos os repositórios, inclusive os já minerados) |
| `analysis` | `poetry run agm analyze` (não precisa minerar nem exportar de novo) |
| `discovery.size_min` | `poetry run agm discover` (o progresso salvo é por intervalo de tamanho, então os intervalos novos são buscados; o que já foi encontrado continua no banco) |

### As duas datas de corte

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

### Corte do histórico na data de referência

A mineração leva semanas, então cada repositório é clonado num dia diferente. Com
`analysis.truncate_at_reference_date: true` (o valor do `study.yaml`), o `analyze` corta o
histórico no fim da `reference_date` (meia-noite UTC do dia seguinte), como se todos os
repositórios tivessem sido minerados nesse instante:

- commits feitos depois do corte saem da análise, com as mudanças de artefatos, os arquivos e os
  co-autores deles;
- um artefato alterado depois do corte é analisado no estado da última mudança antes dele (ou
  some, se essa mudança foi uma remoção); um artefato criado depois do corte ainda não existia e
  sai da análise. Um artefato sem mudanças depois do corte fica como estava no HEAD minerado,
  inclusive os poucos sem nenhuma mudança registrada (alterados só em commits de merge);
- autores cujo primeiro commit é posterior ao corte saem da análise, e a atividade mensal termina
  no mês do corte.

O minerador continua gravando o histórico inteiro: o corte é feito só na análise, e o
`analysis_manifest.json` registra a data e o que foi removido (`history_cutoff`). Ele exige uma
`reference_date` fixa. Duas coisas não podem ser cortadas e precisam ser declaradas no artigo: o
total de commits de cada autor e o perfil de atividade dele (`identities.commits`,
`contributor_activity`, usados nos papéis de autor) cobrem o histórico inteiro do clone, e os
metadados do GitHub (estrelas, licença, linguagem) são do momento da seleção. Os commits são
datados por `committed_at`; um commit feito antes do corte e integrado ao branch depois dele
continua na análise.

### Licenças

```yaml
selection:
  allowed_licenses: [MIT, Apache-2.0, BSD-3-Clause, GPL-3.0]
```

Os valores são identificadores SPDX, como o GitHub os informa (maiúsculas não importam). Declarar a
chave **substitui** a lista padrão inteira (34 licenças aprovadas pela OSI, em `config.py`), então
liste todas as que devem ser aceitas. `allowed_licenses: null` aceita qualquer repositório, inclusive
os sem licença. Repositórios cuja licença o GitHub não reconhece aparecem como `NOASSERTION` e são
rejeitados, a menos que você inclua esse valor na lista.

### Linguagem principal

```yaml
selection:
  require_code_language: true
  non_code_languages: [Markdown, MDX, HTML, reStructuredText, AsciiDoc, Text]
```

A linguagem principal é a que o GitHub mostra no repositório. Ela é rejeitada quando falta ou
está em `non_code_languages` (nomes como o GitHub os escreve; maiúsculas não importam). Para
também excluir, por exemplo, repositórios de CSS ou de TeX, acrescente `CSS` ou `TeX` à lista.
`require_code_language: false` desliga o critério.

### Palavras de tutorial/lista

```yaml
selection:
  exclude_keywords: [awesome, tutorial, course, boilerplate, cheatsheet, starter, dotfiles]
```

As palavras são procuradas inteiras, no singular ou no plural, no nome, na descrição e nos tópicos
do repositório. Declarar a chave substitui a lista padrão. Evite palavras ambíguas, como
`example` ou `template`, que também rejeitam software real.

### Tamanho mínimo do artefato e contribuidores humanos

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
