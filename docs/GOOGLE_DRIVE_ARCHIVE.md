# Arquivo de produção no Google Drive

O Google Drive guarda os arquivos por projeto/tarefa, incluindo mídias e os manifestos necessários para identificar os ativos. Projetos concluídos e projetos pausados ou com falha permanecem recuperáveis. Ao retomar uma tarefa, o CX33 consulta o índice central e baixa os arquivos diretamente do Google Drive para o cache temporário, conferindo SHA-256 e tamanho antes de continuar.

Banco PostgreSQL, Redis, código, logs e cache ativo do renderizador não são movidos para o Drive. Isso evita tratar Google Drive como disco de banco de dados ou volume de trabalho do FFmpeg.

## Preparar o Google OAuth

1. No Google Cloud Console, selecione o projeto OAuth usado pela Codexia e habilite a **Google Drive API**.
2. Configure a tela de consentimento OAuth e autorize a conta Google que será dona do arquivo.
3. Use um OAuth Client do tipo **Web application**. Adicione exatamente a URL de callback exibida em Configurações da Codexia. Com `BASE_URL` em HTTPS, o deploy define automaticamente `/settings/google-drive/callback`; também pode ser configurada pela variável `GOOGLE_DRIVE_OAUTH_REDIRECT_URI`.
4. A Codexia solicita apenas o escopo `drive.file`, que permite ao app criar e acessar os próprios arquivos. O app cria a pasta `Codexia Archive` e subpastas por tarefa.
5. Abra **Configurações → Arquivo permanente no Google Drive**, dê um nome à conta, conecte-a e confirme. É possível conectar várias contas, definir a principal e pausar uma conta para novos projetos. Novas tarefas são distribuídas entre as contas ativas; cada projeto fica permanentemente associado à conta inicialmente escolhida. Projetos antigos continuam vinculados à conta original, mesmo quando ela é pausada. Os tokens de atualização ficam criptografados no banco usando `SECRET_KEY`; mantenha essa chave estável entre deploys.

## Arquivos já existentes

Após conectar, use **Arquivar projetos antigos e liberar espaço**. Cada rodada processa até cinco projetos concluídos, publicados, pausados ou com falha, sem atividade nas últimas 24 horas. Para o acervo que já está no servidor principal, a aplicação envia os ativos e manifestos para a pasta própria da tarefa e registra os IDs do Drive. Só remove arquivos quando o conteúdo foi confirmado e todas as tarefas que compartilham hard links têm cópias verificadas. Projetos ativos e recentes são ignorados. Rode novas rodadas para arquivar os projetos históricos restantes.

Novos uploads do CX33 usam uma sessão retomável: o servidor principal autoriza a sessão e registra os metadados, enquanto o conteúdo vai diretamente do CX33 ao Google Drive. O endpoint antigo de envio de mídia ao servidor principal responde `410 Gone` antes de ler o corpo. Quando uma tarefa termina ou falha, as cópias temporárias do worker só podem ser removidas após a confirmação do Drive e do índice. Se autorização, rede, checksum ou Drive falhar, o CX33 preserva o arquivo local para recuperação.
