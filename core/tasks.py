"""
Task model and the rules for how a task's status may change.

Status meanings:
  PENDING    waiting for dependencies to complete
  READY      can be picked up by a worker
  RUNNING    a worker is executing it
  WAITING    paused on something external (e.g. human approval)
  COMPLETED  finished successfully            (terminal)
  FAILED     finished with an error           (can be retried)
  CANCELLED  stopped on purpose               (terminal)
"""

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# The only status changes code will perform. Anything else is refused.
ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING:   {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.READY:     {TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING:   {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.WAITING,
                           TaskStatus.CANCELLED},
    TaskStatus.WAITING:   {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.FAILED:    {TaskStatus.READY},  # retry
    TaskStatus.COMPLETED: set(),
    TaskStatus.CANCELLED: set(),
}


class InvalidTransition(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    project_id: str
    department: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = Field(ge=1, le=5)  # 1 = highest
    dependencies: list[str] = Field(default_factory=list)
    assigned_worker: str | None = None
    attempts: int = Field(default=0, ge=0)  # how many times the task has been started
    required_capabilities: list[str] = Field(default_factory=list)
    estimated_cost: float = Field(default=0.0, ge=0)
    actual_cost: float = Field(default=0.0, ge=0)
    requires_approval: bool = False
    approved_by: str | None = None  # who approved spending/flagged work (HUMAN, ...)
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: str | None = None
    error: str | None = None
    wait_reason: str | None = None

    def check_transition(self, new_status: TaskStatus) -> None:
        if new_status not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidTransition(
                f"task {self.id}: {self.status.value} -> {new_status.value} is not allowed"
            )
