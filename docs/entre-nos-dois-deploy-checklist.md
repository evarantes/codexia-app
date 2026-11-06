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

## Revisão privada das quatro falas em MP3

O Studio agora permite importar um MP3 por fala do roteiro, ouvir cada arquivo
no navegador e consultar sua duração real, **sem fazer nenhuma chamada de
geração paga**. Os arquivos são privados por administrador, limitados a
10 MB e 45 segundos cada; somente administradores autenticados conseguem
importar ou reproduzir. O roteiro e a ordem Helena/Gabriel permanecem fixos.

Esse recurso permite avaliar narração de arquivos gerados fora do Studio,
mas **não prova por si só que as vozes são Andrey e Luciana**, nem garante
sincronização labial, qualidade ou custo. O operador deve verificar os áudios
antes de aprovar a etapa audiovisual. Não compartilhar URLs de download
protegidas nem guardar os arquivos no repositório Git.

O CI de homologação gera um MP3 sintético de um segundo pelo FFmpeg apenas
para verificar o upload, leitura autenticada e bloqueio de acesso anônimo.
Esse sinal de teste não é áudio de personagem.

## Ainda não disponível

- O painel **não** produz áudio nem vídeo; não há botão de geração paga liberado.
- As imagens separadas de cada personagem precisam ser enviadas ao armazenamento privado do servidor; a imagem conjunta aprovada anteriormente não foi importada automaticamente.
- Nenhum deploy de produção é realizado por este PR em rascunho.
- O worker local no PC ficará para fase posterior: agora a operação fica no Codexia e nos fornecedores externos de IA.

## Recuperação segura de falhas

Em falhas incertas após chamadas pagas, **não repetir automaticamente** uma solicitação. Verificar no painel do fornecedor se a geração já ocorreu. Preservar os registros de reserva no armazenamento persistente e fazer conciliação manual antes de qualquer reenvio. Não usar diretório temporário da imagem Docker para armazenar recibos ou mídias originais.

## Smoke HTTP de homologação (sem créditos)

Depois de iniciar uma **instância de homologação** com o volume persistente,
execute, a partir de uma máquina com acesso ao serviço:

```bash
python scripts/entre_nos_dois_smoke.py --base-url https://URL-DA-HOMOLOGACAO
```

Esse primeiro comando apenas confirma a página HTML e que as APIs privadas
recusam acessos anônimos. Para verificar as respostas administrativas,
defina `CODEXIA_STUDIO_SMOKE_TOKEN` na variável de ambiente local (não no
repositório e não na linha de comando) e execute:

```bash
python scripts/entre_nos_dois_smoke.py --base-url https://URL-DA-HOMOLOGACAO --admin-checks
```

O resultado é JSON com `all_passed` e cada verificação; um erro retorna código
de saída diferente de zero. **Não verifica persistência após restart**, que
deve ser testada manualmente no Coolify; tampouco faz upload, TTS, animação
ou publicação. Nunca reutilize um token de administrador em logs públicos.

## Primeira homologação isolada: Docker Compose

A homologação usa `app.studio_staging:app`, um aplicativo FastAPI limitado
às rotas do Studio, login administrativo e saúde. **Não** inicia o painel
principal, monitor de produção, recuperações de filas, workers ou publicação.
O Dockerfile reduzido cria e valida somente as tabelas necessárias ao Studio
(`tenants`, `users` e `settings`) no PostgreSQL isolado. Se a verificação
do esquema falhar, a API não inicia. Isso não substitui as migrações Alembic
do Codexia principal e não deve ser usado contra bancos de produção.

O arquivo `docker-compose.entre-nos-dois-staging.yml` foi adicionado
como **opção de homologação independente**, com PostgreSQL e armazenamento
de imagens exclusivos. Não usa Redis, módulos de renderização, Chromium nem
volumes do Codexia de produção. A imagem `Dockerfile.studio-staging` evita
a instalação completa da fábrica de vídeos.
A porta da API fica acessível somente por `127.0.0.1:18080` no host.

