# Debug Session: video-duration-mismatch
- **Status**: [OPEN]
- **Issue**: O sistema não respeita a duração de vídeo solicitada pelo usuário. Exemplo observado: pedido de 8 minutos resultando em vídeo final com cerca de 2 minutos.
- **Expected**: A duração final do vídeo deve ficar próxima da duração solicitada, com tolerância pequena.
- **Actual**: A duração final publicada/gerada fica muito abaixo do tempo solicitado.
- **Scope**: Fluxos de geração em `YouTube Auto`, incluindo produção a partir de sugestões e vídeos narrados.

## Hipóteses Iniciais
1. **H1 - A duração solicitada não chega íntegra ao backend**  
   O frontend envia um valor, mas o payload final salvo/processado chega diferente ou vazio.
2. **H2 - O backend recebe a duração, mas o plano final perde `target_duration_sec` antes do render**  
   O ajuste existe em código, porém o campo não está presente no `script` efetivamente entregue ao `create_video_from_plan`.
3. **H3 - O render final ignora o ajuste porque o áudio/narração fica curto e a etapa de compensação não executa**  
   A duração do `final_clip` ou do áudio impede o padding/freeze esperado no pós-processamento.
4. **H4 - O fluxo específico de “Sugestões em alta” ou outro atalho usa um caminho alternativo que contorna a lógica principal**  
   Alguns botões/rotas podem montar payload/plano diferente do fluxo principal.
5. **H5 - O vídeo final respeita o plano internamente, mas a URL/arquivo exibido na UI aponta para outro artefato curto**  
   O arquivo mostrado ao usuário não é o mesmo arquivo final que passou pelo ajuste de duração.

## Evidência Necessária
- Payload recebido em `/youtube/generate_video`
- Duração solicitada normalizada no backend
- Presença/ausência de `target_duration_sec` no plano final
- Duração do áudio principal, do `final_clip` antes e depois do ajuste final
- Caminho do arquivo final e duração real do MP4 gerado

## Próximos Passos
1. Adicionar instrumentação mínima no backend para registrar payload, plano e duração real do arquivo final.
2. Reproduzir o caso com uma solicitação de duração conhecida.
3. Analisar logs e confirmar/rejeitar hipóteses.
4. Aplicar correção mínima baseada em evidência.
