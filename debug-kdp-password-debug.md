# Debug Session: kdp-password-debug
- **Status**: [OPEN]
- **Issue**: Automacao KDP falha ao preencher o campo de senha no login da Amazon.
- **Debug Server**: http://127.0.0.1:7777/event
- **Log File**: .dbg/trae-debug-log-kdp-password-debug.ndjson

## Reproduction Steps
1. Abrir `Configuracoes` no Codexia.
2. Preencher credenciais Amazon KDP.
3. Clicar em `Testar conexao KDP`.
4. Observar erro ao preencher `password`.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | O campo de senha usa seletor diferente do esperado. | High | Low | Pending |
| B | A pagina ainda nao terminou de carregar quando a senha e preenchida. | High | Low | Pending |
| C | A Amazon esta usando um fluxo alternativo apos o e-mail. | Med | Med | Pending |
| D | O botao `Continuar` encontrado nao e o correto nessa tela. | Med | Low | Pending |
| E | Faltam evidencias de URL/titulo/seletores no ponto da falha. | High | Low | Pending |

## Log Evidence
- Instrumentation added in `_login_kdp` to capture URL, title, password selector counts, and continue button counts.
- Production-facing snapshots will be saved under `generated_assets/distribution_logs/<task>/debug_*.json`.

## Verification Conclusion
- Pending.
