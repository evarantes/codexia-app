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
