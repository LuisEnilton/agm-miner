import os
import random
import subprocess
import json
def extract_data_with_perceval(repos_list, github_token, backend_type="git", output_dir="perceval_data"):
    """
    Executa o Perceval para extrair dados de uma lista de repositórios.
    
    :param repos_list: Lista de repositórios no formato ['owner/repo', ...]
    :param github_token: Token de acesso do GitHub (necessário para o backend 'github')
    :param backend_type: 'git' para commits ou 'github' para issues/pull requests
    :param output_dir: Pasta onde os arquivos JSON serão salvos
    """
    # Cria a pasta de saída se ela não existir
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"[+] Diretório '{output_dir}' criado com sucesso.")

    print(f"\n[+] Iniciando extração em massa com Perceval [Backend: {backend_type}]")
    print(f"Total de repositórios a processar: {len(repos_list)}")

    for i, repo in enumerate(repos_list):
        try:
            owner, repo_name = repo.strip().split('/')
        except ValueError:
            print(f"  [Aviso] Linha inválida pulada: {repo}")
            continue

        # Define o nome do arquivo de saída (ex: perceval_data/git_owner_repo.json)
        filename = f"{backend_type}_{owner}_{repo_name}.json"
        output_file = os.path.join(output_dir, filename)
        
        print(f"\n[{i+1}/{len(repos_list)}] Processando: {repo}")

        # Configura o comando do Perceval dependendo do tipo de dado desejado
        if backend_type == "git":
            repo_url = f"https://github.com/{owner}/{repo_name}.git"
            comando = ["perceval", "git", repo_url]
            
        elif backend_type == "github":
            comando = [
                "perceval", "github", owner, repo_name,
                "-t", github_token,
                "--sleep-for-rate" # Pausa automaticamente se bater no limite da API
            ]
        else:
            print(f"  [Erro] Backend '{backend_type}' inválido. Use 'git' ou 'github'.")
            return

        try:
            # Abre o arquivo de texto e joga a saída do comando direto para ele (streaming)
            with open(output_file, "w") as f_output:
                # O argumento stderr=subprocess.PIPE captura logs de erro do Perceval se algo falhar
                resultado = subprocess.run(
                    comando, 
                    stdout=f_output, 
                    stderr=subprocess.PIPE, 
                    text=True, 
                    check=True
                )
            print(f"  [OK] Dados salvos com sucesso em: {output_file}")
            
        except subprocess.CalledProcessError as e:
            print(f"  [Erro] Falha no Perceval ao processar {repo}.")
            print(f"  Detalhes do erro: {e.stderr.strip()}")
            # Se o arquivo ficou vazio por causa do erro, removemos para não acumular lixo
            if os.path.exists(output_file) and os.path.getsize(output_file) == 0:
                os.remove(output_file)
                
        except Exception as e:
            print(f"  [Erro] Erro inesperado ao processar {repo}: {e}")
            
if __name__ == "__main__":
    # Recupera o token do ambiente
    TOKEN = os.getenv("GITHUB_TOKEN")
    
    if not TOKEN:
        print("Erro: A variável de ambiente GITHUB_TOKEN não está definida.")
    else:
        # Nome do arquivo JSON gerado pela etapa de busca
        arquivo_repos = "repositorios.json" 
        
        if not os.path.exists(arquivo_repos):
            print(f"Erro: O arquivo {arquivo_repos} não foi encontrado. Rode a busca primeiro.")
        else:
            print("[+] Lendo arquivo JSON...")
            
            # 1. Abre e faz o parse do JSON
            with open(arquivo_repos, "r", encoding="utf-8") as f:
                dados_json = json.load(f)
                
            # 2. Acessa a lista diretamente pela chave 'repositories'
            repositorios = dados_json.get("repositories", [])
            timestamp = dados_json.get("timestamp", "Data desconhecida")
            
            if not repositorios:
                print("Aviso: Nenhum repositório encontrado dentro da chave 'repositories'.")
            else:
                print(f"[+] Sucesso! {len(repositorios)} repositórios carregados. (Busca realizada em: {timestamp})")
                
                # 3. Executa a extração dos Commits (Git) passando a lista extraída
                extract_data_with_perceval(
                    repos_list= random.sample(repositorios, min(5, len(repositorios))), # Para teste, processa apenas os 5 primeiros repositórios
                    github_token=TOKEN, 
                    backend_type="github", 
                    output_dir="dados_commits"
                )
                
                print("\n=== Processo de extração concluído! ===")