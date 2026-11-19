# RQ1: categorias de conteúdo

A RQ1 classifica cada seção dos artefatos da amostra (tabela `rq1_sections`, saída do
`agm analyze`) nas 16 categorias de conteúdo do *Agent READMEs* (Chatlatanagulchai et al., 2025),
com um LLM, e valida o classificador contra os rótulos humanos desse artigo.

## O caminho dos resultados finais

| Item | Valor |
|---|---|
| Codebook | [`codebooks/seed_agent_readmes.yaml`](../codebooks/seed_agent_readmes.yaml): as 16 categorias da Tabela 3 do *Agent READMEs* (`agent_readmes@2025`) |
| Modelo | Claude Sonnet 5.5 (`claude-sonnet-5-5`), effort `low`, pela Message Batches API (`rq1.provider: anthropic`) |
| Prompt | `classify-v1`, até `rq1.chunk_sections` (30) seções de um mesmo arquivo por requisição |
| Artefatos | arquivos de instruções e `SKILL.md` (`--kind INSTRUCTION --kind SKILL_MANIFEST`); os recursos das Skills ficam de fora |
| Validação | externa, com os 332 arquivos rotulados à mão pelos autores do *Agent READMEs*, com o GPT-5 do artigo como linha de base |

Os valores ficam na seção `rq1:` do [study.yaml](../study.yaml), então os comandos abaixo, sem
`--model`, reproduzem a coleta final:

```bash
# Classificação de todas as seções da amostra
poetry run agm rq1 classify --kind INSTRUCTION --kind SKILL_MANIFEST

# Validação externa (detalhes na seção abaixo)
poetry run agm rq1 agentreadmes import data/sources/agentreadmes/train-00000-of-00001.parquet \
  --human-labels data/sources/agentreadmes/final_am_classification_3.csv \
  --gpt5-labels data/sources/agentreadmes/gpt_5_file_classifications.csv
poetry run agm rq1 classify --gold data/external/agentreadmes \
  --codebook codebooks/seed_agent_readmes.yaml --only-labelled
poetry run agm rq1 agentreadmes score
```

Com as respostas publicadas no cache, `--offline` refaz a classificação sem chave de API e sem
enviar nada: as tabelas saem idênticas às do artigo, inclusive os 31 pedidos que ficaram sem
resposta (recusas e respostas fora do formato). Sem `--offline`, esses pedidos seriam enviados de
novo. Veja [replicacao.md](replicacao.md).

O codebook não foi criado com os comandos de criação de labels do piloto (seção
"[Piloto e alternativas](#piloto-e-alternativas-não-usados-nos-resultados-finais)"): a coleta final
usou a taxonomia publicada do *Agent READMEs*, para que os resultados fossem comparáveis com os
desse artigo.

## Como os comandos da RQ1 funcionam

Os comandos ficam em `agm rq1 ...` e usam a seção `rq1:` do `study.yaml`. Só um comando por vez
trabalha em cada diretório da RQ1: um segundo é recusado, para não enviar os mesmos pedidos (e
pagar) duas vezes. A amostra depende só do conteúdo, da semente e da configuração, então é a mesma
em qualquer export dos mesmos dados. Assim como a análise, eles
não usam o banco: leem a saída do `analyze` e gravam em `DATA_DIR/rq1/<nome da saída>/`, com um
manifesto ao lado de cada resultado. As respostas do LLM ficam em cache em disco, e os lotes da
Batches API ficam registrados, então um comando interrompido continua de onde parou sem pagar de
novo pelo que já foi respondido.

## Classificação

`agm rq1 classify` classifica todas as seções com texto contra o codebook de `rq1.codebook`
(`--codebook` para outro). Seções idênticas são classificadas uma vez só. Cada requisição leva até
`rq1.chunk_sections` seções de um mesmo arquivo; o `system` traz as instruções e o codebook
inteiro (igual em todas as requisições) e avisa que o conteúdo das seções é dado a classificar, não
instrução a seguir. Cada seção recebe uma ou mais categorias, ou `other`.

- `--kind` (repetível) restringe a classificação a tipos de artefato: `--kind INSTRUCTION --kind
  SKILL_MANIFEST` deixa de fora os arquivos de recurso das Skills, que são documentação de apoio e
  não instruções ao agente. Rodar depois sem `--kind` reaproveita do cache tudo o que já foi
  classificado.
- `--limit N` faz um piloto com N requisições, para conferir o custo.
- `--offline` responde só do cache (sem chave e sem requisições) e não altera o cache nem o
  registro de falhas: reproduz uma classificação publicada.
- `--model provedor:modelo` troca o modelo só naquela execução (seção "Comparar dois modelos").

Respostas que não seguem o formato pedido (seção faltando, categoria desconhecida, JSON inválido)
não ficam no cache e são pedidas de novo na próxima execução. Depois de `rq1.max_attempts` falhas,
o pedido é abandonado e aparece em `given_up` no manifesto. Os prompts têm versão: mudar o texto de
um prompt exige mudar a versão, que entra na chave do cache.

Saídas em `DATA_DIR/rq1/<gold>/classify/<codebook>/<provedor>/<modelo>/`:

- `section_labels.parquet`: uma linha por seção e categoria;
- `artifact_labels.parquet`: por arquivo e categoria, seções, palavras e fração das palavras;
- `repo_labels.parquet`: o mesmo por repositório, para juntar ao `repo_features` pelo `repo_id` na
  análise de correlação;
- `label_prevalence.parquet`: fração de arquivos e de seções com cada categoria, por ferramenta e
  tipo de arquivo;
- `classify.json`: o manifesto (codebook com hash, modelo, prompt, cobertura, falhas, tokens).

