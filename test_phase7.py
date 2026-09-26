"""
Phase 7 test: Project Managers.

Part A: planning with dependencies, staged replanning, failure handling, reports
        (scripted fake model - deterministic)
Part B: live - real model plans a project with dependencies; simulated work and failures

Run:  python test_phase7.py
"""

import json
import sys

from clients import ModelClient, ModelResponse, OllamaClient
from core.company import Company
from core.project_manager import ManagementError
from core.tasks import TaskStatus

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


class ScriptedClient(ModelClient):
    """Fake Ollama: returns scripted replies and records every prompt it receives."""
    provider = "ollama"

    def __init__(self, replies=()):
        self.replies = [json.dumps(r) for r in replies]
        self.prompts = []

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        self.prompts.append(messages[-1].content)
        if not self.replies:
            raise AssertionError("model was called but no reply was scripted")
        return ModelResponse(text=self.replies.pop(0), model=model, provider="ollama",
                             duration_seconds=0)


def make_company(client):
    return Company(":memory:", {"ollama": client}, None).orchestrator


def t(desc, dept="research", deps=(), priority=2, cost=0.0):
    return {"description": desc, "department": dept, "priority": priority,
            "required_capabilities": [], "estimated_cost": cost, "requires_approval": False,
            "depends_on": list(deps)}


def run(o, task, ok=True, result="done", cost=0.0):
    """Simulate a worker executing one specific task."""
    claimed = o.queue.claim_next("sim_worker", project_id=task.project_id)
    assert claimed.id == task.id, f"expected {task.description}, got {claimed.description}"
    if ok:
        return o.queue.complete(task.id, result, actual_cost=cost)
    return o.queue.fail(task.id, "simulated failure")


# --- Part A ---------------------------------------------------------------

print("Part A: project manager rules (scripted model)")


def t_plan_dependencies():
    o = make_company(ScriptedClient([{"tasks": [
        t("Research planner niches", "research", priority=1),
        t("Design the planner", "design", deps=[1]),
        t("Write the Etsy listing", "content", deps=[1, 2]),
    ]}]))
    p = o.create_project("Planner", "Earn first EUR 10", 20)
    r = o.plan_project(p.id)
    research, design, listing = r.accepted
    assert [x.status for x in r.accepted] == [TaskStatus.READY, TaskStatus.PENDING,
                                             TaskStatus.PENDING]
    assert design.dependencies == [research.id]
    assert listing.dependencies == [research.id, design.id]
    return "plan numbers 1,2,3 mapped to real task ids; only task 1 READY"


def t_bad_dependencies():
    o = make_company(ScriptedClient([{"tasks": [
        t("Research niches", "research"),
        t("Depends on itself", "design", deps=[2]),
        t("Depends on a later task", "content", deps=[4]),
        t("Unknown department", "legal"),
        t("Depends on a rejected task", "marketing", deps=[4]),
    ]}]))
    p = o.create_project("P", "x", 10)
    r = o.plan_project(p.id)
    reasons = [reason for _, reason in r.rejected]
    assert len(r.accepted) == 4 and len(r.rejected) == 1, reasons
    assert "unknown department" in reasons[0]
    assert all(t.dependencies == [] for t in r.accepted[1:]), [t.dependencies for t in r.accepted]
    dropped = [e for e in o.events.recent(20) if e["action"] == "Dropped invalid dependencies"]
    assert len(dropped) == 3
    return "self/forward references and links to rejected tasks dropped (logged); bad department refused"


def t_staged_planning():
    client = ScriptedClient([
        {"tasks": [t("Research niches", "research")]},
        {"tasks": [t("Design product for wedding niche", "design")]},
    ])
    o = make_company(client)
    p = o.create_project("P", "x", 10)
    first = o.plan_project(p.id).accepted[0]
    msg = expect(ManagementError, lambda: o.plan_project(p.id))
    run(o, first, result="Best niche: wedding planners, low competition")
    o.plan_project(p.id)
    assert "wedding planners, low competition" in client.prompts[-1], "results not passed on"
    return f"'{msg}'; second plan saw the first task's result"


