# CODEXIA → SaaS: Diagnóstico e Plano de Execução

## ETAPA 0 — Diagnóstico

### Estrutura de pastas
```
codexia/
├── app/
│   ├── main.py              # FastAPI app, lifespan, rotas raiz
│   ├── database.py          # Engine, SessionLocal, get_db, DATABASE_URL
│   ├── models.py            # SQLAlchemy models
│   ├── config.py            # BASE_URL, paths estáticos
│   ├── routers/             # 13 routers
│   ├── services/            # 12 serviços (IA, vídeo, YouTube, etc.)
│   └── static/              # index.html (Vue), login, reset-password
├── alembic/                 # env.py, script.py.mako (sem versions/)
├── alembic.ini
├── Dockerfile
├── COOLIFY.md
└── requirements.txt
```

### Principais módulos FastAPI
| Módulo | Responsabilidade |
|--------|------------------|
| `app.main` | App FastAPI, lifespan, CORS, exception handler, mount /static |
| `app.database` | SQLAlchemy engine (SQLite/Postgres), SessionLocal, get_db |
| `app.models` | Book, BookDraft, Post, Lead, Settings, Customer, Sale, ScheduledVideo, User, ChannelReport |

### Rotas (routers)
| Router | Prefixo | Auth | Observação |
|--------|---------|------|------------|
| auth | (root) | parcial | /token, /auth/me, /auth/change-password |
| admin | /admin | admin | /admin/backups, /admin/backups/{filename} |
| books | (root) | não | list_books, delete_book, etc. |
| marketing | (root) | não | generate_ad |
| settings | (root) | não | get_settings, update_settings |
| video | (root) | não | generate_auto_video |
| crm | (root) | não | get_customers, get_sales |
| youtube | (root) | não | get_schedule, save_schedule, etc. |
| webhook | (root) | não | mercadopago_webhook |
| diagnostics | (root) | não | run_diagnostics |
| book_factory | (root) | não | save_draft, list_drafts, etc. |
| hotmart | (root) | não | list_hotmart_products, etc. |
| music | (root) | sim | get_current_user |

**Conclusão:** A maioria das rotas **não exige autenticação**. Apenas music e admin usam Depends(get_current_user/admin).

### Banco de dados atual (SQLite)
| Tabela | Colunas principais | user_id |
|--------|-------------------|---------|
| users | id, email, hashed_password, is_admin, name, is_active, must_change_password | — |
| books | id, title, author, user_id, ... | sim |
| book_drafts | id, title, user_id, ... | sim |
| posts | id, book_id, content, ... | via book |
| leads | id, user_id, ... | sim |
| settings | id, user_id, ... | sim |
| customers | id, user_id, ... | sim |
| sales | id, customer_id, book_id | via customer |
| scheduled_videos | id, user_id, status, ... | sim |
| channel_reports | id, user_id, ... | sim |

### Serviços pesados
| Serviço | Uso | Onde roda |
|---------|-----|-----------|
| video_processing.process_scheduled_video | MoviePy, TTS, IA | APScheduler (1/min) no mesmo processo |
| video_generator | Render de vídeo | Chamado por process_scheduled_video |
| monitor_service | Canal YouTube, uploads, backup | APScheduler background |
| ai_generator | OpenAI/Gemini para scripts | Diversos routers |

### Como o painel chama a API
- **Login:** `POST /token` (form username/password)
- **Auth check:** `GET /auth/me` (Bearer token)
- **Fetch genérico:** `authFetch(url)` com header `Authorization: Bearer ${token}`
- **Rotas relativas:** `/youtube/...`, `/books/...`, etc. (sem prefixo /api em várias)

### Database
- **Padrão:** `sqlite:////data/vibraface.db`
- **Postgres:** `DATABASE_URL` (postgres:// ou postgresql://)
- **Migração legado:** Se `/app/vibraface.db` existe e `/data/vibraface.db` não → `shutil.copy2`
- **Alembic:** Configurado (env.py) mas sem revisions em `alembic/versions/`
- **Migrações atuais:** Inline em `run_migrations()` (main.py) com `inspector` + `ALTER TABLE`

---

## Plano de Execução

### ETAPA 1 — Fundação do banco + migração segura ✓
- [x] DATABASE_URL + fallback SQLite em /data (já existe)
- [x] Cópia /app → /data na primeira execução (já existe)
- [x] Adicionar Tenant model + tenant_id em User
- [x] Migração: tenants table, user.tenant_id, tenant "Default"
- [x] Bootstrap: create_admin_master cria/usa tenant Default

### ETAPA 2 — Multi-tenant + RBAC ✓
- [x] Tabela tenants + tenant_id em User (ETAPA 1)
- [x] JWT com tenant_id, role
- [x] Coluna role (admin, cliente, colaborador)
- [ ] tenant_id em tabelas principais (próximo)
- [ ] dependency current_tenant + filtro por tenant_id

### ETAPA 3 — Planos e assinaturas
- [ ] plans, subscriptions
- [ ] Feature gating (check_feature)
- [ ] Endpoints /api/billing/*

### ETAPA 4 — Fila de jobs + worker
- [ ] Tabela jobs
- [ ] worker.py separado
- [ ] Integrar vídeo/upload no worker

### ETAPA 5 — Observabilidade
- [ ] logs, audit_events
- [ ] Middleware + endpoints admin

### ETAPA 6 — Storage /data/media + limpeza
- [ ] TEMP_DIR, MEDIA_DIR, EXPORT_DIR
- [ ] Rotina de limpeza

### ETAPA 7 — Segurança
- [ ] Rate limit
- [ ] CORS (já configurável)
- [ ] Bloqueio admin por role

### ETAPA 8 — Painel Vue
- [ ] Tenant/Plano, Fila, Logs

### ETAPA 9 — Testes + documentação
- [ ] Script smoke test
- [ ] COOLIFY.md atualizado

---

## Arquivos a alterar por etapa

### ETAPA 1 (este commit)
| Arquivo | Alteração |
|---------|-----------|
| app/models.py | + Tenant, User.tenant_id |
| app/main.py | run_migrations: tenants, tenant_id; create_admin_master: tenant Default |
| COOLIFY.md | + env vars ADMIN_EMAIL, ADMIN_PASSWORD, volumes |
