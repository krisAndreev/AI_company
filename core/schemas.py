"""
Pydantic schemas for model output.

These describe what the AI is allowed to *propose*. Anything that fails
validation is rejected before the rest of the system ever sees it.

Note: IDs (task_id, project_id) are NOT produced by the model. Code assigns
them when a proposal is accepted, so the model cannot invent or overwrite
identifiers.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    # Reject unknown fields instead of silently ignoring them.
    model_config = ConfigDict(extra="forbid")


class TaskProposal(StrictModel):
    description: str = Field(min_length=5, max_length=500)
    department: str = Field(min_length=2, max_length=50,
                            description="e.g. research, marketing, development, content, "
                                        "design, data_analysis, finance, automation")
    priority: int = Field(ge=1, le=5, description="1 = highest, 5 = lowest")
    required_capabilities: list[str] = Field(default_factory=list, max_length=10)
    estimated_cost: float = Field(ge=0, description="Estimated cost in EUR")
    requires_approval: bool


class TaskPlan(StrictModel):
    tasks: list[TaskProposal] = Field(min_length=1, max_length=10)


class PlannedTask(TaskProposal):
    depends_on: list[int] = Field(
        default_factory=list, max_length=5,
        description="Numbers (1-based) of EARLIER tasks in this plan that must finish first")


class ProjectPlan(StrictModel):
    tasks: list[PlannedTask] = Field(min_length=1, max_length=10)


class Decision(StrictModel):
    decision: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    next_actions: list[str] = Field(default_factory=list, max_length=10)


class ProjectDecision(StrictModel):
    """The only decisions the orchestrator's AI may make about a project."""
    decision: Literal["continue", "pause", "finish"]
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    next_actions: list[str] = Field(default_factory=list, max_length=10)


class TaskResult(StrictModel):
    """What a worker returns after executing a task."""
    success: bool = Field(description="false if the task could not be done properly")
    summary: str = Field(min_length=1, max_length=500)
    output: str = Field(min_length=1, max_length=8000)


class LessonProposal(StrictModel):
    scope: Literal["project", "operational"]
    kind: Literal["lesson", "procedure", "fact"]
    content: str = Field(min_length=10, max_length=400)
    confidence: float = Field(ge=0.0, le=1.0)


class ImprovementIdea(StrictModel):
    target: str = Field(min_length=2, max_length=100,
                        description="what to change, e.g. config/company.json or 'worker prompt'")
    proposal: str = Field(min_length=10, max_length=500)
    rationale: str = Field(min_length=10, max_length=500)


class LearningReport(StrictModel):
    lessons: list[LessonProposal] = Field(default_factory=list, max_length=5)
    improvements: list[ImprovementIdea] = Field(default_factory=list, max_length=3)


ActionType = Literal["create_project", "plan_project", "review_project", "pause_project",
                     "resume_project", "finish_project", "add_task", "start_work", "pause_work",
                     "complete_task", "approve_task", "record_metric", "add_guideline"]


class ProposedAction(StrictModel):
    """An action the orchestrator decides on in chat. Code validates it; safe actions run
    at once in autopilot mode, money / publishing / guidelines wait for the owner."""
    type: ActionType
    reason: str = Field(min_length=1, max_length=300)
    project_id: str = Field(default="", max_length=40, description="existing project id")
    name: str = Field(default="", max_length=200, description="create_project only")
    objective: str = Field(default="", max_length=2000, description="create_project only")
    budget_eur: float = Field(default=0, ge=0, description="create_project only")
    priority: int = Field(default=3, ge=1, le=5, description="create_project / add_task")
    department: str = Field(default="", max_length=50, description="add_task only")
    capability: str = Field(default="", max_length=60,
                            description="add_task only: one capability of that department")
    description: str = Field(default="", max_length=500, description="add_task only")
    task_id: str = Field(default="", max_length=40,
                         description="complete_task / approve_task: existing task id")
    result: str = Field(default="", max_length=1500,
                        description="complete_task: what the owner did / the outcome")
    metric: str = Field(default="", max_length=60, description="record_metric only")
    value: float = Field(default=0, description="record_metric only")
    content: str = Field(default="", max_length=400, description="add_guideline only")


class OrchestratorReply(StrictModel):
    # Field order matters for small models: think first (private), then answer briefly.
    thinking: str = Field(default="", max_length=2000,
                          description="your private reasoning; the owner does not see it")
    reply: str = Field(min_length=1, max_length=3000,
                       description="what the owner reads: 1-4 short sentences")
    actions: list[ProposedAction] = Field(default_factory=list, max_length=4)


class ExperimentDecision(StrictModel):
    """The only decisions the AI may make at an experiment checkpoint."""
    decision: Literal["continue", "pivot", "stop"]
    reason: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    new_direction: str = Field(default="", max_length=500,
                               description="required when decision is pivot")

    @model_validator(mode="after")
    def _pivot_needs_direction(self):
        if self.decision == "pivot" and not self.new_direction.strip():
            raise ValueError("new_direction is required when decision is 'pivot'")
        return self
