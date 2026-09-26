"""
Phase 6 test: Master Orchestrator.

Part A: rules and state changes, with a scripted fake model (deterministic)
Part B: live - real models plan and review a project; shows the log file

Run:  python test_phase6.py
"""

import json
import sys
import tempfile
from pathlib import Path

from clients import ModelClient, ModelResponse, OllamaClient
from core.company import Company
from core.orchestrator import OrchestratorError
from core.projects import ProjectStatus
from core.tasks import InvalidTransition

results = []
TMP = Path(tempfile.mkdtemp())


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
    """Fake Ollama: returns scripted replies; fails loudly if called unexpectedly."""
    provider = "ollama"

    def __init__(self, replies=()):
        self.replies = [json.dumps(r) for r in replies]

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        if not self.replies:
            raise AssertionError("model was called but no reply was scripted")
        return ModelResponse(text=self.replies.pop(0), model=model, provider="ollama",
                             duration_seconds=0)


def make_company(client, log_name="test.log"):
    return Company(":memory:", {"ollama": client}, TMP / log_name).orchestrator


def task(desc, dept="research", priority=2, cost=0.0):
    return {"description": desc, "department": dept, "priority": priority,
            "required_capabilities": [], "estimated_cost": cost, "requires_approval": False}


def decision(d, confidence, reason="test reason"):
    return {"decision": d, "reason": reason, "confidence": confidence, "next_actions": []}


# --- Part A ---------------------------------------------------------------

print("Part A: orchestrator rules (scripted model)")


def t_create():
    o = make_company(ScriptedClient())
    p = o.create_project("Digital Product Experiment", "Earn first EUR 10", 20, priority=1)
    assert p.id.startswith("proj_") and p.status == ProjectStatus.ACTIVE
    ev = next(e for e in o.events.recent(5) if e["action"] == "Created project")
    assert ev["details"]["budget_eur"] == 20
    return f"{p.id} created and logged"


def t_limits():
    o = make_company(ScriptedClient())
    o.create_project("A", "a", 20)
    msg = expect(OrchestratorError, lambda: o.create_project("B", "b", 40))  # 20 + 40 > 50
    o.create_project("B", "b", 10)
    o.create_project("C", "c", 10)
    msg2 = expect(OrchestratorError, lambda: o.create_project("D", "d", 1))
    assert o.events.recent(1)[0]["action"] == "Refused create_project"
    return f"{msg} | {msg2}"


def t_plan_validation():
    o = make_company(ScriptedClient([{"tasks": [
        task("Research printable planner niches on Etsy", "Research", 1),
        task("Hire a lawyer to review terms", "legal", 2),
        task("Buy a premium stock photo bundle", "design", 2, cost=50),
        task("Write the product description", "content", 3, cost=2),
    ]}]))
    p = o.create_project("Planner", "Earn first EUR 10", 20)
    r = o.plan_project(p.id)
    assert [t.department for t in r.accepted] == ["research", "content"], r.accepted
    reasons = [reason for _, reason in r.rejected]
    assert any("unknown department" in x for x in reasons)
    assert any("exceeds remaining budget" in x for x in reasons)
    assert o.budget(p.id)["estimated_future"] == 2
    return f"accepted 2, rejected: {reasons}"


def t_decision_pause():
    o = make_company(ScriptedClient([decision("pause", 0.9, "no traction")]))
    p = o.create_project("P", "x", 10)
    r = o.review_project(p.id, metrics={"views": 3, "sales": 0})
    assert r.applied and o.projects.get(p.id).status == ProjectStatus.PAUSED
    expect(OrchestratorError, lambda: o.plan_project(p.id))
    return "AI 'pause' (0.9) applied; planning a paused project refused"


def t_low_confidence():
    o = make_company(ScriptedClient([decision("finish", 0.3)]))
    p = o.create_project("P", "x", 10)
    r = o.review_project(p.id)
    assert not r.applied and o.projects.get(p.id).status == ProjectStatus.ACTIVE
    return "AI 'finish' at 0.3 confidence deferred; project still ACTIVE"


