import os
import json
import random
import subprocess

OPENSEARCH_URL = "http://localhost:9200"

def ingest_to_opensearch(sample_size=5):
    # O arquivo que contém a lista inicial de repositórios que você buscou
    arquivo_repos = "repositorios.json" 
    
    if not os.path.exists(arquivo_repos):
        print(f"Erro: O arquivo {arquivo_repos} não foi encontrado na raiz.")
        return

    with open(arquivo_repos, "r", encoding="utf-8") as f:
        dados_json = json.load(f)
        
    repositorios = dados_json.get("repositories", [])
    
    if not repositorios:
        print("Nenhum repositório encontrado no JSON.")
        return

    print(f"[+] Iniciando extração e ingestão direta de {len(repositorios)} repositórios...")

    for i, repo in enumerate(random.sample(repositorios, min(sample_size, len(repositorios)))):
        url_repo = f"https://github.com/{repo}.git"
        
        print(f"\n[{i+1}/{len(repositorios)}] Processando {url_repo}...")
        
        # O comando limpo, sem o parâmetro inexistente
        comando = [
            "poetry", "run", "python", "p2o.py",
            "--enrich",
            "--index", "git_raw",
            "--index-enrich", "git_enriched",
            "-e", OPENSEARCH_URL,
            "git", url_repo # O backend e a URL que o Perceval vai usar internamente
        ]
        
        try:
            # check=True garante que se o p2o.py falhar, ele cai no except
            subprocess.run(comando, check=True)
            print(f"  [OK] Ingestão concluída com sucesso.")
        except subprocess.CalledProcessError:
            print(f"  [ERRO] Falha ao processar {repo}.")

if __name__ == "__main__":
    ingest_to_opensearch()