# Histórico de versões

O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/), e as versões
seguem o [Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [1.0.1] — versão do artigo

- `agm package` cita no README do pacote a versão da ferramenta que o reproduz (`--tool-version`,
  `--tool-doi`), com o título atual do estudo, e explica as versões de código registradas antes
  da publicação.
- DOI do Zenodo no README e no `CITATION.cff`.

## [1.0.0] — 2026-11-20

Primeira versão pública: a ferramenta que produziu os resultados do artigo *O que os
desenvolvedores dizem aos seus agentes de código?*

- Coleta: `discover` (Code Search com bisseção por tamanho), `select` (metadados pelo GraphQL e
  critérios do `study.yaml`), `mine` (clone parcial e histórico dos artefatos) e `run`, que roda
  os estágios em paralelo. Todos podem ser interrompidos e retomados.
- Exportação e análise: `export` (Parquet, com manifesto) e `analyze` (tabelas silver e gold das
  RQs, sem banco).
- Resultados: `audit` (vieses da amostra), `rq1` (classificação das seções com LLM, validação
  com o *Agent READMEs* e `--offline` para reproduzir do cache), `rq2 summary` e `figures`
  (figuras do artigo em LaTeX).
- `rq1 classify`: a ordem das linhas de `label_prevalence` é fixa nos empates, então uma
  reprodução gera o mesmo arquivo byte a byte.
- Replicação: `package` (pacote para o Zenodo, com os dados pessoais pseudonimizados), imagem
  Docker, dicionário de dados e documentação em `docs/`.
