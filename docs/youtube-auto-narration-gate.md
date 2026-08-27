# YouTube Auto — supervisão da narração

Fluxo novo do História/Devocional:

1. Gere ou cole o texto.
2. Escolha **Gerar vídeo narrado** para manter o fluxo automático, protegido pelo contrato canônico de narração.
3. Ou escolha **Gerar primeiro o áudio da narração** para criar somente o MP3 completo com Edge TTS, sem imagens, MP4 ou fila pesada.
4. Ouça o player e confira o **texto exato enviado ao TTS**.
5. Clique em **Aprovar esta narração**.
6. Clique em **Avançar para geração do vídeo com este áudio**.

A aprovação é vinculada ao SHA-256 do texto. Qualquer alteração posterior invalida a aprovação. Quando o vídeo é iniciado com a aprovação válida, o frontend acrescenta `reuse_audio_from` ao payload canônico `/youtube/generate_video`, reutilizando o MP3 preservado e evitando uma nova geração TTS.

A prévia desta primeira versão usa Edge TTS gratuito para que a supervisão de áudio não consuma créditos de TTS pago.
