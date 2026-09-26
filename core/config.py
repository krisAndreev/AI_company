"""
Company-level limits, loaded from config/company.json and validated on load.
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class OrchestratorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_budget_eur: float = Field(ge=0)
    max_active_projects: int = Field(ge=1)
    min_decision_confidence: float = Field(ge=0, le=1)
    max_tasks_per_plan: int = Field(ge=1, le=10)
    max_task_retries: int = Field(ge=0, le=10)
    brand_name: str = Field(default="", max_length=60)      # shown on products and graphics
    # Autopilot: the orchestrator acts on its own decisions (create/plan projects, add
    # tasks, start work) and advances projects stage by stage while the loop runs.
    # Money, publishing, finishing projects and owner guidelines still need the owner.
    autopilot: bool = True
    autopilot_max_project_budget_eur: float = Field(default=25, ge=0)  # above: owner confirms
    max_stages_per_project: int = Field(default=6, ge=1, le=50)
    autopilot_interval_seconds: float = Field(default=20, ge=1, le=3600)
    # Ask Windows not to sleep while tasks are being worked on; normal sleep otherwise.
    keep_awake_while_working: bool = True
    keep_awake_grace_minutes: float = Field(default=3, ge=0, le=120)  # after the last work
    workspace_dir: str = Field(default="workspace", min_length=1, max_length=200)
    backup_keep: int = Field(default=14, ge=1, le=365)       # daily database backups kept

    @classmethod
    def load(cls, path: str | Path = "config/company.json") -> "OrchestratorConfig":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
