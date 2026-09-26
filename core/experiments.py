"""
Controlled business experiments with explicit stopping conditions.

An experiment owns one project. evaluate() applies, in this order:
  1. success criteria met                    -> SUCCEEDED  (code)
  2. budget / duration / human-hours limits  -> FAILED     (code)
  3. checkpoint due                          -> AI proposes continue / pivot / stop;
                                                code enforces confidence and max pivots
  4. otherwise                               -> continue, no AI call
Metrics always carry a source and timestamp (third-party numbers are estimates).
"""

from __future__ import annotations  # lets `list[...]` hints work after the list() method

import json
import operator
import uuid
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.project_manager import ManagementError
from core.projects import ProjectStatus
from core.router import RouteRequest
from core.schemas import ExperimentDecision
from core.structured import generate_structured
from core.tasks import utcnow

ACTOR = "EXPERIMENT_MANAGER"
_OPS = {">=": operator.ge, ">": operator.gt, "<=": operator.le, "<": operator.lt,
        "==": operator.eq}


class SuccessCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    op: Literal[">=", ">", "<=", "<", "=="]
    target: float


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=2000)
    budget_eur: float = Field(ge=0)
    max_duration_days: float = Field(gt=0)
    max_human_hours: float = Field(ge=0)
    max_pivots: int = Field(ge=0)
    paid_ads_allowed: bool = False
    success_criteria: list[SuccessCriterion] = Field(min_length=1)
    checkpoint_days: list[float] = Field(default_factory=list)
    priority: int = Field(default=3, ge=1, le=5)


class ExperimentStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"      # a stopping condition was hit
    STOPPED = "STOPPED"    # stopped by decision


class Experiment(BaseModel):
    id: str
    project_id: str
    spec: ExperimentSpec
    status: ExperimentStatus = ExperimentStatus.RUNNING
    pivots_used: int = 0
    human_hours_used: float = 0.0
    next_checkpoint: int = 0      # index into spec.checkpoint_days
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    final_result: str | None = None


class Evaluation(BaseModel):
    action: Literal["continue", "pivot", "succeeded", "failed", "stopped"]
    reason: str
    source: Literal["rule", "ai"]
    model_key: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL,
    spec             TEXT NOT NULL,    -- JSON
    status           TEXT NOT NULL,
    pivots_used      INTEGER NOT NULL,
    human_hours_used REAL NOT NULL,
    next_checkpoint  INTEGER NOT NULL,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    final_result     TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    name          TEXT NOT NULL,
    value         REAL NOT NULL,
    source        TEXT NOT NULL,   -- where the number came from (etsy_stats, everbee_estimate...)
    recorded_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_exp ON metrics(experiment_id, name);
