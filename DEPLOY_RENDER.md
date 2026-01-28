# Enviar atualização para o Render

Execute estes comandos **no terminal** (PowerShell ou CMD) dentro da pasta do projeto:

```powershell
cd c:\dev\TRAE\codexia

# 1. Adicionar as alterações
git add app/services/video_generator.py app/services/video_processing.py

# 2. Fazer o commit
git commit -m "fix: video 95%->24% - preset ultrafast no render, progresso nunca diminui"

# 3. Enviar para o GitHub (o Render faz deploy automático ao detectar o push)
git push origin main
```

**Se aparecer erro de `.git/index.lock`:**  
Feche o Cursor/VS Code, abra o terminal fora do editor e rode os comandos de novo.  
Se continuar, apague o arquivo e tente novamente:
```powershell
Remove-Item -Force .git\index.lock -ErrorAction SilentlyContinue
```

**Depois do push:**  
O Render deve iniciar um novo deploy sozinho. Acompanhe no painel do Render em  
[https://dashboard.render.com](https://dashboard.render.com) → seu serviço → **Events** ou **Logs**.
