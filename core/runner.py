"""
Runner: the execution loop that ties everything together, one task at a time.

For each step:
  1. project managers handle failed tasks (retry / give up / cancel blocked)
  2. the orchestrator hands out the next task (highest-priority project first)
  3. the permission gate decides: allow / approval / human_step / deny
  4. if allowed, a temporary Worker executes it and is discarded
Only one task runs at a time: the GPU cannot run two models at once.
"""

import traceback

from clients import ModelClientError
from core.router import NoModelAvailable
from core.structured import StructuredOutputError
from core.tasks import TaskStatus
from core.tools import ToolError
from core.workers import Worker, new_worker_id

ACTOR = "RUNNER"
_MAX_CONTEXT_CHARS = 2500


class Runner:
    def __init__(self, company):
        self.c = company

    def run(self, max_tasks: int = 10) -> list[dict]:
        outcomes = []
        for _ in range(max_tasks):
            outcome = self.run_once()
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    def run_once(self) -> dict | None:
        c = self.c
        for project in c.orchestrator.prioritized_projects():
            c.orchestrator.manager(project.id).handle_failures()

        worker_id = new_worker_id()
        task = c.orchestrator.next_task(worker_id)
        if task is None:
            return None
        project = c.projects.get(task.project_id)
        budget = c.orchestrator.budget(project.id)
        verdict = c.gate.check(task, project, budget)
        c.events.record("PERMISSION_GATE", f"Verdict: {verdict.kind}", project.id, task.id,
                        reason=verdict.reason, approved_by=verdict.approved_by)

        if verdict.kind == "deny":
            c.queue.cancel(task.id, f"denied: {verdict.reason}")
            return {"task": task.id, "outcome": "denied", "reason": verdict.reason}
        if verdict.kind in ("approval", "human_step"):
            c.queue.wait(task.id, verdict.reason)
            return {"task": task.id, "outcome": f"waiting ({verdict.kind})",
                    "reason": verdict.reason}

        department = c.departments.get(task.department)
        worker = Worker(worker_id, task.department, department, c.router, project,
                        c.policy.auto_approve_max_eur, tools=c.tools, research=c.research,
                        memory_context=c.memory.context_for(project.id, task.description),
                        company=c, budget_remaining=budget["remaining"])
        c.workers.started(worker_id, task.department, task)
        c.events.record("WORKER", "Started task", project.id, task.id, worker=worker_id,
                        department=task.department)
        try:
            result, model_key, cost = worker.execute(task, self._context(task))
        except Exception as e:   # expected: model/tool errors; anything else is a bug, logged
            expected = isinstance(e, (ModelClientError, StructuredOutputError, NoModelAvailable,
                                      ToolError))
            if worker.spent > 0:     # money a tool already spent before the failure
                c.ledger.record(project.id, worker.spent, "tool_call", f"tools for {task.id} "
                                "(task failed later)", task.id,
                                approved_by=verdict.approved_by or "POLICY_AUTO")
            c.queue.fail(task.id, f"{type(e).__name__}: {e}", actual_cost=worker.spent)
            c.workers.finished(worker_id, "FAILED", error=str(e))
            c.events.record("WORKER", "Task failed", project.id, task.id, worker=worker_id,
                            error=str(e)[:300], files=len(worker.assets),
                            **({} if expected else {"trace": traceback.format_exc()[-800:]}))
            return {"task": task.id, "outcome": "failed", "reason": str(e)[:200]}

        tokens = sum(r.prompt_tokens + r.completion_tokens for r in result.responses)
        if cost > 0:
            c.ledger.record(project.id, cost, "model_call", f"{model_key} for {task.id}",
                            task.id, approved_by=verdict.approved_by or "POLICY_AUTO")
        value = result.value
        files = ("\n\nFiles produced (asset ids, review them under Studio):\n"
                 + "\n".join(f"- {line}" for line in c.assets.summary_lines(worker.assets[:25]))
                 ) if worker.assets else ""
        if value.success or worker.assets:   # files exist = the tool did the work
            c.queue.complete(task.id, f"{value.summary}\n\n{value.output}{files}", actual_cost=cost)
            status = "COMPLETED"
        else:
            c.queue.fail(task.id, f"worker reported failure: {value.summary}", actual_cost=cost)
            status = "FAILED"
        c.workers.finished(worker_id, status, model_key, tokens, cost)
        c.events.record("WORKER", f"Task {status.lower()}", project.id, task.id,
                        worker=worker_id, model=model_key, tokens=tokens, cost_eur=cost,
                        seconds=round(result.total_seconds, 1), summary=value.summary[:200],
                        files=len(worker.assets))
        return {"task": task.id, "outcome": status.lower(), "summary": value.summary,
                "model": model_key, "seconds": round(result.total_seconds, 1)}

    # --- human actions --------------------------------------------------------

    def approve(self, task_id: str, approver: str = "HUMAN"):
        task = self.c.queue.approve(task_id, approver)
        self.c.events.record(approver, "Approved task", task.project_id, task_id,
                             estimated_cost=task.estimated_cost)
        return task

    def complete_manual(self, task_id: str, result: str, cost_eur: float = 0.0,
                        who: str = "HUMAN"):
        """A human did a manual task (e.g. published a listing, bought something)."""
        task = self.c.queue.get(task_id)
        if task is None or task.status != TaskStatus.WAITING:
            raise ValueError(f"task {task_id} is not waiting for a human")
        self.c.queue.resume(task_id)
        self.c.queue.start(task_id, who)
        if cost_eur > 0:
            self.c.ledger.record(task.project_id, cost_eur, "purchase", task.description,
                                 task_id, approved_by=who)
        task = self.c.queue.complete(task_id, result, actual_cost=cost_eur)
        self.c.events.record(who, "Completed manual task", task.project_id, task_id,
                             cost_eur=cost_eur)
        return task

    # --- internals ------------------------------------------------------------

    def _context(self, task) -> str:
        parts = []
        for dep_id in task.dependencies:
            dep = self.c.queue.get(dep_id)
            if dep and dep.result:
                parts.append(f"- {dep.description}:\n{dep.result[:_MAX_CONTEXT_CHARS]}")
        return "\n".join(parts)
