# Debug Session: render-stuck-86

- Status: OPEN
- Started at: 2026-06-18
- Symptom: Producao de video fica em 86% por muito tempo, mesmo apos atualizar a pagina.
- Scope: YouTube Auto / render final de video narrado

## Hypotheses

1. O render final continua executando, mas o progresso nao avanca na UI porque o logger de `write_videofile` nao esta reportando progresso.
2. O `write_videofile` travou de fato no ffmpeg/moviepy por arquivo de audio/video/imagem corrompido ou combinacao invalida de clips.
3. O processo terminou ou morreu, mas o status persistido no banco ficou parado em `RENDER`, mantendo fallback visual em 86%.
4. O monitor/worker nao esta atualizando o job durante o render final, entao a tarefa parece travada mesmo sem erro visivel.
5. O container entrou em gargalo de CPU/RAM/disco durante a etapa final e o ffmpeg ficou extremamente lento ou bloqueado.

## Evidence Plan

- Instrumentar inicio, heartbeat e fim de `write_videofile`.
- Instrumentar persistencia de progresso/status do job espelhado para diferenciar render lento de status congelado.
- Reproduzir uma geracao e coletar logs pre-fix.

## Notes

- Nao alterar logica de negocio antes da evidencia runtime.
- Limpeza da instrumentacao somente apos confirmacao do usuario.

## Evidence Update

- Evidence source: diagnostico da UI em producao.
- Observacao: `USE_RQ_FOR_VIDEO_GENERATION está ativo, mas não há workers RQ.`

## Hypothesis Status

1. Logger de progresso nao chega na UI durante o render.
   - Status: CONFIRMED
   - Evidence: o `RenderProgressLogger` era criado em `video_generator.py`, mas o codigo sobrescrevia `logger_kw` com `{"logger": None}` imediatamente antes do `write_videofile`, anulando o progresso fino do render e congelando a UI em ~86%.
2. `write_videofile` travou de fato.
   - Status: INCONCLUSIVE
   - Evidence: ainda depende dos heartbeats e/ou excecao de runtime.
3. Status persistido congelou em `RENDER`.
   - Status: INCONCLUSIVE
   - Evidence: ainda depende dos logs instrumentados em `update_task`.
4. Ambiente de fila RQ esta mal configurado.
   - Status: CONFIRMED
   - Evidence: diagnostico do sistema reporta `USE_RQ_FOR_VIDEO_GENERATION` ativo sem worker.
5. Gargalo de CPU/RAM/disco no ffmpeg.
   - Status: INCONCLUSIVE
   - Evidence: sem metricas runtime suficientes ainda.

## Fix Applied

- Restaurado o logger customizado do `write_videofile` em `app/services/video_generator.py`.
- Mantido heartbeat leve durante o render final para evitar tela completamente congelada enquanto o arquivo final cresce.
