"""
External tools, behind an allowlist.

- Tools are enabled only in config/tools.json and implement ONE department capability.
- The model never sees credentials: API keys are read from environment variables
  inside the tool, at call time.
- The model may only propose parameters; they are validated against the tool's
  Pydantic schema before anything runs. Tool-specific settings in tools.json are
  validated against the tool's Settings schema when the config is loaded.
- HTTP goes through core.net: allowlisted API hosts only (https except local/LAN
  services); web research reads public pages only (no private addresses).
- A tool that needs several model proposals (e.g. a product: outline, then pages)
  gets them through ToolContext.generate(): every proposal is schema-validated,
  and its cost is added to the task.
- Paid tools are refused BEFORE running when their worst-case cost exceeds the
  project's remaining budget. Every call (ok, error or refused) is logged in the
  tool_calls table.
"""

import json
import os
import statistics
import time
import urllib.parse
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core import net
from core.db import Database
from core.net import ToolError
from core.research import MarketObservation
from core.schemas import StrictModel
from core.structured import generate_structured
from core.tasks import utcnow

__all__ = ["Tool", "ToolConfig", "ToolContext", "ToolError", "ToolOutput", "ToolRegistry",
           "ToolsConfig", "safe_get_json", "tool_classes"]


class ToolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    description: str
    capability: str
    cost_per_call_eur: float = Field(ge=0)
    max_calls_per_day: int = Field(ge=0)
    allowed_hosts: list[str]
    api_key_env: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)  # checked by the tool's Settings


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: dict[str, ToolConfig]

    @classmethod
    def load(cls, path: str | Path = "config/tools.json") -> "ToolsConfig":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def safe_get_json(url: str, params: dict, headers: dict, allowed_hosts: list[str],
                  timeout: float = 20) -> dict:
    """GET JSON from an allowlisted https API (kept for the marketplace tools)."""
    if urllib.parse.urlparse(url).scheme != "https":
        raise ToolError(f"refused non-https URL: {url}")
    return net.request("GET", url, allowed_hosts, params=params, headers=headers,
                       timeout=timeout)


class ToolOutput(BaseModel):
    summary: dict
    observations: list[MarketObservation] = Field(default_factory=list)
    keyword: str | None = None
    assets: list[dict] = Field(default_factory=list)   # rows from the asset store
    cost_eur: float | None = Field(default=None, ge=0)  # None = config cost_per_call_eur
    text: str = ""                                     # extra context for the worker prompt


@dataclass
class ToolContext:
    """What CODE gives a tool at run time. Nothing here comes from the model."""
    company: Any = None                 # Company (workspace, assets, events, router, ...)
    project_id: str | None = None
    task_id: str | None = None
    route: Any = None                   # RouteDecision used for the tool's model proposals
    context: str = ""                   # results of earlier tasks
    memory: str = ""
    budget_remaining: float | None = None
    model_cost: float = 0.0             # accumulated by generate()
    tokens: int = 0
    model_calls: int = 0
    notes: list[str] = field(default_factory=list)

    def generate(self, prompt: str, schema: type[BaseModel], system: str | None = None,
                 temperature: float = 0.3, constrain_to: type[BaseModel] | None = None,
                 max_attempts: int = 3):
        """One validated model proposal. Raises StructuredOutputError if none is valid."""
        if self.route is None:
            raise ToolError("this tool needs a model, but none was routed")
        result = generate_structured(self.route.client, self.route.model, prompt, schema,
                                     system=system, temperature=temperature,
                                     think=self.route.spec.think, constrain_to=constrain_to,
                                     max_attempts=max_attempts)
        self.model_cost += sum(self.route.spec.estimate_cost(r.prompt_tokens, r.completion_tokens)
                               for r in result.responses)
        self.tokens += result.total_tokens
        self.model_calls += len(result.responses)
        return result.value

    def log(self, actor: str, action: str, **details) -> None:
        if self.company is not None:
            self.company.events.record(actor, action, self.project_id, self.task_id, **details)


