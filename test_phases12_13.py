"""
Phases 12-13 test: memory + learning, external research tools.

Part A: scripted fake model + fake HTTP (deterministic)
Part B: live - real model learns from an ended experiment (+ live Etsy if ETSY_API_KEY set)

Run:  python test_phases12_13.py
"""

import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from clients import ModelClient, ModelResponse, OllamaClient
from core.company import Company
from core.experiments import ExperimentSpec
from core.memory import MemoryRuleError
from core.project_manager import ManagementError
from core.tasks import TaskStatus
from core.tools import ToolError, safe_get_json

results = []
FAKE_KEY = "test-secret-key-123"


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


class ScriptedClient(ModelClient):
    provider = "ollama"

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        self.prompts.append("\n".join(m.content for m in messages))
        if not self.replies:
            raise AssertionError("model was called but no reply was scripted")
        return ModelResponse(text=json.dumps(self.replies.pop(0)), model=model,
                             provider="ollama", duration_seconds=0)


class FakeEtsy:
    """Stands in for the Etsy API; records requests (to check the key never leaks)."""
    def __init__(self):
        self.requests = []

    def __call__(self, url, params, headers, allowed_hosts):
        self.requests.append((url, params, headers))
        assert "openapi.etsy.com" in allowed_hosts
        listing = lambda price, views, favs, days: {
            "price": {"amount": price, "divisor": 100, "currency_code": "EUR"},
            "views": views, "num_favorers": favs,
            "original_creation_timestamp": 1_790_000_000 - days * 86400}
        return {"count": 12400, "results": [listing(499, 120, 10, 200), listing(350, 40, 3, 30),
                                            listing(799, 300, 25, 700)]}


def company(*replies, http_get=None):
    client = ScriptedClient(replies)
    return Company(":memory:", {"ollama": client}, None, http_get=http_get), client


SPEC = ExperimentSpec(
    name="First EUR 10", objective="Generate the first EUR 10 from a digital product.",
    budget_eur=20, max_duration_days=30, max_human_hours=5, max_pivots=1,
    success_criteria=[{"metric": "revenue_eur", "op": ">=", "target": 10}], checkpoint_days=[7])

# --- Phase 12: memory ------------------------------------------------------------------

print("Phase 12: memory and learning")


def t_scope_rules():
    c, _ = company()
    m = c.memory
    e1 = expect(MemoryRuleError, lambda: m.add("personal", "preference", "likes X", "AI"))
    e2 = expect(MemoryRuleError, lambda: m.add("project", "fact", "niche is weddings", "AI"))
    e3 = expect(MemoryRuleError, lambda: m.add("operational", "lesson", "qwen is slow", "AI"))
    m.add("personal", "preference", "Prefers digital products over physical", "HUMAN")
    return f"{e1} | {e2} | {e3}"


def t_isolation():
    c, _ = company()
    a = c.orchestrator.create_project("A", "x", 5)
    b = c.orchestrator.create_project("B", "y", 5)
    c.memory.add("project", "fact", "Project A niche: wedding planners", "WORKER",
                 project_id=a.id, evidence=["task_1"])
    assert "wedding" in c.memory.context_for(a.id)
    assert "wedding" not in c.memory.context_for(b.id)
    expect(MemoryRuleError, lambda: c.memory.recall("project"))
    return "project A memory never appears in project B's context"


def t_evidence_confidence():
    c, _ = company()
    item = c.memory.add("operational", "lesson", "Gemma writes listings faster than Qwen",
                        "AI", evidence=["exp_1"], confidence=0.95)
    assert item.confidence == 0.5, item.confidence
    item = c.memory.confirm(item.id, "exp_2")
    assert item.confidence == 0.75
    item = c.memory.contradict(item.id, "exp_3")
    item = c.memory.contradict(item.id, "exp_4")
    item = c.memory.contradict(item.id, "exp_5")
    assert item.status == "retired"
    return "AI claim 0.95 capped to 0.50 (1 evidence) -> 0.75 confirmed -> retired after contradictions"


