"""
Persistent task queue backed by SQLite.

All task SQL lives in this class, so moving to PostgreSQL later only touches this file.
Every status change goes through _transition(), which enforces ALLOWED_TRANSITIONS.
"""

from __future__ import annotations  # lets `list[...]` hints work after the list() method

import json
import sqlite3
import uuid
from pathlib import Path

from core.db import Database
from core.schemas import TaskProposal
from core.tasks import Task, TaskStatus, utcnow

_LIST_FIELDS = ("dependencies", "required_capabilities")
_OPEN_STATUSES = (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.WAITING)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL,
    department            TEXT NOT NULL,
    description           TEXT NOT NULL,
    status                TEXT NOT NULL,
    priority              INTEGER NOT NULL,
    dependencies          TEXT NOT NULL,   -- JSON list of task ids
    assigned_worker       TEXT,
    attempts              INTEGER NOT NULL,
    required_capabilities TEXT NOT NULL,   -- JSON list
    estimated_cost        REAL NOT NULL,
    actual_cost           REAL NOT NULL,
    requires_approval     INTEGER NOT NULL,
    approved_by           TEXT,
    created_at            TEXT NOT NULL,
    started_at            TEXT,
    completed_at          TEXT,
    result                TEXT,
    error                 TEXT,
    wait_reason           TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
