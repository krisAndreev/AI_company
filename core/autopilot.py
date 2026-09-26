"""
Autopilot: the orchestrator keeps projects moving without the owner clicking.

Called by the work loop whenever there is no task to run (config: autopilot = true).
For every ACTIVE project whose tasks are all finished:
  - no plan yet                    -> plan the first stage
  - stage limit reached (code)     -> finish the project, tell the owner
  - otherwise                      -> review it (code rules first, then AI decides
                                      continue / pause / finish; code enforces the
                                      confidence threshold) and plan the next stage
  - a plan with no usable tasks    -> pause and ask the owner for direction
It also tells the owner, once per task, when a task waits for them (a manual step
such as publishing, or a spending approval) - in the chat, where they can answer.
Every step uses the normal rule-checked code paths and is logged.
"""

from clients import ModelClientError
from core.project_manager import ManagementError
from core.projects import ProjectStatus
from core.router import NoModelAvailable
from core.structured import StructuredOutputError
from core.tasks import TaskStatus, utcnow

ACTOR = "MASTER_ORCHESTRATOR"
_OPEN = {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.WAITING}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_notices (
    task_id  TEXT PRIMARY KEY,
    ts       TEXT NOT NULL
);
"""


class Autopilot:
    def __init__(self, company):
        self.c = company
        self.c.db.executescript(_SCHEMA)

    def step(self) -> list[str]:
        """One round over all projects. Returns short notes of what was done."""
        if not self.c.config.autopilot:
            return []
        notes = self._notify_waiting()
        for project in self.c.orchestrator.prioritized_projects():
            tasks = self.c.queue.list(project_id=project.id)
            if any(t.status in _OPEN for t in tasks):
                continue
            try:
                note = self._advance(project, tasks)
            except (ManagementError, ModelClientError, StructuredOutputError,
                    NoModelAvailable) as e:
                note = f"{project.name}: could not advance ({type(e).__name__}: {str(e)[:120]})"
                self.c.events.record(ACTOR, "Autopilot step failed", project.id, error=str(e)[:300])
            if note:
                notes.append(note)
        return notes

    # --- internals ---------------------------------------------------------------------

    def stages(self, project_id: str) -> int:
        return self.c.db.execute(
            "SELECT COUNT(*) AS n FROM events WHERE project_id = ? AND actor = 'PROJECT_MANAGER' "
            "AND action = 'Received plan proposal'", (project_id,)).fetchone()["n"]

    def _advance(self, project, tasks) -> str | None:
        o = self.c.orchestrator
        stages = self.stages(project.id)
        if stages >= self.c.config.max_stages_per_project:
            reason = f"stage limit reached ({stages} planned stages)"
            o.set_project_status(project.id, ProjectStatus.FINISHED, reason, actor=ACTOR)
            return self._tell(f"✔ **{project.name}** is finished: {reason}. "
                              f"{self._summary(project.id)} Tell me if you want a follow-up project.",
                              project.id)
        if stages > 0 and tasks:
            review = o.review_project(project.id)
            if review.applied and review.decision != "continue":
                verb = {"pause": "paused", "finish": "finished"}[review.decision]
                return self._tell(f"I {verb} **{project.name}**: {review.reason} "
                                  f"{self._summary(project.id)}"
                                  + (" Tell me how you want to continue." if verb == "paused" else ""),
                                  project.id)
        result = o.plan_project(project.id)
        if not result.accepted:
            o.set_project_status(project.id, ProjectStatus.PAUSED,
                                 "no usable next steps were planned", actor=ACTOR)
            why = "; ".join(r for _, r in result.rejected[:2]) or "the plan was empty"
            return self._tell(f"❓ I could not plan useful next steps for **{project.name}** "
                              f"({why}), so I paused it. What should we do next?", project.id)
        stage = self.stages(project.id)
        return self._tell(f"▶ **{project.name}** - stage {stage} planned: "
                          + "; ".join(t.description[:70] for t in result.accepted[:5])
                          + ". Working on it now.", project.id)

    def _notify_waiting(self) -> list[str]:
        notes = []
        for task in self.c.queue.list(status=TaskStatus.WAITING):
            if self.c.db.execute("SELECT 1 FROM owner_notices WHERE task_id = ?",
                                 (task.id,)).fetchone():
                continue
            project = self.c.projects.get(task.project_id)
            if (task.wait_reason or "").startswith("manual"):
                text = (f"🙋 I need you for **{project.name}**: {task.description}. When it is "
                        f"done, tell me what you did (e.g. the link or the result) and I will "
                        f"mark it complete. (task {task.id})")
            else:
                text = (f"💶 Your approval is needed for **{project.name}**: {task.description} "
                        f"- {task.wait_reason}. Approve it in Fleet -> Tasks, or tell me and confirm. "
                        f"(task {task.id})")
            self.c.db.execute("INSERT INTO owner_notices (task_id, ts) VALUES (?, ?)",
                              (task.id, utcnow().isoformat()))
            notes.append(self._tell(text, project.id))
        return notes

    def _summary(self, project_id: str) -> str:
        files = len(self.c.assets.list(project_id=project_id, status="DRAFT", limit=1000))
        done = len(self.c.queue.list(TaskStatus.COMPLETED, project_id))
        return (f"{done} tasks done" + (f", {files} files waiting for your review in Studio & Files."
                                        if files else "."))

    def _tell(self, text: str, project_id: str) -> str:
        """Post to the project's own chat (the main chat sees it in its snapshot)."""
        self.c.chat.post_update(text, thread=project_id)
        self.c.events.record(ACTOR, "Told owner", project_id, text=text[:300])
        return text
