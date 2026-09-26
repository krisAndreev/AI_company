"""
Temporary workers (agents).

A Worker is created for ONE task, executes it with a routed model, and is then
discarded. Its life is recorded in the `agents` table for auditing.
Workers only produce text/structured results. They cannot run code, call URLs,
spend money or publish: for tool-backed capabilities (web research, products,
images, videos, campaigns) the model proposes the tool's parameters and CODE runs
the allowlisted tool, within the project's budget.
"""

import uuid

from core.db import Database
from core.departments import Department
from core.projects import Project
from core.research import RESEARCH_CAPABILITIES, RESEARCH_METHOD, RESEARCH_SCALE
from core.router import ModelRouter, RouteRequest
from core.schemas import TaskResult
from core.structured import StructuredResult, generate_structured
from core.tasks import Task, utcnow
from core.tools import ToolContext

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    department  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    status      TEXT NOT NULL,     -- RUNNING, COMPLETED, FAILED
    model_key   TEXT,
    tokens      INTEGER NOT NULL DEFAULT 0,
    cost_eur    REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT
);
"""


class WorkerStore:
    def __init__(self, db: Database):
        self.db = db
        self.db.executescript(_SCHEMA)

    def started(self, worker_id: str, department: str, task: Task) -> None:
        self.db.execute(
            "INSERT INTO agents (id, department, task_id, project_id, status, created_at) "
            "VALUES (?, ?, ?, ?, 'RUNNING', ?)",
            (worker_id, department, task.id, task.project_id, utcnow().isoformat()))

    def finished(self, worker_id: str, status: str, model_key: str | None = None,
                 tokens: int = 0, cost_eur: float = 0.0, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE agents SET status = ?, model_key = ?, tokens = ?, cost_eur = ?, "
            "finished_at = ?, error = ? WHERE id = ?",
            (status, model_key, tokens, cost_eur, utcnow().isoformat(), error, worker_id))

    def get(self, worker_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM agents WHERE id = ?", (worker_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM agents ORDER BY created_at DESC LIMIT ?", (limit,))]


def new_worker_id() -> str:
    return "worker_" + uuid.uuid4().hex[:8]


class Worker:
    def __init__(self, worker_id: str, department_name: str, department: Department,
                 router: ModelRouter, project: Project, max_call_cost_eur: float,
                 tools=None, research=None, memory_context: str = "", company=None,
                 budget_remaining: float | None = None):
        self.id = worker_id
        self.department_name = department_name
        self.department = department
        self.router = router
        self.project = project
        self.max_call_cost_eur = max_call_cost_eur
        self.tools = tools          # ToolRegistry or None
        self.research = research    # ResearchStore or None
        self.memory_context = memory_context
        self.company = company      # gives tools the workspace / asset store
        self.budget_remaining = budget_remaining
        self.assets: list[dict] = []   # files the task's tools produced (code facts)
        self.tool_calls = 0
        self.spent = 0.0               # EUR spent by tools so far

    def execute(self, task: Task, context: str) -> tuple[StructuredResult[TaskResult], str, float]:
        """Run the task with a routed model. Returns (result, model_key, cost in EUR).
        Raises ToolError if a required tool call is refused or fails."""
        route = self.router.select(RouteRequest(
            task_type=self.department.task_type,
            required_capabilities=["structured_output"],
            allow_cloud="cloud_models" in self.project.permissions,
            max_cost_eur=self.max_call_cost_eur,
        ))
        tool_data, cost = self._use_tools(task, route, context)
        used_tools = self.tool_calls > 0
        method = (f"Research method (owner rule - always use it): {RESEARCH_METHOD}\n"
                  f"{RESEARCH_SCALE}\n\n" if self._is_research(task) else "")
        prompt = (
            f"YOUR TASK (do only this): {task.description}\n"
            + method
            + f"Capabilities you may use: {', '.join(task.required_capabilities) or 'none'}\n\n"
            + (f"Results of earlier tasks you can build on:\n{context}\n\n" if context else "")
            + (f"Data collected / files produced by tools for this task:\n{tool_data}\n\n"
               if tool_data else "")
            + (f"Relevant memory:\n{self.memory_context}\n\n" if self.memory_context else "")
            + f"Background - the overall project goal (NOT your job to achieve it alone, "
              f"other tasks handle the rest):\n{self.project.objective}\n\n"
            + ("The tools above already did the work (searched the web / produced the files). "
               "Report on it in `output`: what was found or made, key points, and what the owner "
               "should review. Only state facts shown above. "
               if used_tools else
               "Produce the deliverable for YOUR TASK now and put it in `output`. You cannot "
               "browse the internet, spend money or publish anything: use your own knowledge "
               "and state assumptions. ")
            + "success = true if you produced a useful deliverable for this task; false only if "
              "this specific task is impossible without tools you do not have."
        )
        result = generate_structured(
            route.client, route.model, prompt, TaskResult,
            system=f"You are a worker in the {self.department_name} department. "
                   f"{self.department.description}",
            think=route.spec.think,
        )
        cost += sum(route.spec.estimate_cost(r.prompt_tokens, r.completion_tokens)
                    for r in result.responses)
        return result, route.model_key, cost

    def _is_research(self, task: Task) -> bool:
        return (self.department_name == "research"
                or bool(RESEARCH_CAPABILITIES & set(task.required_capabilities)))

    def _use_tools(self, task: Task, route, context: str = "") -> tuple[str, float]:
        """For each tool-backed capability: the model proposes parameters, code validates
        and runs the tool, results are stored with source + timestamp. Tools that make
        their own model proposals (products, campaigns, research) get them through a
        ToolContext; their model cost is added to the task."""
        if not self.tools:
            return "", 0.0
        blocks, cost = [], 0.0
        for cap in task.required_capabilities:
            spec = self.department.capabilities.get(cap)
            if not (spec and spec.tool and self.tools.available(spec.tool)):
                continue
            tool = self.tools.tools[spec.tool]
            proposal = generate_structured(
                route.client, route.model,
                f"Task: {task.description}\n"
                + (f"Research method (owner rule): {RESEARCH_METHOD}\n"
                   if self._is_research(task) else "")
                + (f"Earlier results to build on:\n{context[:3000]}\n" if context else "")
                + f"Project goal: {self.project.objective[:600]}\n"
                f"Choose the parameters for the tool '{tool.name}': {tool.config.description}",
                tool.Params, think=route.spec.think,
            )
            cost += sum(route.spec.estimate_cost(r.prompt_tokens, r.completion_tokens)
                        for r in proposal.responses)
            ctx = ToolContext(company=self.company, project_id=self.project.id, task_id=task.id,
                              route=route, context=context, memory=self.memory_context,
                              budget_remaining=(None if self.budget_remaining is None
                                                else self.budget_remaining - cost))
            output, call_cost = self.tools.call(spec.tool, proposal.value.model_dump(),
                                                self.project.id, task.id, ctx)
            self.tool_calls += 1
            cost += call_cost + ctx.model_cost
            self.spent = cost           # kept even if a later step fails
            self.assets += output.assets
            if self.research is not None and output.observations:
                self.research.add_many(output.observations, task.id)
            if output.text:
                blocks.append(f"{tool.name}:\n{output.text}")
            elif self.research is not None and output.observations:
                blocks.append(self.research.compare_text(output.keyword or ""))
            else:
                blocks.append(f"{tool.name}: {output.summary}")
        return "\n".join(blocks), cost