class Tool(ABC):
    name: str
    Params: type[BaseModel]
    Settings: type[BaseModel] = StrictModel   # tool-specific settings in tools.json
    needs_model: bool = False                 # True: run() makes model proposals via ctx

    def __init__(self, config: ToolConfig, http_get: Callable = safe_get_json):
        self.config = config
        self.http_get = http_get
        self.settings = self.Settings.model_validate(config.settings)

    def availability(self) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "disabled in config"
        if self.config.api_key_env and not os.environ.get(self.config.api_key_env):
            return False, f"environment variable {self.config.api_key_env} not set"
        return True, "ok"

    def max_cost(self, params: BaseModel, ctx: "ToolContext | None" = None) -> float:
        """Worst-case cost of one call with these params (checked against the budget)."""
        return self.config.cost_per_call_eur

    def _api_key(self) -> str:
        return os.environ.get(self.config.api_key_env or "", "")

    def _get(self, url: str, params: dict, headers: dict) -> dict:
        return self.http_get(url, params, headers, self.config.allowed_hosts)

    @abstractmethod
    def run(self, params: BaseModel, ctx: ToolContext) -> ToolOutput: ...


# --- Etsy -------------------------------------------------------------------------------

class EtsySearchParams(StrictModel):
    keywords: str = Field(min_length=2, max_length=80, description="search phrase")
    limit: int = Field(default=50, ge=1, le=100, description="listings to sample")


class EtsyListingsTool(Tool):
    """Etsy Open API v3: active listings for a keyword -> competition and listing signals.
    Needs an Etsy developer key in ETSY_API_KEY. Numbers from the top-N sample are marked
    as estimates of the whole market."""
    name = "etsy_listings"
    Params = EtsySearchParams
    URL = "https://openapi.etsy.com/v3/application/listings/active"

    def run(self, params: EtsySearchParams, ctx: ToolContext | None = None) -> ToolOutput:
        data = self._get(self.URL, {"keywords": params.keywords, "limit": params.limit,
                                    "sort_on": "score"}, {"x-api-key": self._api_key()})
        results = data.get("results") or []
        now = time.time()
        prices = [r["price"]["amount"] / r["price"]["divisor"] for r in results
                  if isinstance(r.get("price"), dict) and r["price"].get("divisor")]
        views = [r["views"] for r in results if isinstance(r.get("views"), (int, float))]
        favs = [r["num_favorers"] for r in results
                if isinstance(r.get("num_favorers"), (int, float))]
        ages = [(now - r["original_creation_timestamp"]) / 86400 for r in results
                if isinstance(r.get("original_creation_timestamp"), (int, float))]
        n = len(results)
        sample_note = f"from top {n} listings"
        obs = []

        def add(metric, value, unit, estimate, note=""):
            obs.append(MarketObservation(keyword=params.keywords, metric=metric,
                                         value=round(value, 2), unit=unit, source="etsy_api",
                                         is_estimate=estimate, note=note))

        if isinstance(data.get("count"), (int, float)):
            add("competition_listings", data["count"], "listings", False, "Etsy-reported count")
        if prices:
            add("median_price", statistics.median(prices), results[0]["price"].get(
                "currency_code", ""), True, sample_note)
        if views:
            add("avg_views_per_listing", statistics.mean(views), "views", True, sample_note)
        if favs:
            add("avg_favorites_per_listing", statistics.mean(favs), "favorites", True, sample_note)
        if ages:
            add("median_listing_age_days", statistics.median(ages), "days", True, sample_note)
        return ToolOutput(keyword=params.keywords, observations=obs,
                          summary={"sampled_listings": n, "total_count": data.get("count")})


def tool_classes() -> dict[str, type[Tool]]:
    """Every tool the code implements. Imported lazily: the feature modules import this one."""
    from core.campaigns import CampaignTool
    from core.imagegen import ImageTool
    from core.products import ProductTool
    from core.video import VideoTool
    from core.web import WebResearchTool
    return {"etsy_listings": EtsyListingsTool, "web_research": WebResearchTool,
            "product_builder": ProductTool, "image_studio": ImageTool,
            "video_studio": VideoTool, "campaign_builder": CampaignTool}


