# PR #271 — controlled release checklist

**Do not mark ready or deploy solely because isolated CI is green.**

## Automated (no credits)
- [ ] Assistant bridge CI green on final commit.
- [ ] Video pipeline CI green on final commit.
- [ ] Test authenticated endpoints with two users: create/list/read/approve/cancel; cross-account requests denied.
- [ ] Test concurrent start-music requests; exactly one job is submitted.
- [ ] Test failed provider submission and recovery without duplicate charges.
- [ ] Test dashboard login, mandatory password change, logout and expired JWT.

## Staging (requires operator)
- [ ] Confirm SQLite path is on persistent shared storage or migrate to PostgreSQL before scaling across app replicas.
- [ ] Confirm Suno key availability; set per-user quota and cost ceiling.
- [ ] Confirm task state survives process restart and worker handoff.
- [ ] Test music task result shape and saved filename using a sandbox/mock first.
- [ ] Test clip renderer consumes the saved music file and produces a reviewable artifact.
- [ ] Verify no YouTube auto-upload or other publishing.
- [ ] Configure rate limiting, audit trail, backups and incident rollback.
- [ ] Verify ChatGPT connector is configured with individual account consent; a conversation alone cannot dispatch projects.

## Rollout
- [ ] Operator approves a limited staging trial with paid calls explicitly authorized.
- [ ] Complete one controlled end-to-end production and review costs/output.
- [ ] Merge, deploy and monitor only after gates above pass.
