# Script para IMPLANTAR as atualizações no Render
# Execute no PowerShell: clique direito neste arquivo -> "Executar com PowerShell"
# OU abra o PowerShell, vá na pasta do projeto e rode: .\IMPLANTAR_ATUALIZACOES.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== IMPLANTAR ATUALIZACOES (GITHUB / COOLIFY / RENDER) ===" -ForegroundColor Cyan
Write-Host ""

# Remover lock do Git se existir
if (Test-Path ".git\index.lock") {
    Write-Host "Removendo .git\index.lock..." -ForegroundColor Yellow
    Remove-Item -Force ".git\index.lock" -ErrorAction SilentlyContinue
}

Write-Host "1. Adicionando todos os arquivos..." -ForegroundColor Yellow
git add -A
if ($LASTEXITCODE -ne 0) { Write-Host "ERRO no git add. Feche o Cursor e rode este script de novo no PowerShell." -ForegroundColor Red; exit 1 }

Write-Host "2. Criando commit..." -ForegroundColor Yellow
git commit -m "fix: compatibility improvements for Coolify/Render; logging tweaks"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Nenhuma alteracao para commitar (ou commit ja feito). Tentando push..." -ForegroundColor Yellow
}

Write-Host "3. Enviando para o GitHub (Dispara Deploy no Coolify e Render)..." -ForegroundColor Yellow
git push origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERRO no push. Verifique: (1) Internet, (2) Login no GitHub, (3) Permissoes do repo." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "OK! Atualizacoes enviadas para https://github.com/evarantes/codexia-app" -ForegroundColor Green
Write-Host "O deploy deve iniciar automaticamente no Coolify e Render." -ForegroundColor Green
Write-Host ""
Write-Host "Proximos passos:" -ForegroundColor Cyan
Write-Host "  1. Verifique os logs no seu painel do Coolify ou Render" -ForegroundColor White
Write-Host "  2. Aguarde o deploy terminar (alguns minutos)" -ForegroundColor White
Write-Host "  3. Teste na URL do seu novo ambiente" -ForegroundColor White
Write-Host ""
