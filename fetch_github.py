import os
import requests
import time
import json
from datetime import datetime
from dotenv import load_dotenv

# Recomenda-se colocar o token em uma variável de ambiente:
# No Linux/WSL: export GITHUB_TOKEN="seu_token_aqui"
load_dotenv()
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# Cabeçalhos recomendados pela documentação oficial
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28" # Especificar a versão evita quebras futuras
}

def filter_repos_by_stars(repos_list, min_stars=100):
    print(f"\n[+] Iniciando filtragem por estrelas (mínimo: {min_stars})...")
    print(f"Total de repositórios para analisar: {len(repos_list)}")
    
    repos_aprovados = []
    
    for i, repo_name in enumerate(repos_list):
        # Endpoint para pegar os detalhes do repositório
        url = f"https://api.github.com/repos/{repo_name}"
        
        response = requests.get(url, headers=HEADERS)
        
        # Tratamento de Rate Limit da API principal (Core API)
        if response.status_code == 403 or response.status_code == 429:
            reset_time = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
            sleep_time = max(reset_time - time.time(), 0) + 1
            print(f"⚠️ Limite da Core API atingido. Pausando por {sleep_time:.0f} segundos...")
            time.sleep(sleep_time)
            # Tenta novamente a mesma requisição
            response = requests.get(url, headers=HEADERS)
            
        if response.status_code == 200:
            data = response.json()
            estrelas = data.get("stargazers_count", 0)
            
            if estrelas > min_stars:
                repos_aprovados.append(repo_name)
                print(f"  [OK] {repo_name} aprovado com {estrelas} estrelas.")
            else:
                # Opcional: imprimir quando é rejeitado (comentado para não poluir o terminal)
                # print(f"  [X] {repo_name} rejeitado ({estrelas} estrelas).")
                pass
        else:
            print(f"  [Erro] Não foi possível acessar {repo_name}: Status {response.status_code}")
            
        # Progresso a cada 50 repositórios para você saber que não travou
        if (i + 1) % 50 == 0:
            print(f"Progresso: {i + 1}/{len(repos_list)} analisados...")
            
    return repos_aprovados



def search_github_code():
    url = "https://api.github.com/search/code"
    repos = set()
    
    # 1. Separamos os arquivos para evitar o uso problemático do "OR"
    filenames = [
        # Modelos Anthropic / Google
        "claude.md", 
        "gemini.md", 
        ".geminirules",
        "gemini-instructions.md",
        
        # Copilot / Cursor / OpenAI
        ".cursorrules", 
        "copilot-instructions.md",
        "codex.md",
        "openai.md",
        
        # Padrões Genéricos de Comunidade
        "system_prompt.md",
        "prompt.txt",
        
        # Manifestos de Skills / Tools
        "skills.json",
        "tools.yaml",
        "openapi.yaml"
    ]
    
    # 2. Geramos faixas de tamanho de forma programática.
    tamanho_minimo = 150
    tamanho_maximo = 1000
    passo_inicial = 50
    tamanho_minimo_faixa = 1

    def executar_query(filename, inicio, fim, nivel=0):
        query = f"filename:{filename} size:{inicio}..{fim}"
        indent = "  " * nivel
        print(f"\n{indent}[+] Buscando: {query}")

        page = 1
        per_page = 100
        total_count = 0

        while page <= 10:
            params = {
                "q": query,
                "per_page": per_page,
                "page": page,
            }

            response = requests.get(url, headers=HEADERS, params=params)

            if response.status_code == 403 or response.status_code == 429:
                reset_time = int(response.headers.get("X-RateLimit-Reset", time.time() + 30))
                sleep_time = max(reset_time - time.time(), 0) + 1
                print(f"{indent}⚠️ Limite atingido. Pausando por {sleep_time:.0f} segundos...")
                time.sleep(sleep_time)
                continue

            if response.status_code != 200:
                print(f"{indent}Erro {response.status_code}: {response.text}")
                return 0

            data = response.json()
            items = data.get("items", [])
            total_count = data.get("total_count", 0)

            if not items:
                break

            for item in items:
                repo_name = item["repository"]["full_name"]
                repos.add(repo_name)

            if len(items) < per_page:
                break

            page += 1
            time.sleep(2)

        return total_count

    def buscar_faixa(filename, inicio, fim, passo_atual, nivel=0):
        total_count = executar_query(filename, inicio, fim, nivel=nivel)

        if total_count > 1000 and inicio < fim:
            if passo_atual <= tamanho_minimo_faixa:
                print(
                    f"{'  ' * nivel}⚠️ A query '{filename}' ainda passou de 1000 resultados "
                    f"({total_count}) mesmo com a menor faixa disponível ({inicio}..{fim})."
                )
                return

            novo_passo = max(passo_atual // 2, tamanho_minimo_faixa)
            print(
                f"{'  ' * nivel}↳ Mais de 1000 resultados ({total_count}). "
                f"Reduzindo a faixa de {inicio}..{fim} para passo {novo_passo}."
            )

            sub_inicio = inicio
            while sub_inicio <= fim:
                sub_fim = min(sub_inicio + novo_passo - 1, fim)
                buscar_faixa(filename, sub_inicio, sub_fim, novo_passo, nivel=nivel + 1)
                sub_inicio = sub_fim + 1

    # O script agora itera sobre cada arquivo e adapta a faixa quando necessário
    for filename in filenames:
        inicio = tamanho_minimo
        while inicio <= tamanho_maximo:
            fim = min(inicio + passo_inicial - 1, tamanho_maximo)
            buscar_faixa(filename, inicio, fim, passo_inicial)
            inicio = fim + 1

    return list(repos)

def save_repositories_to_file(repos, filename="repositorios.json"):
    """
    Salva a lista de repositórios em um arquivo JSON
    
    Args:
        repos (list): Lista de repositórios a salvar
        filename (str): Nome do arquivo de saída
    """
    data = {
        "timestamp": datetime.now().isoformat(),
        "total_repositories": len(repos),
        "repositories": repos
    }
    
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    print(f"\n✓ Repositórios salvos em '{filename}'")

if __name__ == "__main__":
    if not GITHUB_TOKEN:
        print("Erro: A variável de ambiente GITHUB_TOKEN não es   tá definida.")
    else:
        lista_repositorios = search_github_code()
        
        print("\n=== Resultados ===")
        print(f"Total de repositórios únicos encontrados: {len(lista_repositorios)}")
        for repo in lista_repositorios[:10]: # Imprime os 10 primeiros como exemplo
            print(f"- {repo}")
        
        lista_repositorios_filtrada = filter_repos_by_stars(lista_repositorios, min_stars=100)
        
        
        # Salva os repositórios em um arquivo
        save_repositories_to_file(lista_repositorios_filtrada)