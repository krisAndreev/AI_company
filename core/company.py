"""
Company: builds and wires every part of the system from the config folder.

    company = Company()                 # real database in data/, log in logs/, files in workspace/
    company = Company(":memory:", ...)  # throwaway, for tests (files go to a temp folder)
"""

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from clients import ModelClient
from core.autopilot import Autopilot
from core.chat import OrchestratorChat
from core.config import OrchestratorConfig
from core.db import Database
from core.departments import DepartmentRegistry
from core.events import EventLog
from core.experiments import ExperimentManager
from core.learning import LearningEngine
from core.memory import ImprovementStore, MemoryStore
from core.model_registry import ModelRegistry
from core.orchestrator import MasterOrchestrator
from core.permissions import ApprovalPolicy, ExpenseLedger, PermissionGate
from core.projects import ProjectStore
from core.research import ResearchStore
from core.resources import NodePool, NodesConfig
from core.router import ModelRouter, RouteRequest
from core.runner import Runner
from core.task_queue import TaskQueue
from core.tools import ToolContext, ToolRegistry, ToolsConfig, check_tools_config
from core.project_manager import ManagementError
from core.tasks import TaskStatus
from core.web import WebSourceStore
from core.workers import WorkerStore
from core.workspace import AssetStore, Workspace


