# Aplicação como arquivo central, worker como cache

Aplicar a mesma versão na API e no worker. Os volumes atuais não devem ser substituídos nem apagados. Na aplicação principal configurar:

```
CODEXIA_ASSET_ARCHIVE_ROLE=archive
CODEXIA_ASSET_TRANSFER_TOKEN=<segredo dedicado, pelo menos 32 caracteres aleatórios>
```

No worker configurar o mesmo segredo e a origem HTTPS da aplicação:

```
CODEXIA_ASSET_ARCHIVE_URL=https://g8w4so4gkkgog0scsw0ogwkw.89.167.1.253.sslip.io
CODEXIA_ASSET_TRANSFER_TOKEN=<mesmo segredo>
```

Não configurar ARCHIVE_URL na API. O token deve ser colocado diretamente no gerenciador de segredos/variáveis do Coolify; não em logs, Git ou conversa. Validar limites de corpo/timeouts do proxy para os maiores MP4s (limite padrão do receptor: 4 GiB por arquivo, ajustável por CODEXIA_ASSET_MAX_BYTES).

O worker recupera apenas ativos da tarefa, valida SHA256/tamanho, registra no manifesto local e remapeia referências de imagens/áudio antes da execução. Checkpoints do manifesto enviam os ativos produzidos, com confirmação de integridade. Ao terminar, uma confirmação final é exigida antes da limpeza. Falhas de transferência preservam o cache. Arquivos referenciados em manifestos de outras tarefas não são apagados. Metadados e roteiro local permanecem para auditoria; ativos legados de tarefas antigas não são removidos em massa.

O receptor é desabilitado sem ROLE=archive e token. Autenticação é dedicada, URLs não contêm segredos, downloads exigem vínculo no índice/manifesto da tarefa; uploads são atômicos, verificam SHA256 e recusam sobrescrever outro conteúdo. O diretório central mantém o nome original dos arquivos de mídia, compatível com as URLs do painel.

A recuperação de imagens acionada pelo usuário usa a contagem física acessível e completa a meta. Exemplo: 7 acessíveis para meta 48 exigem até 41 novas chamadas; referências ausentes não impedem a geração. Sem a opção explícita do pedido de correção, um orçamento já confirmado continua protegido.

Validação operacional: clicar uma vez em Corrigir ativo; observar 7/48 → 48/48; conferir imagens e MP4 na aplicação principal; somente após confirmação central conferir que o cache não compartilhado foi limpo. Não declarar a migração concluída apenas pelo CI.
