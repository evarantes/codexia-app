# Debug Session: prayer-ai-failure
- **Status**: [OPEN]
- **Issue**: Ao usar o tipo `Reflexão com oração`, a geração de texto retorna `Reflexão com Oração (Falha na IA)` em vez do texto narrado esperado.
- **Expected**: A IA deve gerar um texto longo, suave e coerente para reflexão com oração.
- **Actual**: O sistema cai no fallback de erro e devolve apenas o prompt original com o rótulo `Falha na IA`.
- **Scope**: Fluxo `POST /youtube/story/generate_text` e, potencialmente, `improve_text` para `kind=prayer`.

## Hipóteses Iniciais
1. **H1 - O backend aceita `prayer` na UI, mas algum ponto do pipeline de texto ainda não trata esse tipo corretamente**  
   O request chega com `kind=prayer`, porém uma validação/ramificação downstream dispara exceção.
2. **H2 - O provider de texto está falhando por conta do prompt novo de oração**  
   A chamada à IA está levantando erro de provider/modelo, tamanho, política ou formato.
3. **H3 - A geração funciona para `story/devotional`, mas falha apenas com parâmetros do modo oração**  
   O problema está em algum conteúdo específico do prompt, duração ou instrução automática aplicada a `prayer`.
4. **H4 - O erro ocorre antes do provider responder, dentro de `generate_story_or_devotional_text(...)`**  
   Alguma montagem de prompt, normalização ou escolha de título/role está quebrando o fluxo.
5. **H5 - O frontend envia o payload correto, mas o backend recebe dados diferentes do esperado**  
   O `kind`, a duração ou a instrução podem estar sendo serializados de forma incorreta no request final.

## Evidência Necessária
- Payload recebido no endpoint `/youtube/story/generate_text`
- `kind`, duração e instrução normalizados no backend
- Estado de `_has_text_provider()`
- Ponto exato da exceção e mensagem real do erro
- Se a falha ocorre também em `improve_text`

## Próximos Passos
1. Instrumentar rota e serviço de geração de texto.
2. Reproduzir com `kind=prayer`.
3. Ler os logs coletados.
4. Corrigir a causa mínima com base na evidência.