def t_finish_cancels_tasks():
    o = make_company(ScriptedClient([{"tasks": [task("Research niche A"), task("Research B")]},
                                     decision("finish", 0.95, "objective reached")]))
    p = o.create_project("P", "x", 10)
    o.plan_project(p.id)
    r = o.review_project(p.id)
    stats = o.queue.stats(p.id)
    assert r.applied and o.projects.get(p.id).status == ProjectStatus.FINISHED
    assert stats["CANCELLED"] == 2 and stats["READY"] == 0
    expect(InvalidTransition, lambda: o.set_project_status(p.id, ProjectStatus.ACTIVE, "reopen"))
    return "FINISHED, 2 open tasks cancelled, cannot be reopened"


def t_budget_rule():
    o = make_company(ScriptedClient([{"tasks": [task("Buy EverBee month", cost=10)]}]))
    p = o.create_project("P", "x", 10)
    o.plan_project(p.id)
    t = o.next_task("worker_1")
    o.queue.complete(t.id, "bought", actual_cost=10)
    r = o.review_project(p.id)  # no decision scripted: the model must NOT be asked
    assert r.source == "rule" and o.projects.get(p.id).status == ProjectStatus.PAUSED
    return r.reason


def t_project_priority():
    o = make_company(ScriptedClient([{"tasks": [task("Low project, urgent task", priority=1)]},
                                     {"tasks": [task("High project, normal task", priority=4)]}]))
    low = o.create_project("Low", "x", 5, priority=4)
    high = o.create_project("High", "y", 5, priority=1)
    o.plan_project(low.id)
    o.plan_project(high.id)
    first = o.next_task("w")
    assert first.project_id == high.id, first.description
    o.set_project_status(low.id, ProjectStatus.PAUSED, "human paused it")
    assert o.next_task("w") is None, "task from paused project was handed out"
    return "high-priority project served first; paused project gets no workers"


def t_log_file():
    o = make_company(ScriptedClient(), log_name="format.log")
    o.create_project("Digital Product Experiment", "Earn first EUR 10", 20)
    lines = (TMP / "format.log").read_text(encoding="utf-8").strip().splitlines()
    line = next(x for x in lines if "MASTER_ORCHESTRATOR | Created project" in x)
    assert "budget_eur=20" in line
    return line[:110]


check("create project", t_create)
check("company limits", t_limits)
check("plan validated by code", t_plan_validation)
check("AI pause applied", t_decision_pause)
check("low confidence deferred", t_low_confidence)
check("finish cancels open tasks", t_finish_cancels_tasks)
check("budget rule overrides AI", t_budget_rule)
check("project priority + pause", t_project_priority)
check("readable log file", t_log_file)

# --- Part B: live ---------------------------------------------------------

print("\nPart B: live orchestrator (real models - takes a few minutes)")
live_log = TMP / "live.log"


def t_live():
    client = OllamaClient()
    o = make_company(client, log_name="live.log")
    p = o.create_project("Digital Product Experiment",
                         "Generate the first EUR 10 of revenue from a digital product. "
                         "No paid advertising.", budget_eur=20, priority=1)
    plan = o.plan_project(p.id)
    print(f"       plan by {plan.model_key}: {len(plan.accepted)} accepted, "
          f"{len(plan.rejected)} rejected")
    for t in plan.accepted:
        print(f"         + P{t.priority} [{t.department}] {t.description[:65]}")
    for desc, reason in plan.rejected:
        print(f"         - {desc[:45]} -> {reason}")
    assert plan.accepted, "no tasks accepted"

    review = o.review_project(p.id, metrics={"listing_views": 40, "sales": 0,
                                             "days_left": 20})
    print(f"       review by {review.model_key}: {review.decision} "
          f"(confidence {review.confidence}, applied={review.applied})")
    print(f"         reason: {review.reason[:110]}")
    return f"project now {o.projects.get(p.id).status.value}"


if "--offline" in sys.argv:
    print("(live part skipped: --offline)")
elif OllamaClient().is_available():
    check("live plan + review", t_live)
    if live_log.exists():
        print("\n       --- logs (live run) ---")
        for line in live_log.read_text(encoding="utf-8").splitlines():
            print("       " + line[:150])
else:
    results.append(False)
    print("[FAIL] Ollama not reachable; start Ollama and rerun")

print(f"\n{sum(results)}/{len(results)} tests passed")
sys.exit(0 if all(results) else 1)
