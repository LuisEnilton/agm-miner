# Limitações (ameaças à validade)

O que a ferramenta não consegue medir, ou mede com viés, e como cada ponto aparece nos dados. Os
números de cada limitação na coleta final estão na auditoria (`agm audit`,
[auditoria-e-figuras.md](auditoria-e-figuras.md)).

## Coleta

- A Code Search do GitHub indexa só o branch default e arquivos menores que 384 KB, e os seus
  resultados não são determinísticos: repetir a busca não devolve exatamente o mesmo conjunto.
- A busca devolve no máximo 1.000 resultados por query. A bisseção por tamanho contorna o limite,
  mas há mais de 1.000 `AGENTS.md` numa faixa de 20 bytes, então até fatias de 1 byte podem
  estourar. `unreachable_matches`, nas estatísticas da execução e na auditoria, mede quantos
  resultados ficaram inacessíveis.
- A REST devolve `node_id` legado (`MDEw...`) para repositórios antigos, e o GraphQL responde com o
  id novo; `fetch_metadata` casa as respostas por posição e grava o id canônico. O qualificador
  `stars:` não funciona na Code Search, por isso o filtro de estrelas fica no `select`.
- Os metadados do GitHub (estrelas, licença, linguagem) são do momento da seleção, não da data de
  referência.
- A linguagem principal vem do GitHub Linguist, que não conta Markdown. Um repositório de
  documentação com um script pequeno ainda passa no critério de linguagem.

## Amostra

- O corte de criação (`created_before: 2026-08-01`) é mais tardio que o dos artigos relacionados
  (2024-01-01), então a amostra mistura projetos anteriores às ferramentas de agentes e projetos
  criados já com elas. Para comparar com esses artigos, separe os dois grupos por `gh_created_at`
  em `repo_features` ou repita a seleção com `created_before: 2024-01-01`.
- As heurísticas de tutorial/lista (`exclude_keywords`), de bots, de agentes e dos papéis de autor
  são padrões de texto e precisam de validação manual numa amostra.
- `reference_date` fica fixa durante a coleta, mas a mineração leva semanas. Com
  `analysis.truncate_at_reference_date`, o `analyze` corta o histórico no fim dessa data
  ([configuracao.md](configuracao.md#corte-do-histórico-na-data-de-referência)). Não dá para
  cortar `identities.commits` nem `contributor_activity` (agregados do clone inteiro), usados nos
  papéis de autor.

## Métricas

- `hours_since_previous_change` usa a data de autoria, que um rebase pode distorcer.
- A legibilidade (Flesch) só é calculada para textos detectados como inglês, e só sobre parágrafos
  e itens de lista (títulos, tabelas e código ficam de fora). Cada bloco conta como uma frase,
  mesmo sem ponto final, porque os arquivos de configuração são escritos quase sempre em listas;
  sem isso, um arquivo só de bullets virava uma frase única. Blocos de 1 ou 2 palavras (rótulos,
  links soltos) ficam de fora. Blocos feitos só de caminhos ou URLs ainda distorcem o índice.
- Itens de lista compactos não contam como parágrafos, URLs soltas contam como links e o BOM UTF-8
  é ignorado antes do frontmatter.

## RQ1 (classificação com LLM)

- O classificador rotula seções, e um arquivo recebe todas as categorias das suas seções. Essa
  união tende a marcar mais categorias do que um humano lendo o arquivo inteiro (mais revocação,
  menos precisão), e a prevalência por arquivo cresce com o número de seções.
- Os pedidos que o modelo recusou ou respondeu fora do formato ficam sem rótulo (`failed` no
  `classify.json`; 31 na coleta final, cobertura de 99,8%).
- As APIs de LLM não permitem fixar a amostragem: classificar de novo pode dar outro resultado. A
  reprodução exata usa as respostas guardadas (`agm rq1 classify --offline`).
