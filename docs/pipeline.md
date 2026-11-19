# Pipeline de coleta e análise

Como os cinco estágios funcionam e como usar o CLI numa coleta longa: onde o estado fica, como parar e retomar, como rodar em vários terminais e como acompanhar o progresso. A lista de todos os comandos está em [cli.md](cli.md).

## Etapas

```
agm discover → agm select → agm mine → agm export → agm analyze
 Code Search    GraphQL      git clone   Parquet       tabelas por RQ
 (candidatos)   (critérios)  (histórico) (bronze)      (silver/gold)
```

1. **discover**: busca arquivos na GitHub Code Search. O limite de 1000 resultados por query é
   contornado com bisseção do intervalo de tamanho (`size:`), e cada achado é registrado com a
   query que o encontrou.
2. **select**: busca os metadados dos repositórios via GraphQL e aplica os critérios do
   `study.yaml` ([critérios de amostragem](configuracao.md#critérios-de-amostragem)). Os rejeitados guardam o motivo, o que
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
   que dependem do histórico minerado); as silver guardam tudo. O conteúdo das versões é lido em
   lotes e as seções vão para o disco à medida que são geradas, então nem o conteúdo nem as
   seções ficam inteiros na memória; as demais tabelas do bronze (como `commits`, com as
   mensagens) ainda são carregadas, e o pico cresce com elas. Para garantir que um erro de memória derrube só o analyze, e
   não o VS Code junto (no WSL os dois dividem a mesma VM), rode com um teto:
   `systemd-run --user --scope -p MemoryMax=5G poetry run agm analyze`.

Toda execução fica registrada em `pipeline_runs`, com parâmetros, estatísticas, o snapshot do
`study.yaml` e o commit do código. Isso garante a rastreabilidade do dataset.

## Usando o CLI

### Onde o estado fica guardado

Todo o progresso fica **no Postgres**, não no processo. Um comando interrompido pode ser rodado
de novo a qualquer momento, inclusive em outro terminal, e continua de onde parou. O que marca o
progresso:

- o `status` de cada repositório: `DISCOVERED → SELECTED | REJECTED → MINED | FAILED`;
- a tabela `discovery_slices`, com o progresso da busca;
- a tabela `pipeline_runs`, com o histórico das execuções.

Dos estágios, só `export` e `analyze` escrevem arquivos (em `DATA_DIR`); os comandos de
resultados (`audit`, `rq1`, `rq2`, `figures`) também gravam lá, cada um na sua pasta.

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
