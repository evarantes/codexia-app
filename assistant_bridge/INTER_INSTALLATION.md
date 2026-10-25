# Integração segura entre instalações Codexia — etapa 1

## O que já existe

A instalação principal expõe `GET /api/internal/codexia/music-capability` apenas com `Authorization: Bearer <token>`. A resposta contém somente `suno_available` e `provider`; não contém a chave do Suno nem dados de usuários. O cliente em `assistant_bridge/remote_client.py` consulta o endpoint por HTTPS, com timeout e sem seguir redirecionamentos.

## Configuração necessária

Implantar o código atualizado nas **duas** instalações (a principal precisa ter a rota interna). Definir `CODEXIA_INTERNAL_SERVICE_TOKEN` com o **mesmo segredo aleatório de pelo menos 32 caracteres** em ambas, disponível somente em runtime. Na instalação de testes, definir `CODEXIA_MAIN_INTERNAL_URL` com a URL HTTPS exata da instalação principal, sem barra final. Não usar o token público do bridge, a senha de login, a chave Suno ou a chave JWT como segredo de serviço. Não enviar segredos pelo chat.

**Atenção:** ainda não existe execução remota de música, transferência de áudio, autorização delegada por usuário ou vínculo de tarefas entre as instalações. O endpoint é exclusivamente de diagnóstico e não consome créditos. Não habilitar geração remota até definir identidade de serviço, autorização do usuário titular, armazenamento compartilhado/transferência de áudio e idempotência.

## Validação

1. Confirmar que a principal responde 401 sem o token e 503 quando não configurada.
2. Confirmar que o token correto retorna somente disponibilidade booleana.
3. Confirmar que a instalação de testes consegue consultar a principal por HTTPS.
4. Validar o vínculo de usuário e o fluxo de música/áudio em ambiente de testes antes de liberar geração paga.
