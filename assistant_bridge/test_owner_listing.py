"""Regression: another account's newest 500 projects cannot hide mine."""
import json
import sqlite3


def test_owner_filter_runs_before_limit():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE assistant_projects(id TEXT, status TEXT, updated_at TEXT, payload TEXT)")
    db.execute("INSERT INTO assistant_projects VALUES (?, ?, ?, ?)",
               ("my-old-project", "awaiting_review", "2026-01-01",
                json.dumps({"title": "Mine", "_production": {"user_id": 11}})))
    for i in range(550):
        db.execute("INSERT INTO assistant_projects VALUES (?, ?, ?, ?)",
                   (str(i), "awaiting_review", "2026-10-24",
                    json.dumps({"_production": {"user_id": 22}})))
    rows = db.execute(
        """SELECT id FROM assistant_projects
           WHERE json_type(payload, '$._production.user_id') = 'integer'
             AND json_extract(payload, '$._production.user_id') = ?
           ORDER BY updated_at DESC LIMIT 500""", (11,)
    ).fetchall()
    assert rows == [("my-old-project",)]
    assert db.execute(
        """SELECT COUNT(*) FROM assistant_projects
           WHERE json_type(payload, '$._production.user_id') = 'integer'
             AND json_extract(payload, '$._production.user_id') = ?""", (22,)
    ).fetchone()[0] == 550
