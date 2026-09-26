"""
Master Orchestrator: the head of the company.

"AI decides what should happen. Code decides what is allowed to happen.
 Code executes the action."

- Creates projects, allocates budgets and grants permissions (within company limits).
- Hands each project to a ProjectManager, which plans and runs it.
- Reviews projects (code rules first, then AI) and decides continue/pause/finish.
- Routes capability requests to the department that offers them.
- Distributes work to workers, highest-priority project first.
Every step is logged with its reason.
"""

import json
from dataclasses import dataclass

from core.config import OrchestratorConfig
from core.departments import DepartmentRegistry
from core.events import EventLog
from core.permissions import ApprovalPolicy
from core.project_manager import ManagementError, PlanResult, ProjectManager
from core.projects import Project, ProjectStatus, ProjectStore
from core.router import ModelRouter, RouteRequest
from core.schemas import ProjectDecision
from core.structured import generate_structured
from core.task_queue import TaskQueue
from core.tasks import Task, utcnow

__all__ = ["MasterOrchestrator", "OrchestratorConfig", "OrchestratorError", "PlanResult",
           "ReviewResult"]

ACTOR = "MASTER_ORCHESTRATOR"
_LIVE = [ProjectStatus.ACTIVE, ProjectStatus.PAUSED]  # projects that hold allocated budget


class OrchestratorError(ManagementError):
    """A request the orchestrator refuses (limit exceeded, wrong project state, ...)."""


@dataclass
class ReviewResult:
    decision: str        # continue / pause / finish
    reason: str
    confidence: float
    source: str          # "rule" (code) or "ai"
    applied: bool        # False when the decision was deferred
    model_key: str | None = None


