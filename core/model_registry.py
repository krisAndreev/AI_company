"""
Model registry: which models exist and what they can do.

Loaded from config/models.json and validated on load, so a broken config
fails immediately instead of causing strange behaviour later.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Preference = Literal["fast", "quality", "cheap"]


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str                 # must match a registered ModelClient, e.g. "ollama"
    model: str                    # provider's model name, e.g. "gemma3:4b"
    capabilities: list[str]
    quality: int = Field(ge=1, le=5)
    typical_latency_seconds: float = Field(gt=0)
    cost_per_1k_input_tokens: float = Field(ge=0)   # EUR
    cost_per_1k_output_tokens: float = Field(ge=0)  # EUR
    local: bool
    uses_gpu: bool
    enabled: bool = True
    think: bool | None = None     # reasoning models: hidden thinking on/off (None = default)

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.cost_per_1k_input_tokens
                + output_tokens * self.cost_per_1k_output_tokens) / 1000


class TaskProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required_capabilities: list[str]
    prefer: Preference = "fast"


class ModelRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: dict[str, ModelSpec]
    task_profiles: dict[str, TaskProfile]

    @model_validator(mode="after")
    def _check(self):
        if "default" not in self.task_profiles:
            raise ValueError("task_profiles must contain 'default'")
        return self

    @classmethod
    def load(cls, path: str | Path = "config/models.json") -> "ModelRegistry":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, key: str) -> ModelSpec:
        if key not in self.models:
            raise KeyError(f"unknown model key: {key}")
        return self.models[key]

    def profile(self, task_type: str) -> TaskProfile:
        if task_type not in self.task_profiles:
            raise ValueError(f"unknown task type: {task_type!r} "
                             f"(known: {', '.join(self.task_profiles)})")
        return self.task_profiles[task_type]
