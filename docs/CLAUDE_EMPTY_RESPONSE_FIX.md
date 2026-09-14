# Claude Sonnet 5 — correção de resposta vazia

Sintoma observado em produção: ao acionar `Claude: criar direção`, a API respondia HTTP 200, porém o Codexia não encontrava bloco de texto e exibida a mensagem `Claude retornou uma resposta vazia`.

## Causa técnica

Claude Sonnet 5 ativa adaptive thinking por padrão quando `thinking` é omitido. O diretor cinematográfico solicita uma saída JSON extensa (roteiro completo + ~30 cenas + prompts + Shorts) e usava `max_tokens=18000`. Em cargas longas, o orçamento total pode ser consumido por thinking + saída, deixando pouco ou nenhum bloco `text`.

## Correção

- Desabilitar thinking nessa tarefa determinística de geração de JSON (`thinking.type=disabled`).
- Definir `output_config.effort=medium`.
- Aumentar o teto padrão de saída para 36k tokens, configurável por `CLAUDE_DIRECTOR_MAX_TOKENS` e limitado a 64k.
- Extrair blocos `text` de forma robusta.
- Quando não houver texto, informar `stop_reason`/`finish_reason` em vez de erro genérico.
- No fallback OpenRouter, solicitar `response_format=json_object`, esforço de reasoning baixo e aceitar respostas em formato de partes tipadas.

Nenhuma mídia paga é gerada por esta correção; o gasto só ocorre quando o usuário aciona o diretor ou provedores de vídeo explicitamente.