class Company:
    def __init__(self, db_path: str | Path = "data/company.db",
                 clients: dict[str, ModelClient] | None = None,
                 log_path: str | Path | None = "logs/company.log",
                 config_dir: str | Path = "config",
                 http_get: Callable | None = None,
                 workspace_dir: str | Path | None = None):
        cfg = Path(config_dir)
        self.config = OrchestratorConfig.load(cfg / "company.json")
        self.departments = DepartmentRegistry.load(cfg / "departments.json")
        self.policy = ApprovalPolicy.load(cfg / "policy.json")
        tools_config = ToolsConfig.load(cfg / "tools.json")
        registry = ModelRegistry.load(cfg / "models.json")
        self.nodes_config = NodesConfig.load(cfg / "nodes.json")
        check_config(self.departments, self.policy, registry, tools_config)
        if clients is None:  # real run: model calls go through the multi-node pool
            clients = {"ollama": NodePool(self.nodes_config)}
        pool = clients.get("ollama")
        self.resources = pool if isinstance(pool, NodePool) else None

        self.db = Database(db_path)
        self.events = EventLog(self.db, log_path)
        if workspace_dir is None and str(db_path) == ":memory:":
            # throwaway company (tests, checks): never write into the real workspace
            workspace_dir = tempfile.mkdtemp(prefix="aic_workspace_")
        self.workspace = Workspace(workspace_dir or self.config.workspace_dir)
        self.assets = AssetStore(self.db, self.workspace)
        self.web_sources = WebSourceStore(self.db)
        self.projects = ProjectStore(self.db)
        self.workspace.project_info = lambda pid: (
            (p := self.projects.get(pid)).name, p.created_at.date().isoformat())
        self.queue = TaskQueue(self.db)
        self.ledger = ExpenseLedger(self.db)
        self.workers = WorkerStore(self.db)
        self.memory = MemoryStore(self.db)
        self.improvements = ImprovementStore(self.db)
        self.research = ResearchStore(self.db)
        self.tools = ToolRegistry(tools_config, self.db, http_get)
        self.router = ModelRouter(registry, clients)
        self.gate = PermissionGate(self.policy, self.departments, self.tools)
        self.orchestrator = MasterOrchestrator(self.projects, self.queue, self.events,
                                               self.router, self.config, self.departments,
                                               self.policy, self.memory, self.tools)
        self.runner = Runner(self)
        self.experiments = ExperimentManager(self)
        self.learning = LearningEngine(self)
        self.chat = OrchestratorChat(self)
        self.autopilot = Autopilot(self)
        self.control = None   # work-loop control (start/pause/status), set by the dashboard

    def tool_context(self, project_id: str | None, task_type: str = "writing",
                     allow_cloud: bool = False, needs_model: bool = True) -> ToolContext:
        """Context for a tool run started by a human from the dashboard (no task).
        Model proposals go through the normal router; the budget check uses the
        project's remaining budget."""
        route = self.router.select(RouteRequest(
            task_type=task_type, required_capabilities=["structured_output"],
            allow_cloud=allow_cloud)) if needs_model else None
        budget = self.orchestrator.budget(project_id)["remaining"] if project_id else None
        memory = self.memory.context_for(project_id) if project_id else ""
        return ToolContext(company=self, project_id=project_id, route=route, memory=memory,
                           budget_remaining=budget)

    def run_tool(self, name: str, params: dict, project_id: str | None,
                 actor: str = "HUMAN") -> dict:
        """A human-requested tool run (dashboard Studio). Same validation, limits,
        budget check and logging as a worker's tool call; costs go to the ledger."""
        if project_id is not None:
            self.projects.get(project_id)            # unknown project -> KeyError
        task_type = "analysis" if name == "web_research" else "writing"
        tool = self.tools.tools.get(name)
        ctx = self.tool_context(project_id, task_type,
                                needs_model=bool(tool and tool.needs_model))
        self.events.record(actor, f"Started {name}", project_id)
        output, cost = self.tools.call(name, params, project_id, None, ctx)
        total = cost + ctx.model_cost
        if total > 0 and project_id:
            self.ledger.record(project_id, total, "tool_call", f"{name} (studio)",
                               approved_by=actor)
        if project_id and output.observations:
            self.research.add_many(output.observations)
        return {"summary": output.summary, "assets": [a["id"] for a in output.assets],
                "cost_eur": round(total, 4), "model_calls": ctx.model_calls,
                "text": output.text[:3000]}

    def delete_project(self, project_id: str, who: str = "HUMAN") -> dict:
        """Owner-only: remove a project with its tasks, chat, files, research and memory.
        The event log, expenses and tool-call audit stay (history of money and actions)."""
        if who != "HUMAN":
            raise ManagementError("only the owner can delete a project")
        p = self.projects.get(project_id)
        tasks = self.queue.list(project_id=project_id)
        if any(t.status == TaskStatus.RUNNING for t in tasks):
            raise ManagementError("a task of this project is running right now: pause the "
                                  "project, wait for the task to finish, then delete it")
        files = self.assets.list(project_id=project_id, limit=100_000)
        with self.db.transaction():
            for t in tasks:
                self.db.execute("DELETE FROM agents WHERE task_id = ?", (t.id,))
                self.db.execute("DELETE FROM owner_notices WHERE task_id = ?", (t.id,))
            for exp in self.db.execute("SELECT id FROM experiments WHERE project_id = ?",
                                       (project_id,)).fetchall():
                self.db.execute("DELETE FROM metrics WHERE experiment_id = ?", (exp["id"],))
            for sql in ("DELETE FROM tasks WHERE project_id = ?",
                        "DELETE FROM experiments WHERE project_id = ?",
                        "DELETE FROM memory WHERE scope = 'project' AND project_id = ?",
                        "DELETE FROM chat_messages WHERE thread = ?",
                        "DELETE FROM assets WHERE project_id = ?",
                        "DELETE FROM web_sources WHERE project_id = ?",
                        "DELETE FROM projects WHERE id = ?"):
                self.db.execute(sql, (project_id,))
        folder = self.workspace.project_dir(project_id, create=False)
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        for a in files:
            thumb = self.assets.thumb_path(a["id"])
            if thumb:
                thumb.unlink(missing_ok=True)
        self.events.record(who, "Deleted project", project_id, name=p.name, tasks=len(tasks),
                           files=len(files))
        return {"deleted": p.name, "tasks": len(tasks), "files": len(files)}

    def close(self) -> None:
        self.db.close()


def check_config(departments: DepartmentRegistry, policy: ApprovalPolicy,
                 registry: ModelRegistry, tools_config: ToolsConfig) -> None:
    """Cross-file consistency: fail at startup (or when saving config), not mid-run."""
    check_tools_config(tools_config)
    for name, dept in departments.departments.items():
        if dept.task_type not in registry.task_profiles:
            raise ValueError(f"department {name}: unknown task_type {dept.task_type!r}")
        for cap, spec in dept.capabilities.items():
            if spec.requires_permission and spec.requires_permission not in policy.known_permissions:
                raise ValueError(f"capability {cap}: unknown permission "
                                 f"{spec.requires_permission!r}")
            if spec.tool and (spec.tool not in tools_config.tools
                              or tools_config.tools[spec.tool].capability != cap):
                raise ValueError(f"capability {cap}: tool {spec.tool!r} missing or "
                                 f"registered for another capability")
