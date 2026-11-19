# Replicação

Os resultados do estudo podem ser reproduzidos em três níveis, do mais rápido ao mais completo.
Cada nível parte de um ponto diferente do pipeline:

| Nível | Parte de | O que roda | Precisa de | Tempo |
|---|---|---|---|---|
| **L1** | saídas publicadas (bronze, gold, cache da RQ1) | RQ1 a partir do cache, RQ2, auditoria e figuras | Python ou Docker; sem rede, banco ou chave | minutos |
| **L2** | export bronze | `agm analyze` e depois o L1 | cerca de 5 GB de RAM | cerca de 1 hora |
| **L3** | GitHub | coleta inteira com a mesma versão da ferramenta | token do GitHub, Postgres, chave da Anthropic | semanas |

L1 e L2 devem reproduzir os números do artigo exatamente. O L3 não: a Code Search não é
determinística, repositórios mudam ou são apagados e o LLM não permite fixar a amostragem
([limitacoes.md](limitacoes.md)).

## Onde ficam os dados

Os comandos leem e gravam em `DATA_DIR` (padrão `data/`). A coleta final usa o export
`20261117T023135Z`, e cada pasta guarda um manifesto com a versão do código e a configuração usadas:

```
DATA_DIR/
  bronze/20261117T023135Z/     # export do banco (manifest.json)
  gold/20261117T023135Z/       # saída do analyze (analysis_manifest.json)
  rq1/20261117T023135Z/        # cache das respostas do LLM, classify/, failures.json
  rq1/agentreadmes/            # o mesmo para a validação externa
  external/agentreadmes/       # dataset do Agent READMEs importado (gerado pelo import)
  audit/20261117T023135Z/      # auditoria (audit.json, audit.md, audit.tex)
  rq2/20261117T023135Z__vs__agentreadmes/   # tabelas da RQ2
  figures/20261117T023135Z/    # figuras do artigo (figuras.tex)
```

A descrição de cada tabela e coluna está em [dados.md](dados.md).

## O pacote de replicação

`agm package` reúne num pacote para o Zenodo tudo o que um export precisa para ser reproduzido,
lendo só arquivos (sem banco):

```bash
# PACKAGE_SALT no .env: chave secreta de 16 caracteres ou mais (guarde-a; nunca a publique)
poetry run agm package          # o export mais recente; --bronze DIR escolhe outro
```

A saída fica em `DATA_DIR/package/<export>/` (gravada em `.partial` e renomeada no fim):

| Arquivo | Conteúdo |
|---|---|
| `dados/bronze-<export>.zip`, `dados/gold-<export>.zip` | O export e a análise, com os dados pessoais pseudonimizados |
| `rq1/rq1-<export>.zip`, `rq1/rq1-agentreadmes.zip` | Cache das respostas do LLM, rótulos e manifestos da RQ1 (sem lotes pendentes nem locks) |
| `resultados/` | Auditoria, RQ2 e figuras, em zip e os arquivos principais soltos |
| `config/` | O `study.yaml`, os codebooks e as fontes fixadas do *Agent READMEs* (URL e SHA-256) |
| `README.md`, `manifest.json` | Proveniência (export, versões do código, amostra) e como reproduzir |
| `DICIONARIO_DE_DADOS.md` | O mesmo de [dados.md](dados.md) |
| `licencas.csv`, `LICENSE-DATA.md` | Licença de cada repositório publicado; CC BY 4.0 para o resto |
| `reproduzir.sh`, `SHA256SUMS` | O script dos níveis L1 e L2; o hash de cada arquivo |

Cada zip reproduz a estrutura do `DATA_DIR`: extraídos na mesma pasta, formam um `DATA_DIR` que
os comandos leem diretamente. O `reproduzir.sh` faz isso, guarda os resultados publicados em
`data/publicado/` e os refaz com os comandos abaixo.

**Dados pessoais.** Nomes, e-mails, logins do GitHub e chaves de identidade de pessoas viram
pseudônimos estáveis (HMAC-SHA256 com o `PACKAGE_SALT`): a mesma pessoa tem o mesmo pseudônimo em
todas as tabelas, e as junções continuam valendo. Bots e agentes de código mantêm o que os
identifica (o nome, como `dependabot[bot]`, ou o e-mail, como `noreply@anthropic.com`); a outra
parte é trocada, porque há pessoas que fazem commits com o nome de um bot ou o e-mail de um agente.
Como o padrão que reconheceu cada bot continua valendo, `agm analyze` sobre o bronze do pacote
reproduz exatamente o gold do pacote, com os papéis de autor (`test_package.py` verifica). Nas mensagens de commit, os
pares "Nome &lt;e-mail&gt;" (trailers como `Signed-off-by`) e os e-mails soltos também são trocados;
um nome escrito em outro ponto do texto não tem como ser reconhecido e fica. O conteúdo dos
arquivos minerados é publicado como está: são arquivos públicos, sob licenças OSI.

