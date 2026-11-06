# Arquivo de produção no Google Drive

O Google Drive guarda os arquivos por projeto/tarefa, incluindo mídias e os manifestos necessários para identificar os ativos. Projetos concluídos e projetos pausados ou com falha permanecem recuperáveis. Ao retomar uma tarefa, o CX33 consulta o índice central e baixa os arquivos diretamente do Google Drive para o cache temporário, conferindo SHA-256 e tamanho antes de continuar.

Banco PostgreSQL, Redis, código, logs e cache ativo do renderizador não são movidos para o Drive. Isso evita tratar Google Drive como disco de banco de dados ou volume de trabalho do FFmpeg.

## Preparar o Google OAuth

1. No Google Cloud Console, selecione o projeto OAuth usado pela Codexia e habilite a **Google Drive API**.
2. Configure a tela de consentimento OAuth e autorize a conta Google que será dona do arquivo.
3. Crie um OAuth Client do tipo **Aplicativo da Web**. Em **Configurações → Configurações OAuth do Google Drive** na Codexia, informe o Client ID e o Client Secret próprios do Drive. Eles ficam separados das credenciais do YouTube; o segredo é criptografado no banco usando `SECRET_KEY` e não é retornado pela API.
4. Copie a URL **Callback OAuth** exibida no painel do Google Drive da Codexia e cadastre-a em **URIs de redirecionamento autorizados** no OAuth Client. Com `BASE_URL` em HTTPS, o caminho é `/settings/google-drive/callback`; também pode ser configurada pela variável `GOOGLE_DRIVE_OAUTH_REDIRECT_URI`.
5. A Codexia solicita apenas o escopo `drive.file`, que permite ao app criar e acessar os próprios arquivos. O app cria a pasta `Codexia Archive` e subpastas por tarefa.
6. Na mesma página da Codexia, dê um nome à conta, conecte-a e confirme. É possível conectar várias contas, definir a principal e pausar uma conta para novos projetos. Novas tarefas são distribuídas entre as contas ativas; cada projeto fica permanentemente associado à conta inicialmente escolhida. Projetos antigos continuam vinculados à conta original, mesmo quando ela é pausada. Os tokens de atualização ficam criptografados no banco usando `SECRET_KEY`; mantenha essa chave estável entre deploys.

## Arquivos já existentes

Após conectar, use **Fazer backup completo e liberar espaço**. A tela percorre os projetos estáveis em lotes de até 25, incluindo mídia e manifestos reconhecidos pelos projetos, legendas e a biblioteca de músicas salvas. Projetos concluídos, publicados, pausados ou com falha entram após 24 horas sem atividade; tarefas ativas ou recentes ficam para uma rodada futura. Após 24 horas sem atividade, arquivos de projetos concluídos, pausados ou com falha podem ser removidos dos servidores quando todos os vínculos compartilhados tiverem cópias verificadas. Isso inclui vídeos finais e mídias da biblioteca de músicas; as informações da biblioteca e as letras continuam no banco, e áudio, capas e vídeos são transmitidos do Google Drive quando solicitados.

Cada arquivo só é registrado depois que o tamanho e o SHA-256 do Google Drive correspondem à origem. Se já houver um arquivo com o mesmo nome e conteúdo diferente, a cópia existente não é substituída: a nova é guardada com o SHA-256 no nome. A migração continua para os próximos projetos mesmo quando algum item falha; esses itens permanecem no disco e aparecem para revisão.

No servidor principal, hard links só são removidos depois que todas as tarefas que os compartilham têm uma cópia verificada do mesmo tipo e conteúdo. Depois, a aplicação envia ao worker do CX33 uma solicitação de limpeza para os projetos selecionados. O worker consulta o índice persistido do Drive e só remove arquivos estáveis cuja cópia está confirmada por tipo, SHA-256 e tamanho. Ele ignora tarefas em andamento, checkpoints do sistema de arquivos, links simbólicos e referências compartilhadas não verificadas. Vídeos e áudios arquivados são servidos diretamente do Drive; ações que exigem processamento local precisam restaurar uma cópia temporária verificada. Se a fila Redis/RQ estiver indisponível, a limpeza do CX33 não é executada e suas cópias permanecem no disco. A tela mostra os resultados dos dois servidores separadamente.

Em vídeos e músicas salvas, use **Buscar no backup** para listar as cópias confirmadas daquele projeto. Os links baixam os arquivos do Drive e expiram após dez minutos. Banco de dados, código, logs, cache ativo, letras e arquivos sem cópia confirmada no Drive não são removidos por esta rotina.

Novos uploads do CX33 usam uma sessão retomável: o servidor principal autoriza a sessão e registra os metadados, enquanto o conteúdo vai diretamente do CX33 ao Google Drive. O endpoint antigo de envio de mídia ao servidor principal responde `410 Gone` antes de ler o corpo. Quando uma tarefa termina ou falha, as cópias temporárias do worker só podem ser removidas após a confirmação do Drive e do índice. Se autorização, rede, checksum ou Drive falhar, o CX33 preserva o arquivo local para recuperação. Banco de dados, código, logs e cache ativo do renderizador continuam nos servidores.
