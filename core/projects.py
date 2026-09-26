"""
Projects and their persistent store.

Status meanings:
  ACTIVE     work is being planned and executed
  PAUSED     no new work is picked up; can be resumed
  FINISHED   ended (success or failure recorded in `outcome`)   (terminal)
  CANCELLED  abandoned                                          (terminal)
"""

from __future__ import annotations  # lets `list[...]` hints work after the list() method

import json
import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from core.db import Database
from core.tasks import InvalidTransition, utcnow


class ProjectStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    CANCELLED = "CANCELLED"


ALLOWED_PROJECT_TRANSITIONS = {
    ProjectStatus.ACTIVE:    {ProjectStatus.PAUSED, ProjectStatus.FINISHED, ProjectStatus.CANCELLED},
    ProjectStatus.PAUSED:    {ProjectStatus.ACTIVE, ProjectStatus.FINISHED, ProjectStatus.CANCELLED},
    ProjectStatus.FINISHED:  set(),
    ProjectStatus.CANCELLED: set(),
}


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=4000)
    status: ProjectStatus = ProjectStatus.ACTIVE
    priority: int = Field(ge=1, le=5)  # 1 = highest
    budget_eur: float = Field(ge=0)
    permissions: list[str] = Field(default_factory=list)  # e.g. publishing, paid_ads
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    outcome: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    objective   TEXT NOT NULL,
    status      TEXT NOT NULL,
    priority    INTEGER NOT NULL,
    budget_eur  REAL NOT NULL,
    permissions TEXT NOT NULL,   -- JSON list
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    finished_at TEXT,
    outcome     TEXT
);
"""


class ProjectStore:
    def __init__(self, db: Database):
        self.db = db
        self.db.executescript(_SCHEMA)

    def create(self, name: str, objective: str, budget_eur: float, priority: int = 3,
               permissions: list[str] | None = None) -> Project:
        project = Project(id="proj_" + uuid.uuid4().hex[:12], name=name, objective=objective,
                          budget_eur=budget_eur, priority=priority,
                          permissions=sorted(set(permissions or [])))
        self._save(project)
        return project

    def get(self, project_id: str) -> Project:
        row = self.db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown project: {project_id}")
        return self._from_row(row)

    def list(self, statuses: list[ProjectStatus] | None = None) -> list[Project]:
        sql, args = "SELECT * FROM projects", []
        if statuses:
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            args = [s.value for s in statuses]
        sql += " ORDER BY priority, rowid"
        return [self._from_row(r) for r in self.db.execute(sql, args)]

    def set_status(self, project_id: str, new_status: ProjectStatus,
                   outcome: str | None = None) -> Project:
        with self.db.transaction():
            project = self.get(project_id)
            if new_status not in ALLOWED_PROJECT_TRANSITIONS[project.status]:
                raise InvalidTransition(f"project {project_id}: {project.status.value} -> "
                                        f"{new_status.value} is not allowed")
            project.status = new_status
            project.updated_at = utcnow()
            if new_status in (ProjectStatus.FINISHED, ProjectStatus.CANCELLED):
                project.finished_at = utcnow()
                project.outcome = outcome
            self._save(project)
        return project

    def update_objective(self, project_id: str, objective: str) -> Project:
        with self.db.transaction():
            project = self.get(project_id)
            project.objective = objective
            project.updated_at = utcnow()
            self._save(project)
        return project

    def _save(self, project: Project) -> None:
        data = project.model_dump(mode="json")
        data["permissions"] = json.dumps(data["permissions"])
        cols = list(data)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
        self.db.execute(
            f"INSERT INTO projects ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            [data[c] for c in cols],
        )

    @staticmethod
    def _from_row(row) -> Project:
        data = dict(row)
        data["permissions"] = json.loads(data["permissions"])
        return Project.model_validate(data)
