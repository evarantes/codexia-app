# Script rápido para fazer deploy direto para GitHub (Render faz deploy automático)
# Uso: .\DEPLOY_RAPIDO.ps1 "sua mensagem de commit"

param(
    [Parameter(Mandatory=$false)]
    [string]$mensagem = "feat: atualização automática"
)

Write-Host "=== DEPLOY RÁPIDO PARA GITHUB ===" -ForegroundColor Cyan
Write-Host ""

Set-Location "c:\dev\TRAE\codexia"

# Remover lock se existir
if (Test-Path ".git\index.lock") {
    Remove-Item -Force ".git\index.lock" -ErrorAction SilentlyContinue
}

# Adicionar todas as mudanças
Write-Host "Adicionando arquivos..." -ForegroundColor Yellow
git add .

# Verificar se há mudanças
$status = git status --short
if (-not $status) {
    Write-Host "⚠ Nenhuma mudança para commitar!" -ForegroundColor Yellow
    exit 0
}

# Commit
Write-Host "Criando commit..." -ForegroundColor Yellow
git commit -m $mensagem

if ($LASTEXITCODE -ne 0) {
    Write-Host "✗ Erro ao criar commit" -ForegroundColor Red
    exit 1
}

# Push
Write-Host "Enviando para GitHub..." -ForegroundColor Yellow
git push origin main

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "✓✓✓ PUSH REALIZADO COM SUCESSO! ✓✓✓" -ForegroundColor Green
    Write-Host ""
    Write-Host "O Render vai detectar automaticamente e fazer o deploy em alguns minutos." -ForegroundColor Cyan
    Write-Host "Acompanhe em: https://dashboard.render.com" -ForegroundColor White
} else {
    Write-Host ""
    Write-Host "✗✗✗ ERRO NO PUSH ✗✗✗" -ForegroundColor Red
    Write-Host "Verifique sua conexão e credenciais Git." -ForegroundColor Yellow
    exit 1
}
