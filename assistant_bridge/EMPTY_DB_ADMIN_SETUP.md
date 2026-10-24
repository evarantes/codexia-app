# Inicializar administrador pelo celular (somente banco vazio)

Esta rotina usa a branch de testes e **não importa usuários nem senhas do Codexia principal**.

1. No Coolify, abra a **aplicação de testes** (não o recurso PostgreSQL) e vá a **Environment Variables > Production Environment Variables**.
2. Crie `BOOTSTRAP_ADMIN_EMAIL` com o e-mail desejado.
3. Crie `BOOTSTRAP_ADMIN_PASSWORD` com **uma senha nova de pelo menos 16 caracteres**, diferente da senha do Codexia principal. Marque **Available at Runtime** e não marque **Available at Buildtime**. Não envie o valor ao chat.
4. Clique em salvar e execute um **Deploy** da branch atualizada. Verifique se o commit implantado contém `a7463746` ou posterior (um deploy do commit antigo não inclui o recurso).
5. Entre no painel `/api/assistant-production/dashboard` com essas credenciais. O login retorna `must_change_password=true`; troque a senha pelo fluxo autenticado `POST /auth/change-password` antes de usar a conta. Se o painel não oferecer essa opção, será necessária uma interface de troca de senha.
6. **Remova as duas variáveis de bootstrap do Coolify** e faça novo deploy após o primeiro login. Nunca deixe a senha inicial armazenada nas configurações.

Segurança:
- A rotina só cria um usuário se a tabela `users` estiver vazia. Nunca altera senhas ou permissões de usuários existentes.
- Em PostgreSQL, uma trava transacional impede duas inicializações simultâneas de criarem o primeiro usuário.
- Se já houver usuários, o bootstrap é ignorado. Nesse caso é necessário um fluxo de recuperação de conta, não uma redefinição silenciosa.
- Atenção: o endpoint de painel deve estar protegido por HTTPS antes de digitar qualquer senha. Não use HTTP para login.
- Não configure `ADMIN_EMAIL`/`ADMIN_PASSWORD` para esta operação: o mecanismo legado pode redefinir senhas a cada reinício.