## Validação externa com o dataset do *Agent READMEs*

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

**Sensibilidade ao codebook.** O `seed_agent_readmes.yaml` tem só as definições de uma linha da
Tabela 3 do artigo. O `codebooks/agent_readmes_enriched.yaml` mantém as mesmas categorias e
definições e acrescenta, como critério de inclusão, só textos dos próprios autores: a definição
operacional do código deles (`labels.py`) e a descrição de cada categoria no artigo, com o exemplo
que ela traz. Rodar os dois mostra quanto do erro vem das definições. As saídas ficam em pastas
separadas (`agent_readmes_2025` e `agent_readmes_2025-enriched`).

```bash
poetry run agm rq1 classify --gold data/external/agentreadmes \
  --codebook codebooks/agent_readmes_enriched.yaml --only-labelled --model anthropic:claude-sonnet-5-5
poetry run agm rq1 agentreadmes score --codebook codebooks/agent_readmes_enriched.yaml \
  --model anthropic:claude-sonnet-5-5
```

**Por seção × arquivo inteiro.** O artigo (e o GPT-5) rotula cada arquivo de uma vez; o nosso
classificador rotula cada seção e une as categorias por arquivo. `--unit file` classifica o
arquivo inteiro numa única requisição, com o mesmo codebook, modelo, effort e proteção contra
injeção. Assim, cada comparação muda um fator só: Claude por arquivo × GPT-5 (o modelo) e Claude
por arquivo × Claude por seção (a unidade).

```bash
poetry run agm rq1 classify --gold data/external/agentreadmes --unit file \
  --codebook codebooks/seed_agent_readmes.yaml --only-labelled --model anthropic:claude-sonnet-5-5
poetry run agm rq1 agentreadmes score --unit file --model anthropic:claude-sonnet-5-5
```

O resultado fica em `classify-file/<codebook>/<provedor>/<modelo>/file_labels.parquet` (uma
linha por arquivo, com a lista de categorias) e o score em `scores-file.*`. Esse score traz também
`section_vs_file`, a concordância entre as duas unidades do mesmo modelo, e
`mean_categories`, a média de categorias por arquivo de cada rotulador. Arquivos idênticos geram
uma requisição só. Precisa do `file_contents.parquet` da importação: importe de novo com
`--force` um dataset importado antes dessa versão.

- **O que a importação grava** (`data/external/agentreadmes/`): `rq1_sections.parquet`, com os
  arquivos quebrados em seções pelo mesmo parser do `analyze`; `rq2_structure.parquet`, com as
  métricas da RQ2 de cada arquivo, nas mesmas colunas e tipos da tabela gold do `analyze` (cada
  arquivo é um artefato cuja única versão é o seu conteúdo, `head_blob_sha` é o id de blob do
  git e o `--study` define a configuração de análise); `files.parquet`, com URL e
  metadados de cada arquivo; `file_contents.parquet`, com o conteúdo de cada arquivo (para
  `--unit file`); `reference_labels.parquet`, com os rótulos humanos e do GPT-5 já nos
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
- **RQ2 no dataset do artigo:** o `rq2_structure.parquet` da importação permite comparar a
  estrutura dos nossos arquivos com a dos arquivos do artigo usando o mesmo código. Nada é
  filtrado: para usar o critério de tamanho da nossa amostra, filtre `size_bytes >=
  analysis.min_artifact_bytes`. O critério de autores humanos não se aplica, porque o dataset
  não traz o histórico; pelo mesmo motivo não há RQ3 nele, nem Skills.
- Saídas em `DATA_DIR/rq1/agentreadmes/agentreadmes/<codebook>/<provedor>/<modelo>/`:
  `scores.json`, `scores.md` e o manifesto (ou `scores-AGENTS-CLAUDE.*` com `--tool`).
- O dataset e o pacote de replicação não declaram licença: cite o artigo e não redistribua os
  arquivos.

## Piloto e alternativas (não usados nos resultados finais)

Antes da coleta final, o pipeline foi testado com modelos gratuitos e com um codebook induzido a
partir de uma amostra. Esses comandos aparecem no painel "Experimental / pilot" do `agm --help`.
Os resultados do piloto ficam fora do artigo; eles ajudam a repetir o teste ou a estender o
estudo com outro codebook ou outro modelo.

### Provedores: testar de graça, coletar com o Claude

O provedor dos modelos é escolhido em `rq1.provider` no `study.yaml`:

| | `gemini` (piloto) | `openrouter` (alternativa para testes) | `anthropic` (coleta final) |
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

O Gemini precisa do extra opcional: `poetry install -E gemini`. As respostas ficam separadas por
provedor e modelo no cache e nas pastas de saída, então os testes nunca se misturam com a coleta
final.

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
   caíram em `other`) e salve a versão final em `codebooks/rq1.yaml`, com um `version` próprio. O
   `codebooks/rq1.yaml` do repositório é o rascunho do piloto; a coleta final classificou com o
   `codebooks/seed_agent_readmes.yaml`.

Respostas que não seguem o formato pedido (seção faltando, sem rótulo, JSON inválido) não ficam no
cache e são pedidas de novo na próxima execução. Depois de `rq1.max_attempts` falhas, o pedido é
abandonado e aparece em `given_up` no manifesto, para não consumir a cota todo dia. Os prompts têm
versão (`propose-v2`, `induce-v1`, `align-v1`, `map-v1`): mudar o texto de um prompt exige mudar a versão.

### Saturação e validação com rótulos próprios

- **`agm rq1 saturation`** classifica `rq1.saturation_size` seções que não estavam na amostra de
   criação dos labels e mede quantas caem em `other` e quais categorias não aparecem.
- **Validação com uma planilha rotulada à mão:**
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
