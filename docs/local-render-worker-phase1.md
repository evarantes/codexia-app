# Codexia Local Render Worker — Fase 1

## Objetivo

Executar **somente render final local por FFmpeg** em um PC Windows autorizado, reaproveitando roteiro, narração, legendas e imagens já preservados pelo Codexia.

Esta fase não publica vídeos, não gera imagens, não chama TTS, não consulta provedores de música e não acessa PostgreSQL/Redis diretamente a partir do Windows.

## Modelo de segurança

- O agente Windows faz apenas conexões **outbound HTTPS** para a API Codexia.
- Nenhuma porta TCP é aberta no Windows pelo agente.
- A autenticação usa `CODEXIA_LOCAL_WORKER_TOKEN`, separado de login/admin/JWT comum.
- O servidor só entrega uma tarefa quando o payload contém simultaneamente:
  - `local_render_worker_allowed=true`
  - `force_render_only=true`
  - `force_reuse_assets=true`
  - `auto_upload=false`
- O agente recebe apenas URLs opacas por índice para os ativos pertencentes à tarefa alugada; ele não envia caminhos de arquivos ao servidor.
- O download de ativo exige token do worker **e** `X-Worker-Id` correspondente ao lease ativo.
- O lease usa `video_task_leases`/`task_manager` já existente e heartbeat periódico; dois executores não podem possuir simultaneamente o mesmo lease válido.
- O upload final só é aceito do mesmo `worker_id` que possui o lease.
- O MP4 é concluído no Codexia como `completed` e fica aguardando revisão/publicação manual.

## Inventário e limites

O heartbeat informa hostname, SO, Python, CPU lógica, RAM quando `psutil` estiver disponível, disco livre/total, GPUs Windows e presença de `h264_qsv` no FFmpeg.

O agente aplica:

- 1 vídeo simultâneo por processo;
- FFmpeg limitado a no máximo 2 threads;
- bloqueio quando RAM já excede o limite configurado (padrão 85%);
- bloqueio quando o disco livre fica abaixo do mínimo (padrão 8 GiB);
- diretório temporário por `task_id` e limpeza no `finally`;
- SHA-256 e tamanho validados em cada download.

## Configuração futura no Windows

Instalar FFmpeg/ffprobe no `PATH` e, opcionalmente, `psutil`. Configurar apenas no computador local:

```powershell
$env:CODEXIA_LOCAL_WORKER_BASE_URL="https://SEU-CODEXIA"
$env:CODEXIA_LOCAL_WORKER_TOKEN="TOKEN-ALEATORIO-LONGO-EXCLUSIVO"
$env:CODEXIA_LOCAL_WORKER_ID="pc-render-01"
python -m local_worker.agent
```

O mesmo token precisa existir no ambiente da API como `CODEXIA_LOCAL_WORKER_TOKEN`. Não reutilizar `SECRET_KEY`, senha de administrador ou chave de IA.

## Política de mídia/custo

O agente não contém integração com OpenAI, Gemini, ElevenLabs, Suno ou upload do YouTube. O manifesto retornado pelo servidor declara explicitamente:

- `paid_media_calls_allowed=false`
- `regenerate_images=false`
- `regenerate_tts=false`
- `publish=false`
- `preserve_full_text=true`
- `preserve_full_narration=true`

## Critérios antes de produção

1. CI completo verde.
2. Revisão do PR sem merge automático.
3. Gerar token exclusivo do worker e configurar apenas quando autorizado.
4. Validar heartbeat do PC real e inventário QSV.
5. Criar uma tarefa de homologação explicitamente opt-in.
6. Confirmar lease único e download somente dos ativos daquela tarefa.
7. Confirmar render com `h264_qsv` e fallback `libx264`.
8. Confirmar upload do MP4 e limpeza dos temporários.
9. Confirmar zero chamadas pagas e zero publicação.
10. Só então decidir sobre merge/deploy em uma execução separada.
