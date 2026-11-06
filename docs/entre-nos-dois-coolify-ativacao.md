# Entre Nós Dois — ativação operacional controlada (Coolify)

**Objetivo:** colocar o Studio administrativo em uma instância separada, sem
interromper, atualizar ou acessar o banco, os volumes ou os workers do Codexia
de produção. Este documento é para uma implantação de *homologação*, não a
publicação do primeiro episódio.

## 1. Segurança e recursos — antes de clicar em Deploy

- Conferir no host **espaço livre em disco**, memória e carga. Se o disco
  estiver quase cheio, **não construir a imagem no mesmo host**; primeiro
  disponibilizar armazenamento ou escolher outro host. O Compose limita a
  API a 768 MiB/1 CPU e o PostgreSQL a 512 MiB/0,75 CPU, mas a compilação
  da imagem Docker também pode consumir recursos e espaço extra.
- Confirmar que há backup consistente e testado da produção. Não encerrar
  backups em andamento nem reutilizar volumes para acelerar a instalação.
- Criar **novo projeto de homologação** no Coolify, distinto do projeto
  principal. Não selecionar a aplicação existente para redeploy.
- Configurar origem Git privada `evarantes/codexia-app`,
  branch `feature/entre-nos-dois-personagens-mvp`, e arquivo
  `docker-compose.entre-nos-dois-staging.yml` como Docker Compose.
  Revisar se a interface suporta esse método na versão instalada.
- Gerar e cadastrar no **Coolify** duas variáveis de ambiente diferentes,
  fortes e aleatórias: `CODEXIA_STAGING_DB_PASSWORD` e
  `CODEXIA_STAGING_SECRET_KEY`. Não copiar chaves do Codexia principal,
  nem registrar os valores em prints, Git ou conversa.
- **Não configurar** chaves ElevenLabs, fal.ai ou YouTube nesse ambiente.
  `CODEXIA_STUDIO_ENABLE_PAID_TTS=0` e
  `CODEXIA_STUDIO_MAX_TTS_USD=0` já estão explícitos no Compose.

## 2. Primeira ativação

1. Validar no Coolify o Compose *isolado* e os dois segredos necessários.
2. Manter a API vinculada apenas a `127.0.0.1:18080` no servidor.
   O banco `studio-db` **não** tem porta publicada.
3. Fazer deploy **somente** do projeto de homologação.
4. Confirmar `studio-db: healthy`, depois `studio-api: healthy`.
   Se qualquer container apresentar erro, conferir seus logs e **não**
   modificar os containers de produção.
5. No host/console seguro com acesso à API, verificar
   `http://127.0.0.1:18080/health`: resposta esperada
   `{"status":"ok","mode":"entre-nos-dois-staging"}`.
6. Criar uma conta administrativa **exclusiva** de homologação, usando
   `python -m scripts.entre_nos_dois_staging_admin` **dentro do
   container studio-api**, via terminal seguro (Coolify ou SSH autorizado).
   O comando é interativo, pede senha temporária e não copia nenhum usuário
   da produção. Sem um terminal seguro, a conta ainda não poderá ser criada;
   não implementar senhas padrão ou contas públicas por conveniência.
7. Acessar a página `/studio/entre-nos-dois/painel` pela própria
   máquina host ou por um **túnel SSH**. Para expor remotamente, primeiro
   configurar HTTPS por proxy reverso, autenticação e limites de acesso.
   Não abrir a porta 18080 na Internet para simplificar.
8. Fazer login com o administrador de homologação e trocar imediatamente
   a senha temporária por uma senha nova de 16–72 caracteres.
9. Verificar que as APIs retornam 401/403 sem login. Carregar uma imagem
   de teste, reiniciar **só** o container `studio-api` e confirmar que
   ela persiste no volume `studio_private`. Não usar ainda os
   retratos oficiais se o domínio/acesso não estiver protegido.

## 3. Critérios de conclusão da implantação

- Acesso administrativo funciona em HTTPS protegido ou túnel.
- Primeiro login obriga troca de senha; acesso anônimo às imagens e MP3
  é recusado.
- Banco e ativos persistem após reinício do Studio.
- O painel abre e mostra andamento dos quatro áudios, personagens,
  storyboard de reprodução e pendências de preparação das cenas.
- O sistema não recebeu nenhuma chave de IA paga e não disponibiliza
  nenhuma operação de geração paga.
- Nenhum worker, container ou banco da produção foi reiniciado.

## 4. Etapa seguinte (somente após homologação)

Importar retratos aprovados de Gabriel e Helena; validar as vozes
Andrey e Luciana na mesma API que será cobrada; produzir e escutar as
quatro falas com orçamento explícito; só então autorizar o envio de mídia
ao provedor de animação facial. O trailer de estreia será anunciado com
**data e horário apenas depois de o episódio completo estar aprovado**.

Não fazer merge do PR #298 enquanto estiver em rascunho e sem a
homologação operacional. Uma passagem verde no GitHub Actions não
equivale ao deploy no seu servidor.
