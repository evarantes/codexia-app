# ✅ VERIFICAÇÃO DE DEPLOY - YouTube Auto + Download de Livros

## 📋 O QUE FOI IMPLEMENTADO

### 1. **YouTube Auto → Auto Análise**
- ✅ Subaba "Auto Análise" criada
- ✅ Botão "Rodar Análise Agora" que chama `/youtube/auto_insights`
- ✅ Exibe: Resumo do canal, Ideias de vídeos longos, Ideias de shorts
- ✅ Botão "Usar plano da IA para agendar vídeos" que preenche o Planejamento Automático

### 2. **YouTube Auto → Monetização**
- ✅ Subaba "Monetização" criada
- ✅ Botão "Atualizar Status" que chama `/youtube/monetization_status`
- ✅ Exibe: Resumo da IA, Barras de progresso (inscritos/horas), "Falta aproximadamente...", Ações semanais sugeridas

### 3. **Meus Livros → Download**
- ✅ Ícone de download nos cards
- ✅ Rota `/books/{id}/download` que regenera PDF se arquivo não existir mais

---

## 🔍 COMO VERIFICAR SE ESTÁ FUNCIONANDO

### Passo 1: Verificar se o deploy aconteceu no Render

1. Acesse: https://dashboard.render.com
2. Vá em seu serviço **Codexia**
3. Aba **Events** ou **Logs**
4. Procure por um deploy **recente** (últimos minutos/horas)
5. Verifique se o deploy **terminou com sucesso** (status verde)

### Passo 2: Limpar cache do navegador

**IMPORTANTE:** O navegador pode estar mostrando versão antiga em cache!

1. Abra o app em **aba anônima** (Ctrl+Shift+N no Chrome)
2. OU pressione **Ctrl+F5** na página do app (força reload sem cache)
3. OU limpe o cache: F12 → Network → "Disable cache" → F5

### Passo 3: Testar as funcionalidades

#### **Teste Auto Análise:**
1. Clique em **YouTube Auto** (menu lateral)
2. Você deve ver **3 botões no topo**: "Produção" | "Auto Análise" | "Monetização"
3. Clique em **"Auto Análise"**
4. Você deve ver um card roxo com título "Auto Análise do Canal"
5. Clique em **"Rodar Análise Agora"**
6. Aguarde alguns segundos
7. Deve aparecer: Resumo + listas de ideias + botão "Usar plano da IA..."

#### **Teste Monetização:**
1. Clique em **"Monetização"** (ao lado de Auto Análise)
2. Você deve ver um card verde com título "Caminho para Monetização"
3. Clique em **"Atualizar Status"**
4. Aguarde alguns segundos
5. Deve aparecer: Resumo + barras de progresso + "Falta aproximadamente..." + Ações semanais

#### **Teste Download de Livros:**
1. Vá em **Meus Livros**
2. Cada livro deve ter um **ícone de download** (⬇) ao lado de editar/excluir
3. Clique no ícone
4. Deve baixar o PDF (ou regenerar se não existir mais)

---

## 🔴 ERRO "RAN OUT OF MEMORY" (512MB) NO RENDER

Se no Render aparecer **"Instance failed: Ran out of memory (used over 512MB)"**:

1. **O que foi feito no código:** Os módulos pesados (moviepy, PIL, numpy) passaram a ser carregados só quando necessário (lazy import). Isso reduz o uso de memória no **startup** e ajuda a subir dentro do limite de 512MB do plano Starter.

2. **Se ainda falhar:** O plano **Starter** do Render tem **512MB de RAM**. Gerar vídeo com IA (MoviePy, imagens, áudio) consome bastante memória. Opções:
   - **Recomendado:** Fazer **upgrade do plano** no Render (ex.: Standard com mais RAM) para o serviço **codexia**. Dashboard → codexia → **Settings** → **Instance Type**.
   - Verificar em **Metrics** se o pico de memória ocorre no startup ou ao gerar vídeo; se for ao gerar, o upgrade de plano costuma resolver.

3. **Depois de alterar o código:** Faça commit e push (incluindo as alterações de lazy import). O Render fará um novo deploy. Confira em **Logs** se o serviço sobe sem "Ran out of memory".

---

## 🔴 ERRO 502 BAD GATEWAY

Se o site mostra **502 Bad Gateway** no Render:

1. **Teste o health check:** Abra no navegador: `https://codexia-psh3.onrender.com/health`  
   - Se responder `{"status":"ok"}` → o app está no ar; o 502 pode ser temporário (tente F5).  
   - Se também der 502 → o processo não está subindo.

2. **Veja os Logs no Render:**  
   Dashboard → seu serviço Codexia → **Logs**.  
   Procure por erros ao iniciar (ex.: `Error`, `Exception`, `DATABASE_URL`, `ModuleNotFoundError`).  
   O startup foi tornado resiliente: migrações, usuário padrão e MonitorService não derrubam mais o app; erros são apenas logados.

3. **Confirme o Procfile:** Deve ter:  
   `web: gunicorn -w 1 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:$PORT app.main:app`  
   (obrigatório `-b 0.0.0.0:$PORT` para o Render conseguir conectar.)

4. **Force um novo deploy** (Manual Deploy) após alterações no código.

---

## ⚠️ SE AINDA NÃO ESTIVER FUNCIONANDO

### Verificar Console do Navegador (para erros JavaScript):

1. Pressione **F12** no navegador
2. Aba **Console**
3. Procure por erros em vermelho
4. Se houver erros, copie e me envie

### Verificar se as rotas estão respondendo:

1. Com F12 aberto, aba **Network**
2. Clique em "Rodar Análise Agora"
3. Procure por requisição para `/youtube/auto_insights`
4. Clique nela e veja a resposta
5. Se der erro 404 ou 500, me informe

### Forçar novo deploy no Render:

Se o deploy não aconteceu automaticamente:

1. No painel do Render, vá em seu serviço
2. Clique em **"Manual Deploy"** → **"Deploy latest commit"**
3. Aguarde o deploy terminar
4. Teste novamente

---

## 📝 ARQUIVOS MODIFICADOS

Os seguintes arquivos foram alterados e devem estar no último commit:

- `app/routers/youtube.py` - Rotas `/youtube/auto_insights` e `/youtube/monetization_status`
- `app/services/youtube_service.py` - Métodos `get_recent_videos_performance()` e `get_monetization_progress()`
- `app/services/ai_generator.py` - Métodos `generate_auto_insights()` e `generate_monetization_insights()`
- `app/static/index.html` - Subabas, botões e métodos JavaScript
- `app/routers/books.py` - Rota `/books/{id}/download` com regeneração

---

## 🚀 ÚLTIMOS COMMITS (já enviados)

```
576dc16 feat: YouTube Auto - subabas de Auto Análise e Monetização com IA
ed05cab feat: YouTube Auto - auto análise aplicando plano e painel de monetização
35bcefc feat: botão de download nos cards de Meus Livros
5514cbd fix: download de livros com rota /books/{id}/download e regeneração de PDF
```

Se você não está vendo essas funcionalidades, o problema é:
1. **Deploy não aconteceu** → Force manual deploy no Render
2. **Cache do navegador** → Use aba anônima ou Ctrl+F5
3. **Erro no código** → Verifique Console do navegador (F12)