**O que não entra.** Os arquivos do *Agent READMEs* (o dataset não declara licença): o
`reproduzir.sh` os baixa das revisões fixadas e confere o SHA-256. O piloto (`data/pilot`) também
fica de fora.

## L1: dos dados publicados aos resultados

Com Poetry (`poetry run agm ...`) ou Docker (`docker compose run --rm agm ...`, com os dados em
`./data`). Para não sobrescrever os resultados publicados, aponte as saídas para outra pasta ou
trabalhe numa cópia de `DATA_DIR`.

```bash
export DATA_DIR=data

# RQ1: categorias de cada seção, das respostas guardadas (sem chave e sem requisições)
agm rq1 classify --offline --kind INSTRUCTION --kind SKILL_MANIFEST

# RQ2: tabelas de estrutura, ao lado dos arquivos do Agent READMEs
agm rq2 summary --compare $DATA_DIR/external/agentreadmes

# Auditoria da amostra e figuras do artigo
agm audit
agm figures
```

A validação externa precisa dos arquivos do *Agent READMEs*, que não são redistribuídos (o dataset
não declara licença). Baixe as fontes nas revisões fixadas e importe-as
([rq1.md](rq1.md#validação-externa-com-o-dataset-do-agent-readmes)). Os ids da importação vêm do
conteúdo, então as respostas guardadas continuam valendo:

```bash
agm rq1 agentreadmes import data/sources/agentreadmes/train-00000-of-00001.parquet \
  --human-labels data/sources/agentreadmes/final_am_classification_3.csv \
  --gpt5-labels data/sources/agentreadmes/gpt_5_file_classifications.csv
agm rq1 classify --offline --gold $DATA_DIR/external/agentreadmes \
  --codebook codebooks/seed_agent_readmes.yaml --only-labelled
agm rq1 agentreadmes score
```

`--offline` é o que torna o L1 exato: os pedidos que ficaram sem resposta na coleta final (31)
continuam sem resposta, em vez de serem enviados de novo.

## L2: do export bronze à análise

O `analyze` lê só o export, sem banco. Ele usa alguns GB de memória; com um teto, um erro de
memória derruba só o comando:

```bash
export DATA_DIR=/tmp/agm-l2                    # pasta nova, para não misturar com o publicado
mkdir -p $DATA_DIR/bronze $DATA_DIR/rq1
cp -r data/bronze/20261117T023135Z $DATA_DIR/bronze/
cp -r data/rq1/20261117T023135Z $DATA_DIR/rq1/  # o cache da RQ1
systemd-run --user --scope -p MemoryMax=5G poetry run agm analyze
```

A saída vai para `$DATA_DIR/gold/20261117T023135Z/`, com o mesmo nome do export, e por isso a RQ1
encontra o cache na pasta copiada. Depois, rode os comandos do L1 com esse `DATA_DIR`.

## L3: coletar de novo

Use a mesma versão da ferramenta (a tag citada no artigo) e o `study.yaml` dela, e siga
[instalacao.md](instalacao.md) e [pipeline.md](pipeline.md):

```bash
poetry run agm run --workers 4                 # discover, select e mine; depois export e analyze
poetry run agm audit
poetry run agm rq1 classify --kind INSTRUCTION --kind SKILL_MANIFEST   # envia ao Claude (pago)
```

Para comparar com a coleta original, mantenha `selection.reference_date` e
`analysis.truncate_at_reference_date` do `study.yaml`: o histórico é cortado na mesma data. A
auditoria mostra a cobertura da busca e o efeito de cada critério, e permite ver onde a nova
amostra difere.

## Conferir uma reprodução

As tabelas são comparadas por conteúdo. Para a RQ1, por exemplo:

```python
import polars as pl

published = "data/rq1/20261117T023135Z/classify/agent_readmes_2025/anthropic/claude-sonnet-5-5"
reproduced = (
    "/tmp/agm-l2/rq1/20261117T023135Z/classify/agent_readmes_2025/anthropic/claude-sonnet-5-5"
)
for table in ["section_labels", "artifact_labels", "repo_labels", "label_prevalence"]:
    a = pl.read_parquet(f"{published}/{table}.parquet")
    b = pl.read_parquet(f"{reproduced}/{table}.parquet")
    print(table, a.equals(b))
```

Os arquivos LaTeX (`audit.tex`, `figuras.tex`) têm um cabeçalho com caminhos e a versão do código;
fora dele, devem ser idênticos.
