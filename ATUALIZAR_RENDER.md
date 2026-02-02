# Atualizar o Render

Execute estes comandos **no terminal** (PowerShell ou CMD) dentro da pasta do projeto:

```powershell
cd c:\dev\TRAE\codexia

# 1. Adicionar todas as alterações
git add .

# 2. Fazer o commit
git commit -m "fix: startup resiliente, /health, Procfile PORT; YouTube: publicar agora, histórico com visualizar/regerar/republicar; fila não reenfileira completed"

# 3. Enviar para o GitHub (o Render faz deploy automático)
git push origin main
```

**Se aparecer erro de `.git/index.lock`:**
- Feche o Cursor/VS Code e rode os comandos de novo no terminal do Windows, **ou**
- Apague o lock e tente novamente:
  ```powershell
  Remove-Item -Force .git\index.lock -ErrorAction SilentlyContinue
  git add .
  git commit -m "fix: atualizações para Render e YouTube Auto"
  git push origin main
  ```

**Depois do push:**
1. Acesse [https://dashboard.render.com](https://dashboard.render.com)
2. Abra o serviço **Codexia**
3. Em **Events** ou **Logs**, aguarde o deploy terminar (alguns minutos)
4. Teste o site: `https://codexia-psh3.onrender.com`
5. Opcional: teste o health check: `https://codexia-psh3.onrender.com/health`

---

## Resumo do que será enviado

- **Procfile**: bind em `0.0.0.0:$PORT` (evitar 502)
- **app/main.py**: startup resiliente, endpoint `/health`
- **app/routers/youtube.py**: auth_url com verificação de credenciais; publicar agora; republicar; Histórico com visualizar/regerar/republicar
- **app/services/monitor_service.py**: vídeos em Aguardando Publicação não voltam para a fila (marcam failed se arquivo sumir)
- **app/static/index.html**: botões Publicar agora, Visualizar, Regenerar, Republicar; mensagens de erro YouTube
- **VERIFICAR_DEPLOY.md**: seção sobre 502, health check e **Ran out of memory**
- **Redução de memória (OOM):** Lazy import de `VideoGenerator` e `AIContentGenerator` em `video_processing.py`, `youtube.py`, `video.py` e `music.py` — moviepy/PIL/numpy só carregam quando for gerar vídeo, reduzindo o uso de RAM no startup (evita "Ran out of memory" no plano 512MB). Se ainda falhar, faça upgrade do plano no Render (Settings → Instance Type).
