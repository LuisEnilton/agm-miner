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

## Configuração do estudo

Os parâmetros metodológicos ficam em [study.yaml](study.yaml): queries, critérios de seleção,
limites de mineração, padrões de bots e agentes, janela de pico. Mude os valores ali, não no
código. Depois de alterar os critérios, rode `agm select --reevaluate` para reaplicá-los a todos os
repositórios, inclusive os já minerados. **Antes da coleta final, fixe `selection.reference_date`**, senão o critério de atividade
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
