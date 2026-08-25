web: python scripts/apply_migrations.py && python scripts/apply_local_render_worker_phase1.py --apply --check && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
