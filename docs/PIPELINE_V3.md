# Pipeline V3 e ambiente de testes

## Regras

- O pipeline legado permanece congelado e somente leitura.
- O V3 não reutiliza as tabelas, filas ou diretórios do legado durante a fundação.
- O ambiente de testes usa `CODEXIA_PIPELINE_ENV=test` e `/data/pipeline_test`.
- A produção V3 usa `CODEXIA_PIPELINE_ENV=production` e `/data/pipeline_v3`.
- A promoção para produção ocorrerá somente depois dos testes automatizados e do smoke test audiovisual.
- Falhas no ambiente de testes não interrompem a produção V3.
- Não existe retorno operacional ao pipeline legado.

## Duração

O contrato aceita segundos, minutos e `HH:MM:SS`. Exemplos:

- `10s`
- `30s`
- `1m`
- `2m`
- `8m`
- `15m`
- `00:01:30`

## Próxima etapa

A fundação ainda não conecta rotas existentes ao V3. A integração será feita em uma etapa separada, atrás de uma chave explícita, depois que a máquina de estados e o isolamento forem validados.
