# AGMDataMiner

AGMDataMiner é uma ferramenta para buscar, extrair e analisar dados de repositórios do GitHub usando Perceval e OpenSearch.

## 📋 Pré-requisitos

- Python 3.8+
- Docker e Docker Compose
- Git
- Perceval instalado
- Token de acesso do GitHub

## 🔧 Configuração Inicial

### 1. Configurar o Token do GitHub

Crie um arquivo `.env` na raiz do projeto:

```bash
cp .env.sample .env
```

Abra o `.env` e adicione seu token do GitHub:

```
GITHUB_TOKEN=seu_token_aqui_ghp_xxxxxxxxxxxx
```

Para gerar um token:
1. Acesse https://github.com/settings/tokens
2. Clique em "Generate new token"
3. Selecione os escopos `repo`, `read:org` e `gist`
4. Copie o token e adicione ao arquivo `.env`

### 2. Exportar a Variável de Ambiente

```bash
export GITHUB_TOKEN=$(grep GITHUB_TOKEN .env | cut -d '=' -f 2)
```

## 🚀 Como Executar a Aplicação

### Passo 1: Buscar Repositórios no GitHub

Execute o script `fetch_github.py` para buscar repositórios que atendem aos critérios de busca:

```bash
python fetch_github.py
```

**O que este script faz:**
- Busca repositórios do GitHub que contêm os arquivos especificados (claude.md, gemini.md)
- Filtra os repositórios por número mínimo de estrelas (padrão: 10)
- Salva os repositórios em `repositorios.json`

**Resultado esperado:**
```json
{
  "timestamp": "2026-06-19T12:30:45.123456",
  "total_repositories": 42,
  "repositories": [
    "owner/repository",
    "another_owner/another_repo",
    ...
  ]
}
```

### Passo 2: Extrair Dados dos Repositórios

Execute o script `ingestor.py` para extrair dados e ingeri-los no OpenSearch:

```bash
python ingestor.py
```

**O que este script faz:**
- Lê a lista de repositórios do `repositorios.json`
- Clona e extrai dados de cada repositório usando o Perceval
- Enriquece os dados com informações adicionais
- Ingere os dados no OpenSearch (raw + enriched indexes)

**Nota:** O script processa uma amostra de repositórios por padrão. Para processar todos, modifique o parâmetro `sample_size` na função.

### Passo 3: Iniciar os Serviços

Inicie o OpenSearch e OpenSearch Dashboards com Docker Compose:

```bash
docker-compose up -d
```

**Serviços iniciados:**
- **OpenSearch**: http://localhost:9200 (banco de dados)
- **OpenSearch Dashboards**: http://localhost:5601 (interface visual)

Para verificar se os serviços estão rodando:

```bash
docker-compose ps
```

### Passo 4: Acessar a Aplicação

Abra o navegador e acesse:

```
http://localhost:5601
```

Aqui você pode:
- Visualizar e explorar os índices de dados
- Criar gráficos e dashboards
- Realizar buscas complexas nos dados dos repositórios

## 📊 Perceval: Git vs GitHub Backend

O Perceval é uma ferramenta que extrai dados de diferentes fontes de controle de versão. Existem duas formas principais de extração:

### Backend Git

```bash
perceval git https://github.com/owner/repo.git
```

**Características:**
- ✅ Extrai histórico completo de **commits**
- ✅ Acessa repositório via protocolo Git (HTTP ou SSH)
- ✅ **NÃO requer token de autenticação** (repositórios públicos)
- ✅ Mais rápido para repositórios pequenos
- ✅ Extrai metadados: autor, data, hash, mensagem do commit
- ❌ Acesso limitado a commits (não obtém issues, PRs, etc)

**Exemplo de uso:**
```python
comando = ["perceval", "git", "https://github.com/owner/repo.git"]
subprocess.run(comando)
```

### Backend GitHub

```bash
perceval github owner repo -t seu_token_aqui
```

