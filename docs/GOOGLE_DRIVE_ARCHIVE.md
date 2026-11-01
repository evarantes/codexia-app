# Arquivo de produção no Google Drive

O Google Drive passa a guardar os vídeos, áudios e imagens finalizados. Durante uma produção, o CX33 ainda usa disco temporário para FFmpeg e cache; depois do upload validado, pode remover o cache. O servidor principal mantém os manifests e índices pequenos, mas não precisa manter outra cópia de cada mídia. Ao reproduzir ou retomar uma tarefa, o app lê a mídia do Drive em blocos.

Banco PostgreSQL, Redis, código, logs e cache ativo do renderizador não são movidos para o Drive. Isso evita tratar Google Drive como disco de banco de dados ou volume de trabalho do FFmpeg.

## Preparar o Google OAuth

1. No Google Cloud Console, selecione o projeto OAuth usado pela Codexia e habilite a **Google Drive API**.
2. Configure a tela de consentimento OAuth e autorize a conta Google que será dona do arquivo.
3. Use um OAuth Client do tipo **Web application**. Adicione exatamente a URL de callback exibida em Configurações da Codexia. Com `BASE_URL` em HTTPS, o deploy define automaticamente `/settings/google-drive/callback`; também pode ser configurada pela variável `GOOGLE_DRIVE_OAUTH_REDIRECT_URI`.
4. A Codexia solicita apenas o escopo `drive.file`, que permite ao app criar e acessar os próprios arquivos. O app cria a pasta `Codexia Archive` e subpastas por tarefa.
5. Abra **Configurações → Arquivo permanente no Google Drive → Conectar Google Drive**, escolha a conta e confirme. O token de atualização fica criptografado no banco usando `SECRET_KEY`; mantenha essa chave estável entre deploys.

## Arquivos já existentes

Após conectar, use **Liberar espaço migrando arquivos antigos**. Cada rodada migra até cinco arquivos de vídeo, áudio ou imagem com mais de 24 horas, começando pelos maiores. O servidor compara tamanho e MD5 confirmado pelo Drive, calcula novamente o SHA-256 local, registra o ID remoto e só então remove a cópia antiga. Arquivos ativos, recentes ou com erro permanecem no servidor. Rode novas rodadas conforme necessário.

Novos uploads recebidos do CX33 são transmitidos ao Drive por upload retomável em blocos de memória. Se a autorização, rede, checksum ou Drive falhar, a API não confirma a transferência e o CX33 preserva o arquivo local para recuperação.
