# Script para fazer deploy completo das funcionalidades YouTube Auto + Download de Livros
# Execute este script no PowerShell (como Administrador se necessário)

Write-Host "=== DEPLOY CODEXIA PARA RENDER ===" -ForegroundColor Cyan
Write-Host ""

# 1. Navegar para o diretório do projeto
Set-Location "c:\dev\TRAE\codexia"
Write-Host "[1/6] Diretório: $(Get-Location)" -ForegroundColor Green

# 2. Remover locks do Git
Write-Host "[2/6] Removendo locks do Git..." -ForegroundColor Yellow
if (Test-Path ".git\index.lock") {
    Remove-Item -Force ".git\index.lock" -ErrorAction SilentlyContinue
    Write-Host "  ✓ Lock removido" -ForegroundColor Green
} else {
    Write-Host "  ✓ Nenhum lock encontrado" -ForegroundColor Green
}

# 3. Verificar status
Write-Host "[3/6] Verificando arquivos modificados..." -ForegroundColor Yellow
$status = git status --short
if ($status) {
    Write-Host "  Arquivos modificados:" -ForegroundColor Cyan
    $status | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
} else {
    Write-Host "  ⚠ Nenhum arquivo modificado encontrado!" -ForegroundColor Yellow
    Write-Host "  Isso pode significar que já foi commitado ou não há mudanças." -ForegroundColor Yellow
}

# 4. Adicionar arquivos específicos
Write-Host "[4/6] Adicionando arquivos ao staging..." -ForegroundColor Yellow
$files = @(
    "app/routers/books.py",
    "app/static/index.html",
    "app/services/youtube_service.py",
    "app/services/ai_generator.py",
    "app/routers/youtube.py",
    "app/services/video_generator.py",
    "app/services/video_processing.py",
    "app/services/monitor_service.py",
    "app/main.py"
)

foreach ($file in $files) {
    if (Test-Path $file) {
        git add $file
        Write-Host "  ✓ $file" -ForegroundColor Green
    } else {
        Write-Host "  ⚠ $file não encontrado" -ForegroundColor Yellow
    }
}

# 5. Criar commit
Write-Host "[5/6] Criando commit..." -ForegroundColor Yellow
$commitMsg = "feat: YouTube Auto - Auto Análise + Monetização com IA; Download de livros com regeneração; Fix vídeos 95%"
git commit -m $commitMsg
if ($LASTEXITCODE -eq 0) {
    Write-Host "  ✓ Commit criado com sucesso" -ForegroundColor Green
} else {
    Write-Host "  ⚠ Erro ao criar commit (pode ser que não haja mudanças para commitar)" -ForegroundColor Yellow
}

# 6. Push para GitHub
Write-Host "[6/6] Enviando para GitHub (Render fará deploy automático)..." -ForegroundColor Yellow
git push origin main
if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "✓✓✓ DEPLOY INICIADO COM SUCESSO! ✓✓✓" -ForegroundColor Green
    Write-Host ""
    Write-Host "Próximos passos:" -ForegroundColor Cyan
    Write-Host "1. Acesse: https://dashboard.render.com" -ForegroundColor White
    Write-Host "2. Vá em seu serviço Codexia" -ForegroundColor White
    Write-Host "3. Aba 'Events' ou 'Logs' - aguarde o deploy terminar" -ForegroundColor White
    Write-Host "4. Após deploy, atualize a página do app (Ctrl+F5)" -ForegroundColor White
} else {
    Write-Host ""
    Write-Host "✗✗✗ ERRO NO PUSH ✗✗✗" -ForegroundColor Red
    Write-Host ""
    Write-Host "Possíveis causas:" -ForegroundColor Yellow
    Write-Host "- Problema de rede/conexão com GitHub" -ForegroundColor White
    Write-Host "- Credenciais Git não configuradas" -ForegroundColor White
    Write-Host "- Branch 'main' não existe ou está protegida" -ForegroundColor White
    Write-Host ""
    Write-Host "Execute manualmente:" -ForegroundColor Cyan
    Write-Host "  git push origin main" -ForegroundColor White
}

Write-Host ""
Write-Host "=== FIM DO SCRIPT ===" -ForegroundColor Cyan