Em um ambiente que tenha Docker Compose e o checkout da branch do PR:

```bash
# Configure estes valores como segredos fortes no ambiente privado, nunca no Git:
export CODEXIA_STAGING_DB_PASSWORD='PREENCHER_LOCALMENTE'
export CODEXIA_STAGING_SECRET_KEY='PREENCHER_LOCALMENTE'
docker compose -f docker-compose.entre-nos-dois-staging.yml config --quiet
docker compose -f docker-compose.entre-nos-dois-staging.yml up --build -d
python scripts/entre_nos_dois_smoke.py --base-url http://127.0.0.1:18080
```

**Não execute literalmente os placeholders acima:** use valores aleatórios
fortes diferentes dos de produção; não copie credenciais de produção. A construção
da imagem, mesmo reduzida, consome recursos do servidor. Validar RAM, CPU e
especialmente espaço livre antes de iniciar. Não habilitar um domínio público sem TLS e autenticação.

Criar um **administrador próprio de homologação**, sem copiar usuários,
tokens ou dados da produção. Após confirmar as tabelas isoladas do banco e que ele
está vazio, executar interativamente **somente no container de homologação**:

```bash
docker compose -f docker-compose.entre-nos-dois-staging.yml exec studio-api python -m scripts.entre_nos_dois_staging_admin
```

O comando solicita o e-mail e a senha nova pelo terminal (sem ecoar a senha).
Exige `APP_ENV=staging`, exige o banco isolado `studio-db/codexia_staging`,
e recusa a criação quando já existe qualquer usuário. **Nunca** utilize
a mesma senha do Codexia de produção. O usuário de homologação precisa
alterar sua senha no primeiro acesso.

No primeiro login pelo formulário do Studio, a API retorna a exigência de
troca de senha e a interface exibe o formulário de alteração. **As rotas
privadas do Studio recusam acesso enquanto a senha temporária não for
substituída.** A troca é feita na mesma origem pelo endpoint
`POST /staging/change-password`, com sessão autenticada de homologação,
sem enviar credenciais a serviços externos. Após a alteração, a interface
libera a prévia e o cadastro de personagens. A sessão expira após uma hora.

Depois, abrir `http://127.0.0.1:18080/studio/entre-nos-dois/painel`
**no navegador do próprio host** (ou através de túnel SSH local autorizado).
O Studio agora oferece formulário de login administrativo. O login envia
as credenciais para o endpoint `/token` da **mesma origem** e verifica
a permissão no endpoint protegido antes de guardar a sessão temporária na aba.
Em acesso remoto, **não exponha** a porta de homologação publicamente para
facilitar o login: use um túnel SSH ou HTTPS protegido. Não usar credenciais
de produção. A opção Sair bloqueia o aproveitamento de uma sessão da
interface principal naquela aba.

Para o smoke automatizado autenticado, obter um token de homologação pelo
fluxo normal do Codexia e configurar `CODEXIA_STUDIO_SMOKE_TOKEN` somente
no ambiente local, como explicado acima. Nunca imprima o token nos logs.

Para desligar sem apagar a biblioteca de imagens, use:

```bash
docker compose -f docker-compose.entre-nos-dois-staging.yml down
```

**Não use `down -v`**, pois isso apaga os volumes isolados de banco e
imagens desta homologação. Não use este Compose no mesmo projeto Compose
da instalação de produção nem altere os nomes dos volumes para os de produção.
O workflow `Entre Nos Dois Voice Tests` também inicia essa imagem e executa
o smoke HTTP em um runner temporário do GitHub Actions, sem acessar o
servidor do usuário nem gerar mídia paga. A instalação no Coolify
continua exigindo autorização de acesso operacional e conferência de
recursos no servidor. **Não foi iniciada remotamente por esta PR**.
