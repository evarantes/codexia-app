# Confirmação de duração após revisão editorial

Quando a previsão inicial está dentro da faixa solicitada, mas a revisão editorial posterior aumenta o roteiro além da tolerância, a primeira tentativa é interrompida antes de mídia paga.

Nesse estado recuperável, a interface apresenta **Continuar assim mesmo**. O clique exibe uma confirmação humana e reutiliza a mesma tarefa. O retry marca `force_reuse_assets`, que o guard de duração reconhece como autorização explícita para aquele desvio, mantendo o relatório de auditoria com `approval_source=retry_after_duration_warning`.

A ação não cria uma nova produção e não desativa a proteção de duração para tentativas iniciais sem confirmação.
