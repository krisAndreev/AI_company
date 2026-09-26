"""
Event log: every important action, with enough detail to understand *why*.

Written to two places:
  - the `events` table (for the dashboard and later analysis)
  - a human-readable log file, e.g. logs/company.log
"""

import json
from datetime import datetime
from pathlib import Path

from core.db import Database
from core.tasks import utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    project_id TEXT,
    task_id    TEXT,
    details    TEXT NOT NULL   -- JSON object
);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id);
"""


class EventLog:
    MAX_LOG_BYTES = 5_000_000
    KEEP_LOGS = 5

    def __init__(self, db: Database, log_path: str | Path | None = "logs/company.log"):
        self.db = db
        self.db.executescript(_SCHEMA)
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, actor: str, action: str, project_id: str | None = None,
               task_id: str | None = None, **details) -> dict:
        ts = utcnow()
        self.db.execute(
            "INSERT INTO events (ts, actor, action, project_id, task_id, details) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), actor, action, project_id, task_id, json.dumps(details, default=str)),
        )
        if self.log_path:
            self._write_line(ts, actor, action, project_id, task_id, details)
        return {"ts": ts, "actor": actor, "action": action, "project_id": project_id,
                "task_id": task_id, "details": details}

    def recent(self, limit: int = 50, project_id: str | None = None,
               actor: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if project_id:
            sql += " AND project_id = ?"
            args.append(project_id)
        if actor:
            sql += " AND actor = ?"
            args.append(actor)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = []
        for r in self.db.execute(sql, args):
            row = dict(r)
            row["details"] = json.loads(row["details"])
            rows.append(row)
        return rows

    def _write_line(self, ts: datetime, actor, action, project_id, task_id, details) -> None:
        parts = [ts.astimezone().strftime("%Y-%m-%d %H:%M:%S"), actor, action]
        if project_id:
            parts.append(f"project={project_id}")
        if task_id:
            parts.append(f"task={task_id}")
        parts += [f"{k}={v}" for k, v in details.items()]
        self._rotate()
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(" | ".join(str(p) for p in parts) + "\n")

    def _rotate(self) -> None:
        """Keep the readable log bounded: company.log -> .1 -> ... -> .5 at 5 MB."""
        try:
            if self.log_path.stat().st_size < self.MAX_LOG_BYTES:
                return
        except FileNotFoundError:
            return
        for i in range(self.KEEP_LOGS - 1, 0, -1):
            older = self.log_path.with_name(f"{self.log_path.name}.{i}")
            if older.exists():
                older.replace(self.log_path.with_name(f"{self.log_path.name}.{i + 1}"))
        try:
            self.log_path.replace(self.log_path.with_name(f"{self.log_path.name}.1"))
        except OSError:
            pass   # another process has it open; try again on the next line
