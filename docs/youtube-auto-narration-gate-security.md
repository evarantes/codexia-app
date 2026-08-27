# Limites de segurança

- O preview gera somente áudio completo.
- Não chama gerador de imagens.
- Não renderiza MP4.
- Não entra na fila de vídeo.
- O texto passa por `validate_narration_text` antes do TTS e falha fechado se houver código/estrutura técnica.
- A aprovação é amarrada ao hash do texto; texto alterado exige novo áudio.
- A geração supervisionada usa Edge TTS nesta fase e reaproveita preview idêntico por fingerprint determinístico.
- O vídeo recebe o MP3 aprovado por `reuse_audio_from` para não executar TTS novamente.