**Características:**
- ✅ Extrai **issues**, **pull requests**, **comentários** e mais
- ✅ Acessa via API REST do GitHub (autenticado)
- ✅ **REQUER token de autenticação** (mais requisições permitidas)
- ✅ Respeita limite de taxa (rate limit) da API
- ✅ Dados mais ricos (labels, assignees, reviewers, etc)
- ✅ Melhor para análise de colaboração
- ❌ Mais lento devido às requisições HTTP
- ❌ Limitado a 60 requisições por hora sem token

**Exemplo de uso:**
```python
comando = [
    "perceval", "github", "owner", "repo",
    "-t", "seu_token_aqui",
    "--sleep-for-rate"  # Pausa automaticamente ao atingir limite
]
subprocess.run(comando)
```

### Quadro Comparativo

| Aspecto | Git | GitHub |
|--------|-----|--------|
| **Dados extraídos** | Commits | Issues, PRs, Discussões, Eventos |
| **Autenticação** | Não necessária | Token requerido |
| **Velocidade** | Mais rápida | Mais lenta |
| **Limite** | Nenhum (local) | API rate limit (5000/hora com token) |
| **Protocolo** | Git (local/HTTP/SSH) | API REST HTTP |
| **Metadados ricos** | Médios | Muito ricos |
| **Uso ideal** | Análise de código/histórico | Análise de colaboração/workflow |

**Recomendação:**
- Use **Git** quando precisar de análise de commits e histórico de código
- Use **GitHub** quando precisar entender dinâmica de colaboração (PRs, reviews, issues)
- Use **ambos** para uma análise completa

## 🔍 Verificar Status da Extração

Verifique os dados extraídos no OpenSearch:

```bash
# Listar índices
curl http://localhost:9200/_cat/indices

# Contar documentos em um índice
curl http://localhost:9200/git_raw/_count

# Buscar documentos
curl http://localhost:9200/git_enriched/_search?pretty
```

## 🧹 Limpar os Dados

Para reiniciar do zero:

```bash
# Parar e remover containers
docker-compose down

# Remover volume de dados (ATENÇÃO: isto apaga os dados!)
docker volume rm grimoire_db_data

# Limpar arquivos locais de dados
rm -rf dados_commits/
rm -f repositorios.json
```

## ⚠️ Troubleshooting

### Token do GitHub não encontrado
```
Erro: A variável de ambiente GITHUB_TOKEN não está definida.
```
**Solução:** Execute `export GITHUB_TOKEN=$(grep GITHUB_TOKEN .env | cut -d '=' -f 2)`

### OpenSearch não está respondendo
```
Erro: Connection refused (localhost:9200)
```
**Solução:** Verifique se o Docker está rodando: `docker-compose ps`

### Perceval command not found
```
Erro: perceval: command not found
```
**Solução:** Instale Perceval com `pip install perceval`

### Rate limit da API do GitHub atingido
**Solução:** Aguarde 1 hora ou use o flag `--sleep-for-rate` no backend GitHub

## 📝 Arquivos Principais

- `fetch_github.py` - Busca repositórios com critérios específicos
- `perceval_extraction.py` - Extrai dados usando Perceval (git ou github)
- `ingestor.py` - Ingere dados no OpenSearch
- `p2o.py` - Perceval to OpenSearch (realiza enriquecimento e ingestão)
- `docker-compose.yml` - Configuração dos serviços (OpenSearch + Dashboards)
- `repositorios.json` - Lista de repositórios (gerado pelo fetch_github.py)
- `.env` - Variáveis de ambiente (contém o token do GitHub)

## 📚 Documentação Adicional

- [Perceval Documentation](https://github.com/grimoirelab/perceval)
- [OpenSearch Documentation](https://opensearch.org/docs/latest/)
- [GitHub API Documentation](https://docs.github.com/en/rest)

## 📄 Licença

Este projeto é fornecido como está para fins educacionais e de pesquisa.
