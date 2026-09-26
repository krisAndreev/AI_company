"""
Departments: reusable, shared across all projects (never copied per project).

Each department offers capabilities. A capability is either automated (a worker
can do it with a model) or manual (a human - or a future tool - must do it).
Some capabilities also need a project permission, e.g. "publishing" or "paid_ads".
"""

import json
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from core.schemas import PlannedTask, StrictModel


class CapabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    automated: bool
    requires_permission: str | None = None
    tool: str | None = None  # if this tool is available, workers can do it automatically


class Department(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    task_type: str  # model-router profile used by this department's workers
    capabilities: dict[str, CapabilitySpec] = Field(min_length=1)


class DepartmentRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    departments: dict[str, Department] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_capabilities(self):
        seen = {}
        for dept_name, dept in self.departments.items():
            for cap in dept.capabilities:
                if cap in seen:
                    raise ValueError(f"capability {cap!r} in both {seen[cap]} and {dept_name}")
                seen[cap] = dept_name
        return self

    @classmethod
    def load(cls, path: str | Path = "config/departments.json") -> "DepartmentRegistry":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    def names(self) -> list[str]:
        return list(self.departments)

    def get(self, name: str) -> Department | None:
        return self.departments.get(name)

    def find_provider(self, capability: str) -> str | None:
        """Which department offers this capability?"""
        for name, dept in self.departments.items():
            if capability in dept.capabilities:
                return name
        return None

    def describe(self, tool_available: Callable[[str], bool] | None = None) -> str:
        """Department list for planning prompts. Manual capabilities are marked; ones
        backed by an available tool are marked (tool) with what the tool really does."""
        lines = []
        for name, dept in self.departments.items():
            caps = []
            for c, s in dept.capabilities.items():
                if s.tool and tool_available and tool_available(s.tool):
                    caps.append(f"{c} (tool: {s.description})")
                else:
                    caps.append(c + ("" if s.automated else " (manual)"))
            lines.append(f"- {name}: {dept.description} Capabilities: {', '.join(caps)}")
        return "\n".join(lines)

    def plan_schema(self) -> type[BaseModel]:
        """A ProjectPlan schema whose department/capability fields only allow real names.
        Sent to the model so constrained decoding cannot invent departments."""
        dept_names = Literal[tuple(self.departments)]
        cap_names = Literal[tuple(c for d in self.departments.values() for c in d.capabilities)]
        task = create_model(
            "PlannedTask", __base__=PlannedTask,
            department=(dept_names, ...),
            required_capabilities=(list[cap_names], Field(default_factory=list, max_length=5)),
        )
        return create_model("ProjectPlan", __base__=StrictModel,
                            tasks=(list[task], Field(min_length=1, max_length=10)))
