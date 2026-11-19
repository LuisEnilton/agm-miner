# Referência do CLI

Tudo é feito pelo comando `agm` (`poetry run agm ...` ou `docker compose run --rm agm ...`).
`agm help` lista os comandos agrupados nos mesmos painéis desta página, e
`agm <comando> --help` mostra as opções de cada um.

Convenções de todos os comandos:

- `--study arquivo.yaml` usa outra configuração do estudo (padrão: `STUDY_CONFIG_PATH`);
- códigos de saída: `0` sucesso; `1` erro esperado, com mensagem de uma linha (token inválido,
  repositório desconhecido, estágio já em execução, `DATABASE_URL` ausente); `2` opção inválida;
  `130` interrompido (o trabalho concluído fica salvo; rode o mesmo comando para continuar);
- o resultado sai em JSON na saída padrão; o progresso e os logs saem na saída de erro.

## Coleta (GitHub + Postgres)

| Comando | O que faz | Detalhes |
|---|---|---|
| `agm discover` | Estágio 1: busca os artefatos na Code Search do GitHub, com bisseção por tamanho | [pipeline.md](pipeline.md#etapas) |
| `agm select` | Estágio 2: lê os metadados (GraphQL) e aplica os critérios de seleção | [configuracao.md](configuracao.md#critérios-de-amostragem) |
| `agm add-repo` | Registra repositórios pelo nome (adições manuais) e avalia os critérios | [pipeline.md](pipeline.md#opções-úteis) |
| `agm mine` | Estágio 3: minera o histórico dos artefatos a partir de clones parciais | [pipeline.md](pipeline.md#posso-parar-a-qualquer-momento) |
| `agm run` | Roda os estágios 1 a 3 em paralelo até esvaziar a fila, depois 4 e 5 | [pipeline.md](pipeline.md#coleta-automática-com-agm-run) |

## Exportação e análise

| Comando | O que faz | Detalhes |
|---|---|---|
| `agm export` | Estágio 4: grava todas as tabelas do banco em Parquet (bronze), com manifesto | [dados.md](dados.md#bronze) |
| `agm analyze` | Estágio 5: gera as tabelas silver e gold a partir de um export, sem banco | [dados.md](dados.md#gold) |

## Resultados do estudo (sem banco)

| Comando | O que faz | Detalhes |
|---|---|---|
| `agm audit` | Auditoria da amostra (cobertura da busca, efeito dos critérios, falhas), com LaTeX | [auditoria-e-figuras.md](auditoria-e-figuras.md) |
| `agm figures` | Figuras do artigo em LaTeX (TikZ/pgfplots) | [auditoria-e-figuras.md](auditoria-e-figuras.md#figuras-do-artigo-latex) |
| `agm rq1 classify` | Categorias de cada seção com o LLM (cache, retomável; `--offline` reproduz do cache) | [rq1.md](rq1.md#classificação) |
| `agm rq1 agentreadmes import` | Importa o dataset do *Agent READMEs* no formato da saída do `analyze` | [rq1.md](rq1.md#validação-externa-com-o-dataset-do-agent-readmes) |
| `agm rq1 agentreadmes score` | Concordância com os rótulos humanos do *Agent READMEs*, com o GPT-5 como linha de base | [rq1.md](rq1.md#validação-externa-com-o-dataset-do-agent-readmes) |
| `agm rq2 summary` | Tabelas de estrutura da RQ2, opcionalmente ao lado de outro dataset | [rq2.md](rq2.md) |

## Operação

| Comando | O que faz | Detalhes |
|---|---|---|
| `agm status` | O que está rodando agora e em que ponto (lê o progresso salvo no banco) | [pipeline.md](pipeline.md#acompanhando-o-progresso) |
| `agm help` | Lista todos os comandos por painel | — |
| `agm runs list` | Execuções registradas, com estatísticas | [pipeline.md](pipeline.md#posso-rodar-em-dois-terminais) |
| `agm runs cleanup` | Registra como `INTERRUPTED` as execuções sem sinal de vida | [pipeline.md](pipeline.md#posso-parar-a-qualquer-momento) |
| `agm runs funnel` | Repositórios por status e motivo de rejeição (funil de seleção) | [configuracao.md](configuracao.md#critérios-de-amostragem) |

## Experimental / piloto (não usados nos resultados finais)

| Comando | O que faz | Detalhes |
|---|---|---|
| `agm rq1 sample` | Amostra estratificada de seções para criar os rótulos | [rq1.md](rq1.md#criação-dos-labels) |
| `agm rq1 propose` | Rótulos livres para cada seção da amostra | [rq1.md](rq1.md#criação-dos-labels) |
| `agm rq1 consolidate` | Rascunho de codebook a partir dos rótulos propostos | [rq1.md](rq1.md#criação-dos-labels) |
| `agm rq1 models` | Modelos disponíveis no Gemini ou no OpenRouter | [rq1.md](rq1.md#provedores-testar-de-graça-coletar-com-o-claude) |
| `agm rq1 smoke` | Uma requisição de teste: confere chave, modelo e schema | [rq1.md](rq1.md#provedores-testar-de-graça-coletar-com-o-claude) |
| `agm rq1 saturation` | Quantas seções de uma amostra nova não cabem em nenhuma categoria | [rq1.md](rq1.md#saturação-e-validação-com-rótulos-próprios) |
| `agm rq1 compare` | Compara dois classificadores | [rq1.md](rq1.md#comparar-dois-modelos) |
| `agm rq1 validate export` | Planilha cega para rotular à mão | [rq1.md](rq1.md#saturação-e-validação-com-rótulos-próprios) |
| `agm rq1 validate llm` | Rótulos do LLM na amostra de validação | [rq1.md](rq1.md#saturação-e-validação-com-rótulos-próprios) |
| `agm rq1 validate score` | Concordância humano × LLM, LLM × LLM e humano × humano | [rq1.md](rq1.md#saturação-e-validação-com-rótulos-próprios) |
