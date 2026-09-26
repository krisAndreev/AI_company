"""
Phase 5 test: model registry + router.

Part A: registry loading/validation and routing rules, with fake clients (no model needed)
Part B: live - real config, real Ollama, route two task types and run them

Run:  python test_phase5.py
"""

import sys

from pydantic import ValidationError

from clients import ModelClient, ModelClientError, ModelResponse, OllamaClient
from core.model_registry import ModelRegistry
from core.router import ModelRouter, NoModelAvailable, RouteRequest
from core.schemas import Decision
from core.structured import generate_structured

results = []


def check(name, fn):
    try:
        detail = fn()
        results.append(True)
        print(f"[pass] {name}" + (f" -> {detail}" if detail else ""))
    except Exception as e:
        results.append(False)
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


def expect(exc_type, fn):
    try:
        fn()
    except exc_type as e:
        return str(e)
    raise AssertionError(f"expected {exc_type.__name__}")


class FakeClient(ModelClient):
    def __init__(self, provider, installed, reachable=True):
        self.provider, self.installed, self.reachable = provider, installed, reachable

    def is_available(self):
        return self.reachable

    def list_models(self):
        if not self.reachable:
            raise ModelClientError("down")
        return list(self.installed)

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        return ModelResponse(text="{}", model=model, provider=self.provider, duration_seconds=0)


# --- Part A ---------------------------------------------------------------

print("Part A: registry and routing rules")
registry = ModelRegistry.load()
cloud_enabled = registry.model_copy(deep=True)
cloud_enabled.models["cloud_example"].enabled = True
cloud_enabled.models["cloud_example"].model = "cloud-model"

ollama_ok = FakeClient("ollama", ["gemma3:4b", "qwen3:4b"])
cloud_ok = FakeClient("openai", ["cloud-model"])


def t_config():
    return f"{len(registry.models)} models, {len(registry.task_profiles)} task profiles"


def t_bad_config():
    bad = registry.model_dump()
    bad["models"]["local_fast"]["cost_per_1k_input_tokens"] = -1
    return expect(ValidationError, lambda: ModelRegistry.model_validate(bad)).splitlines()[0]


def route(reg, clients, **kw):
    return ModelRouter(reg, clients).select(RouteRequest(**kw))


def t_fast_task():
    d = route(registry, {"ollama": ollama_ok}, task_type="classification")
    assert d.model_key == "local_fast", d.reason
    return d.reason


def t_reasoning_task():
    d = route(registry, {"ollama": ollama_ok}, task_type="planning")
    assert d.model_key == "local_reasoning", d.reason
    assert "missing capabilities" in d.rejected["local_fast"]
    return f"{d.model_key}; local_fast rejected: {d.rejected['local_fast']}"


def t_prefer_override():
    d = route(registry, {"ollama": ollama_ok}, task_type="classification", prefer="quality")
    assert d.model_key == "local_reasoning"
    return "classification + prefer=quality -> local_reasoning"


def t_unknown_task():
    return expect(ValueError, lambda: route(registry, {"ollama": ollama_ok}, task_type="hack"))


def t_not_installed():
    only_gemma = FakeClient("ollama", ["gemma3:4b"])
    assert route(registry, {"ollama": only_gemma}, task_type="classification").model_key == "local_fast"
    msg = expect(NoModelAvailable, lambda: route(registry, {"ollama": only_gemma},
                                                 task_type="planning"))
    assert "not installed" in msg
    return "classification still routes; planning refused: qwen3:4b not installed"


def t_provider_down():
    down = FakeClient("ollama", [], reachable=False)
    msg = expect(NoModelAvailable, lambda: route(registry, {"ollama": down}))
    assert "not reachable" in msg
    return "NoModelAvailable: provider not reachable"


def t_cloud_permission():
    clients = {"ollama": ollama_ok, "openai": cloud_ok}
    d = route(cloud_enabled, clients, task_type="planning")
    assert d.model_key == "local_reasoning"
    assert "not permitted" in d.rejected["cloud_example"]
    d = route(cloud_enabled, clients, task_type="planning", allow_cloud=True)
    assert d.model_key == "cloud_example" and d.estimated_cost > 0
    return f"blocked without permission; with permission -> cloud (est EUR {d.estimated_cost:.4f})"


def t_cost_limit():
    clients = {"ollama": ollama_ok, "openai": cloud_ok}
    d = route(cloud_enabled, clients, task_type="planning", allow_cloud=True, max_cost_eur=0.001)
    assert d.model_key == "local_reasoning"
    assert "exceeds limit" in d.rejected["cloud_example"]
    return d.rejected["cloud_example"]


def t_cost_math():
    spec = cloud_enabled.models["cloud_example"]  # 0.002 in, 0.008 out per 1k
    assert abs(spec.estimate_cost(1000, 500) - 0.006) < 1e-12
    return "1000 in + 500 out = EUR 0.006"


def t_disabled():
    d = route(registry, {"ollama": ollama_ok, "openai": cloud_ok}, allow_cloud=True)
    assert d.rejected["cloud_example"] == "disabled in config"
    return "disabled cloud model never chosen"


check("config loads", t_config)
check("invalid config rejected", t_bad_config)
check("fast task -> gemma", t_fast_task)
check("reasoning task -> qwen", t_reasoning_task)
check("preference override", t_prefer_override)
check("unknown task type rejected", t_unknown_task)
check("model not installed", t_not_installed)
check("provider down", t_provider_down)
check("cloud needs permission", t_cloud_permission)
check("cost limit enforced", t_cost_limit)
check("cost estimate", t_cost_math)
check("disabled model ignored", t_disabled)

# --- Part B: live ---------------------------------------------------------

print("\nPart B: live routing with real Ollama")
router = ModelRouter(registry, {"ollama": OllamaClient()})

PROMPT = ("Experiment: sell a printable budget planner on Etsy. Budget EUR 20, EUR 3 spent, "
          "day 10 of 30, 40 listing views, 0 sales. Should we continue, pivot, or stop?")


def t_status():
    for row in router.status():
        print(f"       {row['key']:<16} {row['model']:<30} enabled={row['enabled']!s:<5} "
              f"installed={row['installed']}")
    return None


def run_live(task_type):
    d = router.select(RouteRequest(task_type=task_type))
    r = generate_structured(d.client, d.model, PROMPT, Decision,
                            system="You are the head of a small online business. Be concise.",
                            think=d.spec.think)
    print(f"       decision={r.value.decision!r} confidence={r.value.confidence}")
    return f"{d.model_key} ({d.model}), valid in {r.attempts} attempt(s), {r.total_seconds:.1f}s"


check("model status", t_status)
if "--offline" in sys.argv:
    print("(live part skipped: --offline)")
elif router.clients["ollama"].is_available():
    check("live classification-type call", lambda: run_live("classification"))
    check("live decision-type call", lambda: run_live("decision"))
else:
    results.append(False)
    print("[FAIL] Ollama not reachable; start Ollama and rerun")

print(f"\n{sum(results)}/{len(results)} tests passed")
sys.exit(0 if all(results) else 1)
