# RQ2: estrutura dos artefatos

`agm rq2 summary` lê só o `rq2_structure.parquet` de uma saída do `analyze` e grava
`DATA_DIR/rq2/<gold>/rq2_summary.json` e `.md`. Os dados são agrupados por tipo de arquivo
(`CLAUDE.md`, `AGENTS.md`, `SKILL.md`):

- extensão (linhas, palavras, caracteres) em média e quartis, com faixas de palavras;
- títulos, seções e profundidade máxima;
- porcentagem de arquivos com cada elemento (bloco de código, tabela, listas, links...);
- Flesch, só para os arquivos em inglês;
- frontmatter das Skills: quantos são YAML válidos e a frequência das chaves.

`--compare` resume outra saída com os mesmos filtros, lado a lado. Por exemplo, os arquivos do
artigo *Agent READMEs*, onde o `COPILOT` fica de fora e o limite de tamanho é o mesmo
(`--min-bytes`, padrão `analysis.min_artifact_bytes`):

```bash
poetry run agm rq2 summary --compare data/external/agentreadmes
```

Os dois datasets não têm a mesma população, então cada número sai em quatro recortes:

- **todos os arquivos ou só os da raiz**: o artigo só tem arquivos da raiz, e boa parte dos
  nossos é aninhada;
- **por arquivo ou um arquivo por repositório**: no segundo, cada repositório conta com o arquivo
  da raiz ou, sem ele, o de caminho mais raso, para um monorepo com dezenas de arquivos não pesar
  mais.

A tabela "por faixa de tamanho" mostra se uma diferença (por exemplo, menos blocos de código)
vem de arquivos menores ou de outro jeito de escrever. `code_blocks` conta blocos cercados e
indentados.
