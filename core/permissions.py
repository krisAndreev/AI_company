"""
Budgets and permissions.

- ApprovalPolicy: configurable spending tiers and known project permissions.
- ExpenseLedger: every real expense, with who approved it.
- PermissionGate: decides, in code, whether a task may run now.

Verdicts:
  allow       run it (approved_by says which rule allowed any spending)
  approval    wait for a human approval (spending tier, budget, or AI-flagged)
  human_step  a manual capability: a human must do this task
  deny        never allowed for this project (cancel)
"""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.db import Database
from core.departments import DepartmentRegistry
from core.projects import Project
from core.tasks import Task, utcnow


class ApprovalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_approve_max_eur: float = Field(ge=0)     # spend up to this: automatic
    project_approve_max_eur: float = Field(ge=0)  # up to this: project-level rule
    known_permissions: list[str]
    default_project_permissions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        if self.project_approve_max_eur < self.auto_approve_max_eur:
            raise ValueError("project_approve_max_eur must be >= auto_approve_max_eur")
        unknown = set(self.default_project_permissions) - set(self.known_permissions)
        if unknown:
            raise ValueError(f"unknown default permissions: {sorted(unknown)}")
        return self

    @classmethod
    def load(cls, path: str | Path = "config/policy.json") -> "ApprovalPolicy":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass
class Verdict:
    kind: Literal["allow", "approval", "human_step", "deny"]
    reason: str
    approved_by: str | None = None


class PermissionGate:
    def __init__(self, policy: ApprovalPolicy, departments: DepartmentRegistry, tools=None):
        self.policy = policy
        self.departments = departments
        self.tools = tools  # ToolRegistry or None

    def check(self, task: Task, project: Project, budget: dict) -> Verdict:
        dept = self.departments.get(task.department)
        if dept is None:
            return Verdict("deny", f"unknown department {task.department!r}")

        manual = []
        for cap in task.required_capabilities:
            spec = dept.capabilities.get(cap)
            if spec is None:
                return Verdict("deny", f"capability {cap!r} not offered by {task.department}")
            if spec.requires_permission and spec.requires_permission not in project.permissions:
                return Verdict("deny", f"project lacks permission {spec.requires_permission!r} "
                                       f"needed for {cap!r}")
            tool_ready = bool(spec.tool and self.tools and self.tools.available(spec.tool))
            if not spec.automated and not tool_ready:
                manual.append(cap)

        approved_by = task.approved_by
        cost = task.estimated_cost
        if cost > 0 and not approved_by:
            if cost > budget["remaining"]:
                return Verdict("approval", f"insufficient budget: needs EUR {cost:.2f}, "
                                           f"remaining EUR {budget['remaining']:.2f}")
            if cost <= self.policy.auto_approve_max_eur:
                approved_by = "POLICY_AUTO"
            elif cost <= self.policy.project_approve_max_eur:
                approved_by = "PROJECT_MANAGER"
            else:
                return Verdict("approval", f"EUR {cost:.2f} is above the human-approval "
                                           f"threshold EUR {self.policy.project_approve_max_eur:.2f}")
        if task.requires_approval and not task.approved_by:
            return Verdict("approval", "task was flagged as requiring approval")
        if manual:
            return Verdict("human_step", f"manual capabilities {manual}: a human must do this")
        return Verdict("allow", "all checks passed", approved_by)


_EXPENSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    task_id     TEXT,
    amount_eur  REAL NOT NULL,
    category    TEXT NOT NULL,     -- model_call, purchase, ...
    description TEXT NOT NULL,
    approved_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_expenses_project ON expenses(project_id);
"""


class ExpenseLedger:
    def __init__(self, db: Database):
        self.db = db
        self.db.executescript(_EXPENSE_SCHEMA)

    def record(self, project_id: str, amount_eur: float, category: str, description: str,
               task_id: str | None = None, approved_by: str | None = None) -> str:
        if amount_eur < 0:
            raise ValueError("expense cannot be negative")
        expense_id = "exp_" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO expenses VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (expense_id, utcnow().isoformat(), project_id, task_id, amount_eur, category,
             description, approved_by),
        )
        return expense_id

    def total(self, project_id: str) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(amount_eur), 0) AS t FROM expenses "
                              "WHERE project_id = ?", (project_id,)).fetchone()
        return row["t"]

    def list(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM expenses WHERE project_id = ? ORDER BY ts", (project_id,))]
