# 🚀 Configurar Deploy Automático no Render

## ✅ Status Atual

**Push realizado com sucesso!** 
- Commit: `0e0f50b docs: adiciona scripts e guia de deploy`
- Repositório: `https://github.com/evarantes/codexia-app.git`
- Branch: `main`

---

## 🔧 Como Configurar Deploy Automático no Render

### Passo 1: Verificar se já está configurado

1. Acesse: https://dashboard.render.com
2. Vá em seu serviço **Codexia**
3. Clique em **Settings** (Configurações)
4. Procure por **"Auto-Deploy"** ou **"Build & Deploy"**

### Passo 2: Se NÃO estiver configurado, configure assim:

1. Na seção **"Build & Deploy"**, procure por:
   - **"Auto-Deploy"** → Deixe marcado como **"Yes"**
   - **"Branch"** → Deve estar como **"main"**
   - **"Root Directory"** → Deixe vazio (ou `/` se pedir)
   - **"Build Command"** → Deixe vazio ou coloque: `pip install -r requirements.txt`
   - **"Start Command"** → Deve ter algo como: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

2. Na seção **"Source"**, verifique:
   - **"Repository"** → Deve estar conectado ao `evarantes/codexia-app`
   - Se não estiver, clique em **"Connect GitHub"** e autorize o acesso

3. **Salve** as configurações

### Passo 3: Testar Deploy Automático

Agora, sempre que você (ou eu) fizer um `git push origin main`, o Render vai:
1. Detectar automaticamente o novo commit
2. Iniciar um novo build
3. Fazer deploy da nova versão

**Para testar:**
- Faça qualquer mudança pequena no código
- Execute: `git add .`, `git commit -m "test"`, `git push origin main`
- Vá no Render → Events → Você deve ver um novo deploy iniciando automaticamente

---

## 📝 Como Usar o Script de Deploy

Agora você pode usar o script `DEPLOY_COMPLETO.ps1` para fazer deploy facilmente:

```powershell
# No PowerShell, dentro da pasta do projeto:
.\DEPLOY_COMPLETO.ps1
```

Ou execute manualmente:

```powershell
cd c:\dev\TRAE\codexia
git add .
git commit -m "sua mensagem aqui"
git push origin main
```

**O Render vai detectar automaticamente e fazer o deploy!**

---

## 🔍 Verificar se Deploy Automático Está Funcionando

1. Vá em: https://dashboard.render.com → Seu serviço → **Events**
2. Você deve ver eventos como:
   - `Deploy started` (quando detecta novo commit)
   - `Build succeeded` (quando build termina)
   - `Deploy live` (quando deploy está completo)

Se você ver esses eventos aparecendo automaticamente após um `git push`, está funcionando! ✅

---

## ⚠️ Troubleshooting

### Deploy não está acontecendo automaticamente?

1. **Verifique se o GitHub está conectado:**
   - Render → Settings → Source → Deve mostrar seu repositório

2. **Verifique se Auto-Deploy está ativado:**
   - Render → Settings → Build & Deploy → Auto-Deploy = Yes

3. **Verifique se está na branch correta:**
   - Render → Settings → Branch = `main`

4. **Verifique webhooks do GitHub:**
   - GitHub → Seu repositório → Settings → Webhooks
   - Deve ter um webhook do Render configurado automaticamente

### Ainda não funciona?

- Tente fazer um **Manual Deploy** primeiro para garantir que o código está correto
- Verifique os **Logs** no Render para ver se há erros de build
- Entre em contato com suporte do Render se necessário

---

## 🎯 Próximos Passos

Agora que o deploy automático está configurado:

1. ✅ Sempre que eu fizer mudanças, vou fazer `git push origin main`
2. ✅ O Render vai detectar automaticamente
3. ✅ O deploy vai acontecer sozinho
4. ✅ Você só precisa aguardar alguns minutos e testar!

**Não precisa mais fazer "Manual Deploy" manualmente!** 🎉
