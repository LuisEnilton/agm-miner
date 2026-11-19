# Auditoria da amostra e figuras do artigo

Os dois comandos leem só arquivos já gerados (o export bronze, a saída do `analyze` e a classificação da RQ1) e não usam o banco.

## Auditoria da amostra (vieses de coleta)

`agm audit` lê só um export bronze (como o `analyze`, sem banco) e mostra onde a descoberta, a
seleção e a mineração podem ter enviesado a amostra. Ele grava
`DATA_DIR/audit/<export>/audit.json` e `audit.md`. Rode de novo depois de cada `agm export`:

```bash
poetry run agm export && poetry run agm audit
```

O relatório tem quatro partes e uma lista de avisos (`warnings`) em linguagem direta:

- **descoberta**: para cada query do `study.yaml`, se ela foi buscada, qual faixa de tamanho foi
  coletada, que fração da população estimada pela API essa faixa cobre, quantas fatias passaram
  do limite de 1000 resultados e quantos resultados ficaram inacessíveis. Também mostra o que veio
  de fora do protocolo atual (outra faixa de tamanho ou query) e quantos repositórios mantidos só
  foram achados assim;
- **seleção**: o funil (primeiro critério que falhou) e, para cada critério, quantos
  repositórios falham nele e quantos falham **só** nele (entrariam na amostra sem ele). Mostra
  também os repositórios mais populares rejeitados só pela licença, as estrelas e o ano de criação
  dos mantidos, e os status gravados que a configuração atual não confirma (sinal para rodar
  `agm select --reevaluate`);
- **mineração**: falhas por tipo de erro, conteúdos omitidos, o período da mineração e quantos
  repositórios foram minerados depois da `reference_date`;
- **corte do histórico**: o que o corte na `reference_date` remove (commits, mudanças, artefatos
  que ainda não existiam ou estavam em outro estado, autores que ainda não tinham commitado),
  quantos repositórios foram minerados antes do fim da data e podem não ter alguns dias de
  histórico, os artefatos sem mudança registrada e os arquivos elegíveis no HEAD comparados aos
  elegíveis na data de corte. Os números aparecem mesmo com o corte desligado, mostrando o que
  ele removeria;
- **artefatos** (no HEAD dos repositórios minerados, antes do filtro de contribuidores humanos):
  por tipo de arquivo, symlinks, arquivos abaixo de `min_artifact_bytes`, arquivos elegíveis
  maiores que a faixa coletada da query daquele tipo, e repositórios cujos arquivos de um tipo só
  chegaram pela query de outro tipo. Mostra ainda a concentração (fração dos arquivos nos 1%
  repositórios com mais arquivos) e as duplicatas (blobs idênticos).

O critério de tamanho segue o da amostra (`analysis.min_artifact_bytes`, sem symlinks). A
configuração usada é a do `--study` (padrão `STUDY_CONFIG_PATH`), e o relatório avisa quando ela
difere da que foi gravada no export.

### Para o artigo (LaTeX)

A auditoria também grava dois arquivos LaTeX, em português e no formato do template da SBC
(tabelas só com `tabular` e `\hline`, números como `5.380` e `9,5\%`):

- `audit.tex`: uma macro por número (`\auditKept`, `\auditCoverageClaude`,
  `\auditOnlyLicense`, `\auditTopOnePctShare`...) e uma macro por tabela
  (`\auditTableDiscovery`, `\auditTableSelection`, `\auditTableArtifacts`, com os rótulos
  `tab:audit-*`). Não edite esse arquivo: rode a auditoria de novo;
- `audit_rascunho.tex`: rascunho dos parágrafos de descoberta, seleção, mineração e ameaças à
  validade, escrito com as macros. As primeiras linhas listam, como comentários, os avisos da
  auditoria. Copie o texto para o artigo e edite lá.

No artigo, carregue as macros uma vez no preâmbulo e use as tabelas onde quiser:

```latex
\input{audit}            % preâmbulo
...
\auditTableSelection     % no texto
Dos \auditEvaluated{} repositórios avaliados, \auditKept{} foram mantidos.
```

Depois de uma nova coleta (`agm export && agm audit`), basta trocar o `audit.tex` no Overleaf
para todos os números do texto e das tabelas se atualizarem. O comentário no topo do arquivo
registra o export, o `code_version` e o hash do `study.yaml` de onde os números vieram.

## Figuras do artigo (LaTeX)

`agm figures` gera as figuras do artigo em LaTeX puro (TikZ e pgfplots), porque a sincronização
com o Overleaf só leva arquivos de texto. Ele lê apenas saídas de outros comandos (sem banco) e
grava `DATA_DIR/figures/<gold>/figuras.tex`, com uma macro por figura, cada uma com o ambiente
`figure` completo, legenda e rótulo:

- `\figPipeline` (`fig:pipeline`): fluxograma das etapas, com os repositórios descobertos,
  avaliados, selecionados e minerados (do `audit.json` do mesmo export) e os arquivos e
  repositórios analisados (do `rq2_structure`, com os filtros do `agm rq2 summary`);
- `\figElementos` (`fig:elementos`): porcentagem dos `CLAUDE.md` e `AGENTS.md` com tabelas,
  blocos de código, listas numeradas, listas com marcadores e links, na nossa amostra e nos
  arquivos do *Agent READMEs* (o `rq2_structure` do `--compare`, padrão
  `DATA_DIR/external/agentreadmes`);
- `\figPrevalencia` (`fig:prevalencia`): mapa de calor com a porcentagem de arquivos com cada
  categoria da QP1, nas colunas Todos, Raiz (o arquivo da raiz de cada repositório, escolhido
  por `one_per_repository`) e AR (os arquivos rotulados do *Agent READMEs*, classificados pelo
  mesmo modelo). Usa o `artifact_labels.parquet` do `classify` da nossa amostra e o
  `section_labels.parquet` do `classify` do *Agent READMEs*.

As pastas do `classify` saem do `--model` e do `--codebook` (como nos comandos da RQ1), ou de
`--labels` e `--compare-labels`:

```bash
poetry run agm figures --model anthropic:claude-sonnet-5-5 \
  --codebook codebooks/seed_agent_readmes.yaml
```

Cada porcentagem é arredondada uma única vez, a partir das contagens exatas (2031/7977 =
25,46% vira 25, e não 26 como no arredondamento de 0,255). Os números seguem o formato
brasileiro (`5.037`). As figuras usam só tons de cinza, como pede a SBC, e cabem nos 15 cm de
texto do template. O comentário no topo do arquivo registra as entradas e diz o que o preâmbulo
precisa:

```latex
\usepackage{pgfplots}       % carrega o TikZ
\pgfplotsset{compat=1.18}
\input{figuras}
...
\figPipeline                % no texto, onde a figura deve ficar
```
