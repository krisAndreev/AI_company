"""
Project Manager: owns one project.

- plans the project's work (AI proposes tasks + dependencies, code validates)
- replans in stages, using results of completed tasks
- handles failed tasks (retry, give up, cancel blocked dependents, escalate)
- reports the project's state as code-computed facts

A ProjectManager holds no state of its own: everything lives in the database,
so one can be created on demand and nothing is lost on restart.
"""

from dataclasses import dataclass

from core.config import OrchestratorConfig
from core.departments import DepartmentRegistry
from core.events import EventLog
from core.projects import Project, ProjectStatus
from core.router import ModelRouter, RouteRequest
from core.schemas import ProjectPlan
from core.structured import generate_structured
from core.task_queue import TaskQueue
from core.tasks import Task, TaskStatus

ACTOR = "PROJECT_MANAGER"
_OPEN = {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.WAITING}


class ManagementError(Exception):
    """A request a manager refuses (wrong state, limit exceeded, ...)."""


@dataclass
class PlanResult:
    accepted: list[Task]
    rejected: list[tuple[str, str]]  # (task description, reason)
    model_key: str


class ProjectManager:
    def __init__(self, project: Project, queue: TaskQueue, events: EventLog,
                 router: ModelRouter, config: OrchestratorConfig,
                 departments: DepartmentRegistry, memory=None, tools=None):
        self.project = project
        self.queue = queue
        self.events = events
        self.router = router
        self.config = config
        self.departments = departments
        self.memory = memory  # MemoryStore or None
        self.tools = tools    # ToolRegistry or None (planner sees which tools are available)

    # --- planning -------------------------------------------------------------

    def plan(self) -> PlanResult:
        """Plan the next stage of work. Refused while earlier tasks are still open."""
        if self.project.status != ProjectStatus.ACTIVE:
            self._refuse("plan", f"project is {self.project.status.value}")
        tasks = self._tasks()
        open_tasks = [t for t in tasks if t.status in _OPEN]
        if open_tasks:
            self._refuse("plan", f"{len(open_tasks)} tasks still open; finish them first")

        budget = self._budget()
        done = [t for t in tasks if t.status == TaskStatus.COMPLETED][-10:]
        history = "\n".join(f"- {t.description}: {(t.result or '')[:200]}" for t in done)
        route = self.router.select(RouteRequest(task_type="planning"))
        memory = (self.memory.context_for(self.project.id, self.project.objective)
                  if self.memory else "")
        prompt = (
            f"Project: {self.project.name}\n"
            f"Objective: {self.project.objective}\n"
            f"Budget available: EUR {budget['uncommitted']:.2f}\n"
            f"Departments:\n{self.departments.describe(self.tools.available if self.tools else None)}\n"
            + (f"What we already know:\n{memory}\n" if memory else "")
            + (f"Already completed:\n{history}\n" if history else "This is the first plan.\n")
            + f"Propose at most {self.config.max_tasks_per_plan} concrete next tasks. "
              f"Give each task the capabilities it needs from its own department. "
              f"Use depends_on to list the numbers (1-based) of EARLIER tasks in this plan "
              f"that must finish first; tasks that can run in parallel should not depend on "
              f"each other. Capabilities marked (tool) are carried out for real by code (web "
              f"search, building product files, images, videos, campaign packs): use them "
              f"for real deliverables, and make a task depend on the tasks whose results it "
              f"needs (e.g. a campaign on the product). Work done by local AI costs EUR 0; "
              f"only give a cost above 0 if real money must be spent. Set requires_approval "
              f"to true only if the task spends money or publishes something."
        )
        plan = generate_structured(
            route.client, route.model, prompt, ProjectPlan,
            system="You are the project manager of a small online business project. "
                   "Be concrete and realistic.",
            think=route.spec.think,
            constrain_to=self.departments.plan_schema(),
        ).value
        self._log("Received plan proposal", model=route.model_key, proposed=len(plan.tasks))

        accepted, rejected = [], []
        accepted_ids: dict[int, str] = {}  # plan number -> real task id
        committed = 0.0
        for number, proposal in enumerate(plan.tasks, start=1):
            department = proposal.department.strip().lower()
            dept = self.departments.get(department)
            # Dependencies are ordering hints: a reference to itself, a later task or a
            # rejected task is DROPPED (logged) instead of throwing the task away - small
            # models often number them wrongly, and one bad number used to sink a plan.
            bad_refs = [d for d in proposal.depends_on if d < 1 or d >= number]
            missing = [d for d in proposal.depends_on if d not in bad_refs and d not in accepted_ids]
            if bad_refs or missing:
                self._log("Dropped invalid dependencies", plan_number=number,
                          invalid=bad_refs, rejected=missing)
                proposal = proposal.model_copy(update={"depends_on": [
                    d for d in proposal.depends_on if d not in bad_refs and d not in missing]})
            if number > self.config.max_tasks_per_plan:
                reason = f"over max_tasks_per_plan ({self.config.max_tasks_per_plan})"
            elif dept is None:
                reason = f"unknown department {proposal.department!r}"
            elif foreign := [c for c in proposal.required_capabilities
                             if c not in dept.capabilities]:
                reason = f"capabilities {foreign} not offered by {department}"
            elif committed + proposal.estimated_cost > budget["uncommitted"]:
                reason = (f"estimated cost EUR {proposal.estimated_cost:.2f} exceeds remaining "
                          f"budget EUR {budget['uncommitted'] - committed:.2f}")
            else:
                reason = None

            if reason:
                rejected.append((proposal.description, reason))
                self._log("Rejected proposed task", plan_number=number,
                          description=proposal.description, reason=reason)
                continue
            task = self.queue.add(
                proposal.model_copy(update={"department": department}),
                self.project.id,
                dependencies=[accepted_ids[d] for d in proposal.depends_on],
            )
            accepted_ids[number] = task.id
            committed += proposal.estimated_cost
            accepted.append(task)
            self._log(f"Created {department} task", task.id, plan_number=number,
                      depends_on=proposal.depends_on, status=task.status.value,
                      description=task.description, estimated_cost=task.estimated_cost)
        return PlanResult(accepted, rejected, route.model_key)

    # --- failure handling -------------------------------------------------------

    def handle_failures(self) -> dict[str, list[str]]:
        """Retry failed tasks while allowed; otherwise give up, cancel blocked tasks, escalate."""
        outcome = {"retried": [], "gave_up": [], "cancelled_blocked": []}
        for task in self._tasks(TaskStatus.FAILED):
            if task.attempts <= self.config.max_task_retries:
                self.queue.retry(task.id)
                outcome["retried"].append(task.id)
                self._log("Retried failed task", task.id, attempt=task.attempts + 1,
                          max_attempts=self.config.max_task_retries + 1, last_error=task.error)
            else:
                outcome["gave_up"].append(task.id)
                self._log("Gave up on task", task.id, attempts=task.attempts, error=task.error)

        # Cancel tasks that can never run because a dependency failed for good or was
        # cancelled. Repeat until stable so whole chains of dependents are cancelled.
        changed = True
        while changed:
            changed = False
            dead = {t.id: t.status for t in self._tasks()
                    if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)}
            for task in self._tasks(TaskStatus.PENDING):
                blockers = [d for d in task.dependencies if d in dead]
                if blockers:
                    self.queue.cancel(task.id, f"blocked: dependency {blockers[0]} "
                                               f"{dead[blockers[0]].value.lower()}")
                    outcome["cancelled_blocked"].append(task.id)
                    self._log("Cancelled blocked task", task.id, blocked_by=blockers)
                    changed = True

        if outcome["gave_up"]:
            self._log("Escalated to MASTER_ORCHESTRATOR",
                      reason=f"{len(outcome['gave_up'])} task(s) failed after all retries",
                      tasks=outcome["gave_up"])
        return outcome

    # --- reporting ------------------------------------------------------------------

    def report(self) -> dict:
        """Facts about the project, computed by code (the AI never writes these)."""
        tasks = self._tasks()
        counts = {s.value: 0 for s in TaskStatus}
        for t in tasks:
            counts[t.status.value] += 1
        countable = len(tasks) - counts["CANCELLED"]
        dead = {t.id for t in tasks if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)}
        return {
            "project": self.project.name,
            "objective": self.project.objective,
            "status": self.project.status.value,
            "progress_percent": round(100 * counts["COMPLETED"] / countable) if countable else 0,
            "tasks": {k: v for k, v in counts.items() if v},
            "budget_eur": self._budget(),
            "failed": [t.description for t in tasks if t.status == TaskStatus.FAILED],
            "blocked": [t.description for t in tasks if t.status == TaskStatus.PENDING
                        and any(d in dead for d in t.dependencies)],
            "ready": [t.description for t in tasks if t.status == TaskStatus.READY],
            "recent_results": [f"{t.description}: {(t.result or '')[:150]}"
                               for t in tasks if t.status == TaskStatus.COMPLETED][-5:],
        }

    # --- internals ------------------------------------------------------------------

    def _tasks(self, status: TaskStatus | None = None) -> list[Task]:
        return self.queue.list(status=status, project_id=self.project.id)

    def _budget(self) -> dict:
        spent, open_estimated = self.queue.project_costs(self.project.id)
        allocated = self.project.budget_eur
        return {"allocated": allocated, "spent": spent, "remaining": allocated - spent,
                "estimated_future": open_estimated,
                "uncommitted": allocated - spent - open_estimated}

    def _log(self, action: str, task_id: str | None = None, **details) -> None:
        self.events.record(ACTOR, action, self.project.id, task_id, **details)

    def _refuse(self, action: str, reason: str):
        self._log(f"Refused {action}", reason=reason)
        raise ManagementError(f"{action} refused: {reason}")