def t_learning():
    c, _ = company({"lessons": [
        {"scope": "operational", "kind": "lesson", "content": "Research tasks succeed more often "
         "when given a concrete niche", "confidence": 0.9},
        {"scope": "project", "kind": "fact", "content": "Budget planners had zero sales in 30 days",
         "confidence": 0.8}],
        "improvements": [{"target": "config/company.json", "proposal": "Raise min confidence "
                          "for stop decisions to 0.8", "rationale": "Stopped before publishing"}]})
    exp = c.experiments.create(SPEC)
    expect(ManagementError, lambda: c.learning.learn_from_experiment(exp.id))   # still running
    c.experiments.evaluate(exp.id, now=exp.started_at + timedelta(days=31))     # -> FAILED
    before = Path("config/company.json").read_text(encoding="utf-8")
    out = c.learning.learn_from_experiment(exp.id)
    assert len(out.new_lessons) == 2 and all(i.evidence == [exp.id] for i in out.new_lessons)
    op = next(i for i in out.new_lessons if i.scope == "operational")
    assert op.confidence == 0.5
    imps = c.improvements.list("PROPOSED")
    assert len(imps) == 1 and Path("config/company.json").read_text(encoding="utf-8") == before
    expect(ManagementError, lambda: c.learning.learn_from_experiment(exp.id))  # no double-learn
    expect(MemoryRuleError, lambda: c.improvements.decide(imps[0]["id"], True, who="AI"))
    c.improvements.decide(imps[0]["id"], False)
    return "2 lessons stored (evidence + capped confidence); improvement PROPOSED only, config untouched"


def t_memory_in_prompts():
    c, client = company({"tasks": [{"description": "Research niches", "department": "research",
                                    "priority": 1, "required_capabilities": ["market_research"],
                                    "estimated_cost": 0, "requires_approval": False,
                                    "depends_on": []}]},
                        {"success": True, "summary": "ok", "output": "done"})
    c.memory.add("personal", "preference", "Owner prefers printable products", "HUMAN")
    c.memory.add("operational", "lesson", "Research niches before designing", "HUMAN",
                 evidence=["exp_old"])
    p = c.orchestrator.create_project("P", "Sell a printable", 5)
    c.orchestrator.plan_project(p.id)
    assert "prefers printable" in client.prompts[-1]
    c.runner.run_once()
    assert "Research niches before designing" in client.prompts[-1]
    return "personal + operational memory reach planner and worker prompts"


def t_stats():
    c, _ = company({"tasks": [{"description": "Research niches", "department": "research",
                               "priority": 1, "required_capabilities": [], "estimated_cost": 0,
                               "requires_approval": False, "depends_on": []}]},
                   {"success": True, "summary": "ok", "output": "done"})
    p = c.orchestrator.create_project("P", "x", 5)
    c.orchestrator.plan_project(p.id)
    c.runner.run_once()
    s = c.learning.operational_stats()[0]
    assert s["runs"] == 1 and s["success_rate"] == 1.0
    return f"{s['model_key']}/{s['department']}: {s['runs']} run, success {s['success_rate']}"


check("memory scope rules", t_scope_rules)
check("project memory isolation", t_isolation)
check("evidence-based confidence", t_evidence_confidence)
check("learning from experiment", t_learning)
check("memory used in prompts", t_memory_in_prompts)
check("operational stats", t_stats)

# --- Phase 13: tools ----------------------------------------------------------------------

print("\nPhase 13: external research tools")


def research_task(c):
    p = c.orchestrator.create_project("P", "Find a niche", 5)
    c.orchestrator.plan_project(p.id)
    return p, c.queue.list(project_id=p.id)[0]


PLAN = {"tasks": [{"description": "Pull Etsy data for wedding planner", "department": "research",
                   "priority": 1, "required_capabilities": ["research_marketplace"],
                   "estimated_cost": 0, "requires_approval": False, "depends_on": []}]}


def t_no_key_human_step():
    os.environ.pop("ETSY_API_KEY", None)
    c, _ = company(PLAN)
    research_task(c)
    out = c.runner.run_once()
    assert out["outcome"] == "waiting (human_step)"
    return "no ETSY_API_KEY -> tool unavailable -> task waits for a human"


def t_tool_flow():
    os.environ["ETSY_API_KEY"] = FAKE_KEY
    etsy = FakeEtsy()
    c, client = company(PLAN, {"keywords": "wedding planner", "limit": 50},
                        {"success": True, "summary": "competition high", "output": "analysis"},
                        http_get=etsy)
    p, task = research_task(c)
    out = c.runner.run_once()
    assert out["outcome"] == "completed", out
    call = c.tools.recent(1)[0]
    assert call["status"] == "ok" and json.loads(call["params"])["keywords"] == "wedding planner"
    assert etsy.requests[0][2]["x-api-key"] == FAKE_KEY
    everything_logged = json.dumps(c.tools.recent()) + "".join(client.prompts)
    assert FAKE_KEY not in everything_logged, "API key leaked to model or logs"
    cmp = c.research.compare("wedding planner")
    assert cmp["competition_listings"]["values"]["etsy_api"] == 12400
    assert "etsy_api=12400" in client.prompts[-1]
    return (f"model chose params, code ran tool, {len(cmp)} metrics stored with source; "
            f"key never seen by model or logs")