def check_tools_config(config: ToolsConfig) -> None:
    classes = tool_classes()
    for name, cfg in config.tools.items():
        if name not in classes:
            raise ValueError(f"tools.json: no implementation for tool {name!r}")
        try:
            classes[name].Settings.model_validate(cfg.settings)
        except ValidationError as e:
            raise ValueError(f"tools.json: settings of {name}: {e}") from e


# --- registry + audit log -------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    tool        TEXT NOT NULL,
    project_id  TEXT,
    task_id     TEXT,
    params      TEXT NOT NULL,     -- JSON (never contains credentials)
    status      TEXT NOT NULL,     -- ok, error, refused
    result      TEXT,              -- JSON summary
    error       TEXT,
    cost_eur    REAL NOT NULL,
    seconds     REAL NOT NULL
);
"""


class ToolRegistry:
    def __init__(self, config: ToolsConfig, db: Database, http_get: Callable | None = None):
        self.db = db
        self.db.executescript(_SCHEMA)
        self.tools: dict[str, Tool] = {}
        classes = tool_classes()
        for name, cfg in config.tools.items():
            if name not in classes:
                raise ValueError(f"tools.json: no implementation for tool {name!r}")
            self.tools[name] = classes[name](cfg, http_get or safe_get_json)

    def available(self, name: str) -> bool:
        return name in self.tools and self.tools[name].availability()[0]

    def call(self, name: str, raw_params: dict, project_id: str | None = None,
             task_id: str | None = None, ctx: ToolContext | None = None) -> tuple[ToolOutput, float]:
        """Validate and run one tool call. Returns (output, tool cost). Raises ToolError."""
        ctx = ctx or ToolContext(project_id=project_id, task_id=task_id)
        ctx.project_id, ctx.task_id = project_id or ctx.project_id, task_id or ctx.task_id
        tool = self.tools.get(name)
        if tool is None:
            return self._refuse(name, raw_params, ctx, "unknown tool")
        ok, why = tool.availability()
        if not ok:
            return self._refuse(name, raw_params, ctx, why)
        if self._calls_today(name) >= tool.config.max_calls_per_day:
            return self._refuse(name, raw_params, ctx,
                                f"daily limit {tool.config.max_calls_per_day} reached")
        try:
            params = tool.Params.model_validate(raw_params)
        except ValidationError as e:
            return self._refuse(name, raw_params, ctx, f"invalid params: {e}")
        worst = tool.max_cost(params, ctx)
        if ctx.budget_remaining is not None and worst > ctx.budget_remaining + 1e-9:
            return self._refuse(name, raw_params, ctx,
                                f"worst-case cost EUR {worst:.2f} exceeds remaining budget "
                                f"EUR {ctx.budget_remaining:.2f}")

        start = time.perf_counter()
        try:
            output = tool.run(params, ctx)
        except ToolError as e:
            self._log(name, params.model_dump(), ctx, "error", None, str(e), 0,
                      time.perf_counter() - start)
            raise
        cost = tool.config.cost_per_call_eur if output.cost_eur is None else output.cost_eur
        summary = {**output.summary, **({"assets": [a["id"] for a in output.assets]}
                                        if output.assets else {})}
        self._log(name, params.model_dump(), ctx, "ok", summary, None, cost,
                  time.perf_counter() - start)
        return output, cost

    def recent(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM tool_calls ORDER BY ts DESC LIMIT ?", (limit,))]

    def _calls_today(self, name: str) -> int:
        today = utcnow().date().isoformat()
        return self.db.execute("SELECT COUNT(*) AS n FROM tool_calls WHERE tool = ? AND "
                               "status = 'ok' AND ts >= ?", (name, today)).fetchone()["n"]

    def _refuse(self, name, raw_params, ctx, reason):
        self._log(name, raw_params, ctx, "refused", None, reason, 0, 0)
        raise ToolError(f"tool {name} refused: {reason}")

    def _log(self, name, params, ctx, status, result, error, cost, seconds):
        self.db.execute(
            "INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("call_" + uuid.uuid4().hex[:12], utcnow().isoformat(), name, ctx.project_id,
             ctx.task_id, json.dumps(params, default=str), status,
             json.dumps(result, default=str) if result is not None else None, error, cost,
             round(seconds, 3)))