"""


class TaskQueue:
    def __init__(self, db: Database | str | Path = "data/company.db"):
        self.db = db if isinstance(db, Database) else Database(db)
        self.conn = self.db.conn
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- creating and reading ----------------------------------------------

    def add(self, proposal: TaskProposal, project_id: str,
            dependencies: list[str] | None = None) -> Task:
        """Accept a validated proposal into the queue. Code assigns the id and status."""
        dependencies = list(dependencies or [])
        with self.db.transaction():
            for dep_id in dependencies:
                if self._load(dep_id) is None:
                    raise ValueError(f"unknown dependency: {dep_id}")
            task = Task(
                id="task_" + uuid.uuid4().hex[:12],
                project_id=project_id,
                department=proposal.department,
                description=proposal.description,
                priority=proposal.priority,
                dependencies=dependencies,
                required_capabilities=proposal.required_capabilities,
                estimated_cost=proposal.estimated_cost,
                requires_approval=proposal.requires_approval,
            )
            if self._dependencies_done(task):
                task.status = TaskStatus.READY
            self._save(task)
        return task

    def get(self, task_id: str) -> Task | None:
        return self._load(task_id)

    def list(self, status: TaskStatus | None = None, project_id: str | None = None) -> list[Task]:
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if status is not None:
            sql += " AND status = ?"
            args.append(status.value)
        if project_id is not None:
            sql += " AND project_id = ?"
            args.append(project_id)
        sql += " ORDER BY priority, rowid"
        return [self._from_row(r) for r in self.conn.execute(sql, args)]

    def stats(self, project_id: str | None = None) -> dict[str, int]:
        counts = {s.value: 0 for s in TaskStatus}
        sql, args = "SELECT status, COUNT(*) AS n FROM tasks", []
        if project_id is not None:
            sql += " WHERE project_id = ?"
            args.append(project_id)
        for row in self.conn.execute(sql + " GROUP BY status", args):
            counts[row["status"]] = row["n"]
        return counts

    def project_costs(self, project_id: str) -> tuple[float, float]:
        """(actual cost spent so far, estimated cost of tasks not yet finished).
        Spent includes expenses outside tasks (e.g. studio runs started by the owner)."""
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(actual_cost), 0) AS spent, "
            f"COALESCE(SUM(CASE WHEN status IN ({','.join('?' * len(_OPEN_STATUSES))}) "
            f"THEN estimated_cost ELSE 0 END), 0) AS open_estimated "
            f"FROM tasks WHERE project_id = ?",
            [s.value for s in _OPEN_STATUSES] + [project_id],
        ).fetchone()
        try:
            untasked = self.conn.execute(
                "SELECT COALESCE(SUM(amount_eur), 0) AS t FROM expenses "
                "WHERE project_id = ? AND task_id IS NULL", (project_id,)).fetchone()["t"]
        except sqlite3.OperationalError:   # queue used without an expense ledger
            untasked = 0.0
        return row["spent"] + untasked, row["open_estimated"]

    # --- lifecycle ----------------------------------------------------------

    def claim_next(self, worker: str, project_id: str | None = None) -> Task | None:
        """Atomically take the highest-priority READY task (oldest first) and mark it RUNNING."""
        sql, args = "SELECT id FROM tasks WHERE status = ?", [TaskStatus.READY.value]
        if project_id is not None:
            sql += " AND project_id = ?"
            args.append(project_id)
        with self.db.transaction():
            row = self.conn.execute(sql + " ORDER BY priority, rowid LIMIT 1", args).fetchone()
            if row is None:
                return None
            return self.start(row["id"], worker)

    def start(self, task_id: str, worker: str) -> Task:
        """Mark one specific READY task RUNNING."""
        with self.db.transaction():
            attempts = self._require(task_id).attempts + 1
            return self._transition(task_id, TaskStatus.RUNNING, assigned_worker=worker,
                                    started_at=utcnow(), attempts=attempts)

    def complete(self, task_id: str, result: str, actual_cost: float = 0.0) -> Task:
        with self.db.transaction():
            task = self._transition(task_id, TaskStatus.COMPLETED, result=result,
                                    actual_cost=actual_cost, completed_at=utcnow())
            self._promote_pending()
        return task

    def fail(self, task_id: str, error: str, actual_cost: float = 0.0) -> Task:
        with self.db.transaction():
            cost = self._require(task_id).actual_cost + actual_cost
            return self._transition(task_id, TaskStatus.FAILED, error=error,
                                    actual_cost=cost, completed_at=utcnow())

    def retry(self, task_id: str) -> Task:
        with self.db.transaction():
            return self._transition(task_id, TaskStatus.READY, error=None, assigned_worker=None,
                                    started_at=None, completed_at=None)

    def wait(self, task_id: str, reason: str) -> Task:
        with self.db.transaction():
            return self._transition(task_id, TaskStatus.WAITING, wait_reason=reason)

    def resume(self, task_id: str) -> Task:
        with self.db.transaction():
            return self._transition(task_id, TaskStatus.READY, wait_reason=None,
                                    assigned_worker=None, started_at=None)

    def approve(self, task_id: str, approver: str) -> Task:
        """Record an approval and release a WAITING task."""
        with self.db.transaction():
            return self._transition(task_id, TaskStatus.READY, approved_by=approver,
                                    wait_reason=None, assigned_worker=None, started_at=None)

    def cancel(self, task_id: str, reason: str = "") -> Task:
        with self.db.transaction():
            return self._transition(task_id, TaskStatus.CANCELLED, error=reason or None,
                                    completed_at=utcnow())

    def cancel_open(self, project_id: str, reason: str) -> list[Task]:
        """Cancel every unfinished task of a project."""
        with self.db.transaction():
            return [self.cancel(t.id, reason) for t in self.list(project_id=project_id)
                    if t.status in _OPEN_STATUSES]

    # --- internals ----------------------------------------------------------

    def _require(self, task_id: str) -> Task:
        task = self._load(task_id)
        if task is None:
            raise KeyError(f"unknown task: {task_id}")
        return task

    def _transition(self, task_id: str, new_status: TaskStatus, **changes) -> Task:
        task = self._require(task_id)
        task.check_transition(new_status)
        task.status = new_status
        for field, value in changes.items():
            setattr(task, field, value)  # validate_assignment=True re-checks each value
        self._save(task)
        return task

    def _dependencies_done(self, task: Task) -> bool:
        for dep_id in task.dependencies:
            dep = self._load(dep_id)
            if dep is None or dep.status != TaskStatus.COMPLETED:
                return False
        return True

    def _promote_pending(self) -> None:
        for task in self.list(status=TaskStatus.PENDING):
            if self._dependencies_done(task):
                self._transition(task.id, TaskStatus.READY)

    def _load(self, task_id: str) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._from_row(row) if row else None

    def _save(self, task: Task) -> None:
        data = task.model_dump(mode="json")
        for f in _LIST_FIELDS:
            data[f] = json.dumps(data[f])
        data["requires_approval"] = int(data["requires_approval"])
        cols = list(data)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
        self.conn.execute(
            f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            [data[c] for c in cols],
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Task:
        data = dict(row)
        for f in _LIST_FIELDS:
            data[f] = json.loads(data[f])
        data["requires_approval"] = bool(data["requires_approval"])
        return Task.model_validate(data)
