# Codexia V2 — auditoria e foco de produto

## Objetivo principal

Por 30 dias, o Codexia passa a otimizar **horas qualificadas, retenção, CTR, inscritos e R$/hora assistida** para o canal principal. Quantidade de módulos e quantidade bruta de vídeos deixam de ser métricas de sucesso.

## Mantido na navegação principal

- Meta Monetização — 30 dias.
- Produção Cinematográfica Inteligente.
- Devocionais.
- Shorts derivados.
- **Música e Clipe** — preservado integralmente, inclusive biblioteca e louvores existentes.
- Fila & Custos.
- Configurações.

## Retirado da navegação principal (não apagado)

Os módulos abaixo ficam arquivados no sistema anterior durante a campanha. Código, banco e arquivos não são excluídos nesta mudança para evitar perda de dados e permitir rollback:

- Dashboard antigo.
- Meus Livros.
- Campanhas & IA genérica.
- Criar Vídeos genérico.
- YouTube Auto antigo como tela principal.
- Bancada de Narração como produto isolado.
- Fábrica de Conteúdo genérica.
- Hotmart Auto.
- Fábrica de Livros.
- AI Factory.
- Fábrica de Humor.
- Fábrica de Vídeos Bíblicos antiga como tela separada.
- CRM.
- Roteamento IA como tela de uso diário.

Esses recursos continuam acessíveis em `/static/legacy/index.html` para conferência/rollback, mas não competem pela atenção no Codexia V2.

## Arquitetura V2

1. Claude Sonnet 5 = diretor: roteiro, hook, continuidade, cenas, revisão e aplicação devocional.
2. Cenas A = Kling/Veo para clímax e momentos de alto impacto.
3. Cenas B = Runway/Veo para movimento útil e econômico.
4. Cenas C = imagem cinematográfica + movimentos de câmera/parallax/partículas no renderizador.
5. Histórias: alvo de 20–30% de vídeo generativo.
6. Devocionais: alvo de 5–10%.
7. Shorts: reutilizam cenas já pagas e funcionam como funil para vídeos longos.
8. Teto de custo antes de qualquer geração e revisão antes de pagar mídia.

## Segurança de migração

A interface anterior foi preservada como cópia estática. Esta etapa **não exclui tabelas, louvores, vídeos, livros nem histórico**. A remoção física de módulos somente deve ocorrer depois de 30 dias de métricas e backup validado.

## Variáveis novas esperadas

- `ANTHROPIC_API_KEY` (opcional se a chave já estiver salva em Settings).
- `CLAUDE_DIRECTOR_MODEL=claude-sonnet-5`.
- `RUNWAYML_API_SECRET` para Runway.
- `FAL_KEY` para Kling via fal.ai.
- `GEMINI_API_KEY` para Veo (ou chave já salva em Settings).
- `CODEXIA_USD_BRL` para a conversão usada nas estimativas.

Nenhum endpoint de status faz geração paga. Créditos são consumidos somente em ações explícitas de direção/geração.