def t_retry_then_give_up():
    o = make_company(ScriptedClient([{"tasks": [
        t("Research niches", "research"),
        t("Design product", "design", deps=[1]),
        t("Write listing", "content", deps=[2]),
        t("Independent marketing research", "marketing"),
    ]}]))
    p = o.create_project("P", "x", 10)
    research, design, listing, independent = o.plan_project(p.id).accepted
    pm = o.manager(p.id)
    max_attempts = o.config.max_task_retries + 1
    for attempt in range(1, max_attempts + 1):
        run(o, research, ok=False)
        outcome = pm.handle_failures()
        if attempt < max_attempts:
            assert outcome["retried"] == [research.id], outcome
    assert outcome["gave_up"] == [research.id]
    assert set(outcome["cancelled_blocked"]) == {design.id, listing.id}, "chain not cancelled"
    assert o.queue.get(independent.id).status == TaskStatus.READY, "unrelated task touched"
    actions = [e["action"] for e in o.events.recent(20)]
    assert "Escalated to MASTER_ORCHESTRATOR" in actions
    return (f"retried {max_attempts - 1}x, gave up, cancelled 2 dependents (chain), "
            f"escalated; independent task untouched")


def t_report():
    o = make_company(ScriptedClient([{"tasks": [
        t("Research niches", "research"),
        t("Buy mockup template", "design", cost=4),
        t("Write listing", "content", deps=[1]),
    ]}]))
    p = o.create_project("P", "x", 10)
    research, mockup, listing = o.plan_project(p.id).accepted
    run(o, research, result="niche found")
    run(o, mockup, cost=3.5)
    r = o.manager(p.id).report()
    assert r["progress_percent"] == 67, r["progress_percent"]
    assert r["budget_eur"]["spent"] == 3.5 and r["ready"] == ["Write listing"]
    return (f"progress {r['progress_percent']}%, spent EUR {r['budget_eur']['spent']}, "
            f"ready {r['ready']}")


def t_review_uses_report():
    client = ScriptedClient([
        {"tasks": [t("Research niches", "research")]},
        {"decision": "continue", "reason": "early", "confidence": 0.8, "next_actions": []},
    ])
    o = make_company(client)
    p = o.create_project("P", "x", 10)
    o.plan_project(p.id)
    o.review_project(p.id, metrics={"views": 5})
    assert '"progress_percent"' in client.prompts[-1] and '"views": 5' in client.prompts[-1]
    return "orchestrator review prompt built from the PM's report + metrics"


check("plan with dependencies", t_plan_dependencies)
check("invalid dependencies refused", t_bad_dependencies)
check("staged planning uses results", t_staged_planning)
check("retry, give up, cancel chain, escalate", t_retry_then_give_up)
check("code-computed report", t_report)
check("review uses PM report", t_review_uses_report)

# --- Part B: live ---------------------------------------------------------

print("\nPart B: live project manager (real model)")


def t_live():
    o = make_company(OllamaClient())
    p = o.create_project("Digital Product Experiment",
                         "Generate the first EUR 10 of revenue from a digital product. "
                         "No paid advertising.", budget_eur=20, priority=1)
    plan = o.plan_project(p.id)
    number = {task.id: i for i, task in enumerate(plan.accepted, start=1)}
    print(f"       plan by {plan.model_key}: {len(plan.accepted)} accepted, "
          f"{len(plan.rejected)} rejected")
    for i, task in enumerate(plan.accepted, start=1):
        deps = [number[d] for d in task.dependencies]
        print(f"         {i}. {task.status.value:<7} [{task.department}] "
              f"{task.description[:55]}" + (f"  (after {deps})" if deps else ""))
    for desc, reason in plan.rejected:
        print(f"         x {desc[:45]} -> {reason}")
    assert plan.accepted, "no tasks accepted"

    # Simulate: first ready task succeeds, next ready task fails every time.
    pm = o.manager(p.id)
    first = o.queue.list(TaskStatus.READY, p.id)[0]
    run(o, first, result="simulated result")
    ready = o.queue.list(TaskStatus.READY, p.id)
    if ready:
        for _ in range(o.config.max_task_retries + 1):
            run(o, ready[0], ok=False)
            outcome = pm.handle_failures()
        print(f"       simulated failure of '{ready[0].description[:40]}': "
              f"gave_up={len(outcome['gave_up'])}, "
              f"cancelled_blocked={len(outcome['cancelled_blocked'])}")
    r = pm.report()
    print(f"       report: progress {r['progress_percent']}%, tasks {r['tasks']}")
    return None


if "--offline" in sys.argv:
    print("(live part skipped: --offline)")
elif OllamaClient().is_available():
    check("live plan + simulated work", t_live)
else:
    results.append(False)
    print("[FAIL] Ollama not reachable; start Ollama and rerun")

print(f"\n{sum(results)}/{len(results)} tests passed")
sys.exit(0 if all(results) else 1)