def t_bad_params_and_limits():
    os.environ["ETSY_API_KEY"] = FAKE_KEY
    c, _ = company(http_get=FakeEtsy())
    e1 = expect(ToolError, lambda: c.tools.call("etsy_listings", {"keywords": "x" * 200}))
    e2 = expect(ToolError, lambda: c.tools.call("shell", {"cmd": "rm -rf /"}))
    c.tools.tools["etsy_listings"].config.max_calls_per_day = 1
    c.tools.call("etsy_listings", {"keywords": "planner"})
    e3 = expect(ToolError, lambda: c.tools.call("etsy_listings", {"keywords": "planner"}))
    statuses = [r["status"] for r in c.tools.recent()]
    assert statuses.count("refused") == 3
    return f"{e1[:50]}... | {e2} | {e3}"


def t_url_allowlist():
    e1 = expect(ToolError, lambda: safe_get_json("https://evil.example.com/x", {}, {},
                                                 ["openapi.etsy.com"]))
    e2 = expect(ToolError, lambda: safe_get_json("http://openapi.etsy.com/x", {}, {},
                                                 ["openapi.etsy.com"]))
    return f"{e1} | {e2}"


def t_signal_comparison():
    os.environ["ETSY_API_KEY"] = FAKE_KEY
    c, _ = company(http_get=FakeEtsy())
    output, _ = c.tools.call("etsy_listings", {"keywords": "wedding planner"})
    c.research.add_many(output.observations)
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write("keyword,metric,value,unit\nwedding planner,competition_listings,30000,listings\n"
                "wedding planner,median_price,5.2,EUR\nwedding planner,monthly_searches,1800,\n")
    c.research.import_csv(f.name, source="everbee_export")
    os.unlink(f.name)
    cmp = c.research.compare("wedding planner")
    assert cmp["competition_listings"]["assessment"].startswith("sources disagree")
    assert cmp["median_price"]["assessment"] == "sources agree"
    assert cmp["monthly_searches"]["assessment"] == "single source - unverified"
    assert cmp["monthly_searches"]["estimates"] == ["everbee_export"]
    return "; ".join(f"{k}: {v['assessment']}" for k, v in cmp.items()
                     if k in ("competition_listings", "median_price", "monthly_searches"))


check("tool unavailable -> human step", t_no_key_human_step)
check("full tool flow + key safety", t_tool_flow)
check("invalid params, unknown tool, rate limit", t_bad_params_and_limits)
check("URL allowlist + https only", t_url_allowlist)
check("multi-source signal comparison", t_signal_comparison)
os.environ.pop("ETSY_API_KEY", None)

# --- Part B: live -------------------------------------------------------------------------

print("\nPart B: live learning (real model)")


def t_live():
    c = Company(":memory:", None, None)
    c.memory.add("personal", "preference", "Owner prefers low-effort digital products", "HUMAN")
    exp = c.experiments.create(SPEC)
    p = c.projects.get(exp.project_id)
    for desc, dept, ok in [("Research printable planner niches", "research", True),
                           ("Write Etsy listing copy", "content", True),
                           ("Publish listing on Etsy", "content", False)]:
        from core.schemas import TaskProposal
        t = c.queue.add(TaskProposal(description=desc, department=dept, priority=2,
                                     estimated_cost=0, requires_approval=False), p.id)
        c.queue.start(t.id, "sim")
        (c.queue.complete(t.id, "done: niche = budget planners") if ok
         else c.queue.fail(t.id, "needs human; nobody published it"))
    c.experiments.record_metric(exp.id, "revenue_eur", 0, "etsy_stats")
    c.experiments.evaluate(exp.id, now=exp.started_at + timedelta(days=31))
    out = c.learning.learn_from_experiment(exp.id)
    for item in out.new_lessons:
        print(f"       lesson [{item.scope}, conf {item.confidence:.2f}] {item.content[:90]}")
    for imp in c.improvements.list("PROPOSED"):
        print(f"       proposal [{imp['target']}] {imp['proposal'][:90]}")
    return f"{len(out.new_lessons)} lessons, {len(out.improvement_ids)} proposals ({out.model_key})"


if "--offline" in sys.argv:
    print("(live part skipped: --offline)")
elif OllamaClient().is_available():
    check("live learning", t_live)
else:
    results.append(False)
    print("[FAIL] Ollama not reachable")
if os.environ.get("ETSY_API_KEY"):
    print("       (ETSY_API_KEY set: live Etsy call not part of automated test yet)")
else:
    print("       note: live Etsy API not tested - set ETSY_API_KEY to enable the tool")

print(f"\n{sum(results)}/{len(results)} tests passed")
sys.exit(0 if all(results) else 1)
