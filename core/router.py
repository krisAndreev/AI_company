"""
Model router: pick the best allowed model for a request.

Deterministic code, not AI. Every decision records why each other model
was rejected, so routing is auditable.
"""

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from clients import ModelClient, ModelClientError
from core.model_registry import ModelRegistry, ModelSpec, Preference


class NoModelAvailable(Exception):
    def __init__(self, message: str, rejected: dict[str, str]):
        super().__init__(message + "; " + "; ".join(f"{k}: {v}" for k, v in rejected.items()))
        self.rejected = rejected


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: str = "default"
    required_capabilities: list[str] = Field(default_factory=list)  # added to the profile's
    prefer: Preference | None = None       # overrides the profile's preference
    allow_cloud: bool = False              # permission: may this request use non-local models?
    max_cost_eur: float | None = None      # budget limit for this single call
    est_input_tokens: int = Field(default=1000, ge=0)
    est_output_tokens: int = Field(default=500, ge=0)


@dataclass
class RouteDecision:
    model_key: str
    spec: ModelSpec
    client: ModelClient
    estimated_cost: float
    reason: str
    rejected: dict[str, str] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return self.spec.model


class ModelRouter:
    def __init__(self, registry: ModelRegistry, clients: dict[str, ModelClient]):
        self.registry = registry
        self.clients = clients  # provider name -> client; credentials stay inside clients

    def select(self, request: RouteRequest) -> RouteDecision:
        profile = self.registry.profile(request.task_type)
        needed = set(profile.required_capabilities) | set(request.required_capabilities)
        prefer = request.prefer or profile.prefer
        installed = self._installed_by_provider()

        candidates, rejected = [], {}
        for key, spec in self.registry.models.items():
            cost = spec.estimate_cost(request.est_input_tokens, request.est_output_tokens)
            problem = self._reject_reason(spec, needed, request, installed, cost)
            if problem:
                rejected[key] = problem
            else:
                candidates.append((key, spec, cost))

        if not candidates:
            raise NoModelAvailable(
                f"no model for task_type={request.task_type!r} needing {sorted(needed)}", rejected)

        sort_keys = {
            "fast":    lambda c: (c[1].typical_latency_seconds, c[2], -c[1].quality),
            "quality": lambda c: (-c[1].quality, c[2], c[1].typical_latency_seconds),
            "cheap":   lambda c: (c[2], c[1].typical_latency_seconds, -c[1].quality),
        }
        candidates.sort(key=sort_keys[prefer])
        key, spec, cost = candidates[0]
        for other_key, _, _ in candidates[1:]:
            rejected[other_key] = f"eligible but ranked lower (prefer={prefer})"

        return RouteDecision(
            model_key=key, spec=spec, client=self.clients[spec.provider], estimated_cost=cost,
            reason=f"task_type={request.task_type}, needs {sorted(needed)}, prefer={prefer}: "
                   f"chose {key} ({spec.model}, quality {spec.quality}, "
                   f"~{spec.typical_latency_seconds:g}s, est EUR {cost:.4f})",
            rejected=rejected,
        )

    def status(self) -> list[dict]:
        """One row per registered model: is it usable right now?"""
        installed = self._installed_by_provider()
        rows = []
        for key, spec in self.registry.models.items():
            models = installed.get(spec.provider)
            rows.append({
                "key": key, "provider": spec.provider, "model": spec.model,
                "enabled": spec.enabled,
                "provider_available": models is not None,
                "installed": models is not None and spec.model in models,
            })
        return rows

    # --- internals ----------------------------------------------------------

    def _reject_reason(self, spec, needed, request, installed, cost) -> str | None:
        if not spec.enabled:
            return "disabled in config"
        missing = needed - set(spec.capabilities)
        if missing:
            return f"missing capabilities {sorted(missing)}"
        if not spec.local and not request.allow_cloud:
            return "cloud models not permitted for this request"
        if request.max_cost_eur is not None and cost > request.max_cost_eur:
            return f"estimated cost EUR {cost:.4f} exceeds limit EUR {request.max_cost_eur:.4f}"
        if spec.provider not in self.clients:
            return f"no client registered for provider {spec.provider!r}"
        if installed.get(spec.provider) is None:
            return f"provider {spec.provider!r} not reachable"
        if spec.model not in installed[spec.provider]:
            return f"model {spec.model!r} not installed"
        return None

    def _installed_by_provider(self) -> dict[str, set[str] | None]:
        """provider -> installed model names, or None if the provider is unreachable."""
        result = {}
        for name, client in self.clients.items():
            try:
                result[name] = set(client.list_models())
            except ModelClientError:
                result[name] = None
        return result