"""


class ExperimentManager:
    def __init__(self, company):
        self.c = company
        self.c.db.executescript(_SCHEMA)

    # --- lifecycle ----------------------------------------------------------------

    def create(self, spec: ExperimentSpec) -> Experiment:
        criteria = ", ".join(f"{s.metric} {s.op} {s.target:g}" for s in spec.success_criteria)
        objective = (
            f"{spec.objective}\nConstraints: budget EUR {spec.budget_eur:g}; max duration "
            f"{spec.max_duration_days:g} days; max human effort {spec.max_human_hours:g} hours; "
            f"paid advertising {'allowed' if spec.paid_ads_allowed else 'NOT allowed'}.\n"
            f"Success means: {criteria}."
        )
        project = self.c.orchestrator.create_project(
            spec.name, objective, spec.budget_eur, spec.priority,
            permissions=["paid_ads"] if spec.paid_ads_allowed else [])
        exp = Experiment(id="exp_" + uuid.uuid4().hex[:12], project_id=project.id, spec=spec)
        self._save(exp)
        self._log(exp, "Started experiment", criteria=criteria,
                  max_days=spec.max_duration_days, max_pivots=spec.max_pivots)
        return exp

    def get(self, exp_id: str) -> Experiment:
        row = self.c.db.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown experiment: {exp_id}")
        data = dict(row)
        data["spec"] = json.loads(data["spec"])
        return Experiment.model_validate(data)

    def list(self, status: ExperimentStatus | None = None) -> list[Experiment]:
        sql, args = "SELECT id FROM experiments", []
        if status is not None:
            sql += " WHERE status = ?"
            args.append(status.value)
        return [self.get(r["id"]) for r in self.c.db.execute(sql + " ORDER BY started_at", args)]

    def record_metric(self, exp_id: str, name: str, value: float, source: str) -> None:
        exp = self.get(exp_id)
        self.c.db.execute(
            "INSERT INTO metrics (experiment_id, name, value, source, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)", (exp_id, name, value, source, utcnow().isoformat()))
        self._log(exp, "Recorded metric", metric=name, value=value, source=source)

    def latest_metrics(self, exp_id: str) -> dict[str, dict]:
        rows = self.c.db.execute(
            "SELECT name, value, source, recorded_at FROM metrics WHERE experiment_id = ? "
            "ORDER BY id", (exp_id,))
        return {r["name"]: {"value": r["value"], "source": r["source"],
                            "recorded_at": r["recorded_at"]} for r in rows}

    def log_human_hours(self, exp_id: str, hours: float, note: str = "") -> None:
        if hours < 0:
            raise ValueError("hours cannot be negative")
        exp = self.get(exp_id)
        exp.human_hours_used += hours
        self._save(exp)
        self._log(exp, "Logged human effort", hours=hours, total=exp.human_hours_used, note=note)

    # --- evaluation -------------------------------------------------------------------

    def evaluate(self, exp_id: str, now: datetime | None = None) -> Evaluation:
        exp = self.get(exp_id)
        if exp.status != ExperimentStatus.RUNNING:
            raise ManagementError(f"experiment {exp_id} already ended ({exp.status.value})")
        now = now or utcnow()
        spec = exp.spec
        days = (now - exp.started_at).total_seconds() / 86400
        metrics = self.latest_metrics(exp_id)
        budget = self.c.orchestrator.budget(exp.project_id)

        # 1. success (code)
        if all(s.metric in metrics and _OPS[s.op](metrics[s.metric]["value"], s.target)
               for s in spec.success_criteria):
            return self._end(exp, ExperimentStatus.SUCCEEDED, "success criteria met",
                             Evaluation(action="succeeded", reason="success criteria met",
                                        source="rule"))
        # 2. stopping conditions (code)
        for hit, why in [
            (days >= spec.max_duration_days, f"max duration {spec.max_duration_days:g} days reached"),
            (exp.human_hours_used >= spec.max_human_hours > 0,
             f"max human effort {spec.max_human_hours:g} h reached"),
            (spec.budget_eur > 0 and budget["spent"] >= spec.budget_eur,
             f"budget EUR {spec.budget_eur:g} spent"),
        ]:
            if hit:
                return self._end(exp, ExperimentStatus.FAILED, why,
                                 Evaluation(action="failed", reason=why, source="rule"))
        # 3. checkpoint (AI proposes, code enforces)
        due = (exp.next_checkpoint < len(spec.checkpoint_days)
               and days >= spec.checkpoint_days[exp.next_checkpoint])
        if not due:
            return Evaluation(action="continue", reason="no checkpoint due", source="rule")
        return self._checkpoint(exp, days, metrics, budget)

    # --- internals ----------------------------------------------------------------------

    def _checkpoint(self, exp: Experiment, days: float, metrics: dict, budget: dict) -> Evaluation:
        spec = exp.spec
        exp.next_checkpoint += 1
        self._save(exp)
        facts = {
            "experiment": spec.model_dump(), "days_elapsed": round(days, 1),
            "metrics_latest": metrics, "budget_eur": budget,
            "human_hours_used": exp.human_hours_used,
            "pivots_left": spec.max_pivots - exp.pivots_used,
            "project": self.c.orchestrator.manager(exp.project_id).report(),
            "memory": self.c.memory.context_for(exp.project_id, spec.objective),
        }
        route = self.c.router.select(RouteRequest(task_type="decision"))
        d = generate_structured(
            route.client, route.model,
            "Checkpoint review of a business experiment. Decide: continue, pivot (change "
            "approach, give new_direction) or stop. Metrics from third parties are estimates.\n"
            + json.dumps(facts, indent=2, default=str),
            ExperimentDecision,
            system="You run small, disciplined business experiments. Stop what is clearly "
                   "failing; pivot only with a concrete new direction.",
            think=route.spec.think,
        ).value
        self._log(exp, f"Checkpoint decision: {d.decision}", confidence=d.confidence,
                  model=route.model_key, reason=d.reason, new_direction=d.new_direction)

        if d.confidence < self.c.config.min_decision_confidence:
            self._log(exp, "Decision deferred (low confidence)")
            return Evaluation(action="continue", reason=f"deferred: {d.reason}", source="ai",
                              model_key=route.model_key)
        if d.decision == "stop":
            return self._end(exp, ExperimentStatus.STOPPED, d.reason,
                             Evaluation(action="stopped", reason=d.reason, source="ai",
                                        model_key=route.model_key))
        if d.decision == "pivot":
            if exp.pivots_used >= spec.max_pivots:
                self._log(exp, "Pivot refused", reason=f"max pivots ({spec.max_pivots}) used")
                return Evaluation(action="continue", reason="pivot refused: no pivots left",
                                  source="rule", model_key=route.model_key)
            exp.pivots_used += 1
            self._save(exp)
            cancelled = self.c.queue.cancel_open(exp.project_id, "pivot")
            project = self.c.projects.get(exp.project_id)
            self.c.projects.update_objective(
                exp.project_id, f"{project.objective}\nPivot {exp.pivots_used}: {d.new_direction}")
            self._log(exp, "Pivoted", pivot=exp.pivots_used, cancelled_tasks=len(cancelled),
                      new_direction=d.new_direction)
            return Evaluation(action="pivot", reason=d.new_direction, source="ai",
                              model_key=route.model_key)
        return Evaluation(action="continue", reason=d.reason, source="ai",
                          model_key=route.model_key)

    def _end(self, exp: Experiment, status: ExperimentStatus, reason: str,
             evaluation: Evaluation) -> Evaluation:
        exp.status, exp.ended_at, exp.final_result = status, utcnow(), reason
        self._save(exp)
        self._log(exp, f"Experiment {status.value}", reason=reason)
        if self.c.projects.get(exp.project_id).status in (ProjectStatus.ACTIVE,
                                                          ProjectStatus.PAUSED):
            self.c.orchestrator.set_project_status(
                exp.project_id, ProjectStatus.FINISHED, f"experiment {status.value}: {reason}",
                actor=ACTOR)
        return evaluation

    def _save(self, exp: Experiment) -> None:
        data = exp.model_dump(mode="json")
        data["spec"] = json.dumps(data["spec"])
        cols = list(data)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
        self.c.db.execute(
            f"INSERT INTO experiments ({', '.join(cols)}) VALUES "
            f"({', '.join('?' for _ in cols)}) ON CONFLICT(id) DO UPDATE SET {updates}",
            [data[c] for c in cols])

    def _log(self, exp: Experiment, action: str, **details) -> None:
        self.c.events.record(ACTOR, action, exp.project_id, experiment=exp.id, **details)