class MasterOrchestrator:
    def __init__(self, projects: ProjectStore, queue: TaskQueue, events: EventLog,
                 router: ModelRouter, config: OrchestratorConfig,
                 departments: DepartmentRegistry, policy: ApprovalPolicy, memory=None,
                 tools=None):
        self.projects = projects
        self.queue = queue
        self.events = events
        self.router = router
        self.config = config
        self.departments = departments
        self.policy = policy
        self.memory = memory  # MemoryStore or None
        self.tools = tools    # ToolRegistry or None

    # --- projects -------------------------------------------------------------

    def create_project(self, name: str, objective: str, budget_eur: float,
                       priority: int = 3, permissions: list[str] | None = None) -> Project:
        permissions = sorted(set(self.policy.default_project_permissions) | set(permissions or []))
        unknown = set(permissions) - set(self.policy.known_permissions)
        if unknown:
            self._refuse("create_project", f"unknown permissions {sorted(unknown)}", name=name)
        active = self.projects.list([ProjectStatus.ACTIVE])
        if len(active) >= self.config.max_active_projects:
            self._refuse("create_project", f"max_active_projects ({self.config.max_active_projects}) "
                                           f"reached", name=name)
        allocated = sum(p.budget_eur for p in self.projects.list(_LIVE))
        if allocated + budget_eur > self.config.total_budget_eur:
            self._refuse("create_project",
                         f"budget EUR {budget_eur:.2f} exceeds unallocated company budget "
                         f"EUR {self.config.total_budget_eur - allocated:.2f}", name=name)

        project = self.projects.create(name, objective, budget_eur, priority, permissions)
        self.events.record(ACTOR, "Created project", project.id, name=name,
                           budget_eur=budget_eur, priority=priority, permissions=permissions,
                           objective=objective)
        self.events.record(ACTOR, "Assigned project manager", project.id)
        return project

    def manager(self, project_id: str) -> ProjectManager:
        return ProjectManager(self.projects.get(project_id), self.queue, self.events,
                              self.router, self.config, self.departments, self.memory,
                              self.tools)

    def set_project_status(self, project_id: str, status: ProjectStatus, reason: str,
                           actor: str = "HUMAN") -> Project:
        project = self.projects.set_status(project_id, status, outcome=reason)
        if status in (ProjectStatus.FINISHED, ProjectStatus.CANCELLED):
            cancelled = self.queue.cancel_open(project_id, f"project {status.value.lower()}")
            self.events.record(actor, f"Cancelled {len(cancelled)} open tasks", project_id)
        self.events.record(actor, f"Project -> {status.value}", project_id, reason=reason)
        return project

    def prioritized_projects(self) -> list[Project]:
        """Active projects in the order work should be done (priority, then oldest)."""
        return self.projects.list([ProjectStatus.ACTIVE])

    def budget(self, project_id: str) -> dict:
        return self.manager(project_id).report()["budget_eur"]

    def request_capability(self, capability: str, project_id: str | None = None) -> str:
        """Which shared department provides a capability? Refused if none does."""
        department = self.departments.find_provider(capability)
        if department is None:
            self._refuse("request_capability", f"no department offers {capability!r}",
                         project_id=project_id)
        self.events.record(ACTOR, "Capability request", project_id, capability=capability,
                           department=department)
        return department

    # --- planning (delegated) ---------------------------------------------------

    def plan_project(self, project_id: str) -> PlanResult:
        self._require_active(project_id, "plan_project")
        return self.manager(project_id).plan()

    # --- reviewing ----------------------------------------------------------------

    def review_project(self, project_id: str, metrics: dict | None = None) -> ReviewResult:
        project = self._require_active(project_id, "review_project")
        report = self.manager(project_id).report()
        budget = report["budget_eur"]

        # Hard rules first: code decides, the AI is not asked.
        if budget["spent"] > 0 and budget["spent"] >= budget["allocated"]:
            result = ReviewResult("pause", f"budget exhausted (spent EUR {budget['spent']:.2f} of "
                                           f"EUR {budget['allocated']:.2f})", 1.0, "rule", True)
            return self._apply_review(project, result)

        facts = {
            **report,
            "days_running": round((utcnow() - project.created_at).total_seconds() / 86400, 1),
            "metrics": metrics or {},
        }
        route = self.router.select(RouteRequest(task_type="decision"))
        decision = generate_structured(
            route.client, route.model,
            "Review this project and decide: continue, pause, or finish.\n"
            + json.dumps(facts, indent=2, default=str),
            ProjectDecision,
            system="You are the head of a small online business. Stop work that is not "
                   "working; continue work that is progressing. Be honest about confidence.",
            think=route.spec.think,
        ).value

        applied = decision.confidence >= self.config.min_decision_confidence
        result = ReviewResult(decision.decision, decision.reason, decision.confidence, "ai",
                              applied, route.model_key)
        return self._apply_review(project, result)

    # --- work distribution --------------------------------------------------------

    def next_task(self, worker: str) -> Task | None:
        """Give a worker the next task: highest-priority active project first."""
        for project in self.prioritized_projects():
            task = self.queue.claim_next(worker, project_id=project.id)
            if task:
                self.events.record(ACTOR, "Assigned task", project.id, task.id, worker=worker)
                return task
        return None

    # --- internals ------------------------------------------------------------------

    def _apply_review(self, project: Project, result: ReviewResult) -> ReviewResult:
        self.events.record(ACTOR, f"Decision: {result.decision}", project.id,
                           source=result.source, confidence=result.confidence,
                           applied=result.applied, model=result.model_key, reason=result.reason)
        if not result.applied:
            self.events.record(ACTOR, "Decision deferred (confidence below "
                                      f"{self.config.min_decision_confidence})", project.id)
            return result
        if result.decision == "pause":
            self.set_project_status(project.id, ProjectStatus.PAUSED, result.reason, actor=ACTOR)
        elif result.decision == "finish":
            self.set_project_status(project.id, ProjectStatus.FINISHED, result.reason, actor=ACTOR)
        return result

    def _require_active(self, project_id: str, action: str) -> Project:
        project = self.projects.get(project_id)
        if project.status != ProjectStatus.ACTIVE:
            self._refuse(action, f"project is {project.status.value}", project_id=project_id)
        return project

    def _refuse(self, action: str, reason: str, project_id: str | None = None, **details):
        self.events.record(ACTOR, f"Refused {action}", project_id, reason=reason, **details)
        raise OrchestratorError(f"{action} refused: {reason}")
