# Studio Entre Nós Dois — implantação controlada (primeira fase no servidor)

Este módulo permanece isolado no PR #298. **Não implantar apenas porque os testes passaram.**
Não há autorização para chamadas pagas ou publicação automática.

## Pré-requisitos no Coolify (serviço principal)

1. Criar **volume persistente privado**, montado no container do aplicativo (exemplo de caminho interno: `/data/codexia/studio-private`). Não montar sob `/static`, `/generated_assets` ou pasta pública.
2. Configurar a variável de ambiente `CODEXIA_STUDIO_PRIVATE_DIR=/data/codexia/studio-private`. O valor precisa corresponder ao ponto de montagem do volume. Confirmar permissões de leitura e escrita para o usuário do container. **Nunca** salvar as imagens oficiais diretamente no Git.
3. Utilizar a chave ElevenLabs já configurada em Settings do usuário administrador ou `ELEVENLABS_API_KEY` no ambiente; somente presença da chave é verificada automaticamente. O botão **Verificar vozes** faz consultas GET aos IDs oficiais e não cria áudio.
4. Para futura sincronização, `FAL_KEY` pode ser disponibilizada em variável privada do serviço. Sua presença **não** aprova cobrança, não comprova saldo e não executa tarefas na fal.ai.
5. Entrar no Codexia como administrador e abrir `/studio/entre-nos-dois/painel`. A página HTML é pública como estrutura visual, mas todas as APIs que exibem dados ou recebem imagens exigem Bearer JWT administrativo. O login precisa ter colocado o token `access_token` no armazenamento local do navegador.
6. Carregar separadamente os retratos oficiais de **Gabriel** e **Helena** (JPG/PNG/WebP, máximo 8 MB cada). Validar que ambos continuam disponíveis após reiniciar o container.
7. Clicar em **Verificar vozes (sem gerar áudio)**. Se o acesso direto aos IDs de Andrey e Luciana falhar, não substituir automaticamente por outras vozes; revisar se as vozes da biblioteca Runway também são acessíveis pela conta ElevenLabs.

## Critérios mínimos de aceitação

- CIs específicos e gerais verdes no mesmo SHA do PR, e revisão de mudanças do `app/main.py` e rotas.
- O painel abre após autenticação; usuário não administrador não consegue consultar dados privados, carregar ou ler imagens.
- Upload de Gabriel e Helena preservado após reinício e redeploy.
- Nunca retornar chaves, caminhos absolutos de storage ou URLs assinadas nos endpoints de diagnóstico.
- Testes de TTS e da fila fal.ai executam **somente mocks** no CI. O botão de verificação de voz só consulta metadados.
- Antes de qualquer chamada com cobrança: verificar preços na conta do provedor, conferir referências e áudio, definir limite, salvar recibo durável e confirmar explicitamente qual cena será gerada.
- Revisão humana obrigatória antes de renderizar todo o episódio ou publicar no YouTube.

## Ainda não disponível

- O painel **não** produz áudio nem vídeo; não há botão de geração paga liberado.
- As imagens separadas de cada personagem precisam ser enviadas ao armazenamento privado do servidor; a imagem conjunta aprovada anteriormente não foi importada automaticamente.
- Nenhum deploy de produção é realizado por este PR em rascunho.
- O worker local no PC ficará para fase posterior: agora a operação fica no Codexia e nos fornecedores externos de IA.

## Recuperação segura de falhas

Em falhas incertas após chamadas pagas, **não repetir automaticamente** uma solicitação. Verificar no painel do fornecedor se a geração já ocorreu. Preservar os registros de reserva no armazenamento persistente e fazer conciliação manual antes de qualquer reenvio. Não usar diretório temporário da imagem Docker para armazenar recibos ou mídias originais.
