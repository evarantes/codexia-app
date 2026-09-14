# Checklist de reteste — Claude Diretor

1. Confirmar deploy do commit da correção no ambiente Codexia.
2. Abrir Produção Cinematográfica.
3. Tema: `Davi e Golias — enfrentando o impossível`.
4. Tipo: História bíblica + aplicação.
5. Duração: 10 minutos.
6. Teto: R$ 85.
7. Acionar apenas `Claude: criar direção`.
8. Não gerar imagens/clipes durante este teste.
9. Confirmar que o plano retorna títulos, gancho, roteiro e cenas A/B/C.
10. Confirmar no retorno que o custo da direção é exibido e que `motion_planned_seconds <= motion_budget_seconds`.
11. Se houver falha, registrar a nova mensagem completa; agora ela inclui stop/finish reason para diagnóstico.
