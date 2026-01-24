import os
import sys
import importlib
import pkgutil

def check_imports():
    print("Iniciando verificação de imports...")
    base_path = os.path.dirname(os.path.abspath(__file__))
    sys.path.append(base_path)
    
    error_count = 0
    
    # Caminha por todos os arquivos .py em app/
    for root, dirs, files in os.walk(os.path.join(base_path, "app")):
        for file in files:
            if file.endswith(".py") and file != "__init__.py":
                # Constrói o nome do módulo (ex: app.routers.video)
                rel_path = os.path.relpath(os.path.join(root, file), base_path)
                module_name = rel_path.replace(os.sep, ".").replace(".py", "")
                
                try:
                    print(f"Verificando {module_name}...", end=" ")
                    importlib.import_module(module_name)
                    print("OK")
                except Exception as e:
                    print(f"FALHA: {e}")
                    error_count += 1
                    
    if error_count == 0:
        print("\nTodos os módulos foram importados com sucesso!")
        sys.exit(0)
    else:
        print(f"\nEncontrados {error_count} erros de importação.")
        sys.exit(1)

if __name__ == "__main__":
    check_imports()
