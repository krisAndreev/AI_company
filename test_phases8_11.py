"""
Phases 8-11 test: departments, workers, budgets/permissions, experiments.

Part A: scripted fake model (deterministic), one section per phase
Part B: live - real models run an experiment end to end

Run:  python test_phases8_11.py
"""

import json
import sys
from datetime import timedelta

from clients import ModelClient, ModelClientError, ModelResponse, OllamaClient
from core.company import Company
from core.experiments import ExperimentSpec, ExperimentStatus
from core.project_manager import ManagementError
from core.projects import ProjectStatus
from core.schemas import TaskProposal
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
    """Fake Ollama: scripted replies (dicts, or exceptions to raise); records prompts."""
    provider = "ollama"

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        self.prompts.append(messages[-1].content)
        if not self.replies:
            raise AssertionError("model was called but no reply was scripted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ModelResponse(text=json.dumps(reply), model=model, provider="ollama",
                             duration_seconds=0, prompt_tokens=100, completion_tokens=50)


def company(*replies):
    client = ScriptedClient(replies)
    return Company(":memory:", {"ollama": client}, None), client


def t(desc, dept="research", caps=(), deps=(), cost=0.0, approval=False, priority=2):
    return {"description": desc, "department": dept, "priority": priority,
            "required_capabilities": list(caps), "estimated_cost": cost,
            "requires_approval": approval, "depends_on": list(deps)}


def plan(*tasks):
    return {"tasks": list(tasks)}


def ok(summary="done", output="the work"):
    return {"success": True, "summary": summary, "output": output}


def project_with(c, *tasks, budget=20, permissions=None):
    p = c.orchestrator.create_project("P", "Earn first EUR 10", budget, permissions=permissions)
    accepted = c.orchestrator.plan_project(p.id).accepted
    return p, accepted


# --- Phase 8: departments ------------------------------------------------------

print("Phase 8: departments")


def t_registry():
    c, _ = company()
    assert c.orchestrator.request_capability("keyword_research") == "research"
    msg = expect(ManagementError, lambda: c.orchestrator.request_capability("hack_bank"))
    return f"{len(c.departments.names())} shared departments; unknown capability: {msg}"


def t_plan_schema_enum():
    c, _ = company()
    schema = json.dumps(c.departments.plan_schema().model_json_schema())
    assert '"enum"' in schema and "data_analysis" in schema and "run_paid_ads" in schema
    return "schema sent to model only allows real department/capability names"


def t_capability_mismatch():
    c, _ = company(plan(t("Write listing", "content", caps=["write_text"]),
                        t("Research with content dept", "content", caps=["market_research"])))
    _, accepted = project_with(c)
    p = c.projects.list()[0]
    rejected = [e for e in c.events.recent(20, p.id) if e["action"] == "Rejected proposed task"]
    assert len(accepted) == 1 and "not offered by content" in rejected[0]["details"]["reason"]
    return rejected[0]["details"]["reason"]


check("registry + capability requests", t_registry)
check("plan schema constrains names", t_plan_schema_enum)
check("capability must belong to department", t_capability_mismatch)

# --- Phase 9: workers ------------------------------------------------------------

print("\nPhase 9: workers")


def t_worker_runs_chain():
    c, client = company(
        plan(t("Research niches", "research", caps=["market_research"]),
             t("Write listing", "content", caps=["write_text"], deps=[1])),
        ok("niche found", "Wedding planners: low competition"),
        ok("listing written", "Title: Wedding Planner Printable"),
    )
    _, (research, listing) = project_with(c)
    out = c.runner.run(max_tasks=5)
    assert [o["outcome"] for o in out] == ["completed", "completed"], out
    assert "Wedding planners: low competition" in client.prompts[-1], "dependency result not passed"
    agent = c.workers.recent(5)
    assert len(agent) == 2 and all(a["status"] == "COMPLETED" for a in agent)
    assert c.queue.get(listing.id).status == TaskStatus.COMPLETED
    return (f"2 temporary workers ({out[0]['model']}, {out[1]['model']}); second saw "
            f"first's output")


def t_worker_failure_retry():
    c, _ = company(plan(t("Research niches", caps=["market_research"])),
                   {"success": False, "summary": "not enough info", "output": "-"},
                   ModelClientError("Ollama timed out"),
                   ok())
    _, (task,) = project_with(c)
    outs = [c.runner.run_once()["outcome"] for _ in range(3)]
    assert outs == ["failed", "failed", "completed"], outs
    assert c.queue.get(task.id).attempts == 3
    return "worker-reported failure + model error both retried, then completed"


def t_nothing_to_do():
    c, _ = company()
    assert c.runner.run_once() is None
    return "run_once() -> None when no work"


check("workers execute dependency chain", t_worker_runs_chain)
check("failures retried via PM", t_worker_failure_retry)
check("idle when no tasks", t_nothing_to_do)

# --- Phase 10: budgets and permissions -------------------------------------------

print("\nPhase 10: budgets and permissions")


def t_tiers():
    c, _ = company(plan(t("Buy font license", "finance", ["budget_analysis"], cost=0.05),
                        t("Buy stock photo", "finance", ["budget_analysis"], cost=0.5),
                        t("Buy EverBee month", "finance", ["budget_analysis"], cost=5)),
                   ok(), ok())
    _, (small, mid, big) = project_with(c)
    c.runner.run(max_tasks=5)
    verdicts = {e["task_id"]: e["details"] for e in c.events.recent(50)
                if e["action"].startswith("Verdict")}
    assert verdicts[small.id]["approved_by"] == "POLICY_AUTO"
    assert verdicts[mid.id]["approved_by"] == "PROJECT_MANAGER"
    assert c.queue.get(big.id).status == TaskStatus.WAITING
    return "EUR 0.05 auto, EUR 0.50 project-level, EUR 5 waits for human"


def t_human_approval_flow():
    c, _ = company(plan(t("Buy EverBee month", "finance", ["budget_analysis"], cost=5)), ok())
    _, (task,) = project_with(c)
    c.runner.run_once()
    assert c.queue.get(task.id).status == TaskStatus.WAITING
    c.runner.approve(task.id)
    assert c.runner.run_once()["outcome"] == "completed"
    return "WAITING -> human approve -> executed"


def t_budget_insufficient():
    c, _ = company(plan(t("Buy course", "finance", ["budget_analysis"], cost=8)))
    p, _ = project_with(c, budget=10)
    # An earlier EUR 5 purchase leaves only EUR 5 of the EUR 10 budget.
    earlier = c.queue.add(TaskProposal(description="Earlier purchase", department="finance",
                                       priority=3, estimated_cost=5, requires_approval=False),
                          p.id)
    c.queue.start(earlier.id, "HUMAN")
    c.queue.complete(earlier.id, "bought", actual_cost=5)
    out = c.runner.run_once()
    assert out["outcome"].startswith("waiting") and "insufficient budget" in out["reason"]
    return out["reason"]


def t_permission_denied():
    c, _ = company(plan(t("Run Etsy ads", "marketing", ["run_paid_ads"])))
    _, (task,) = project_with(c)
    out = c.runner.run_once()
    assert out["outcome"] == "denied" and c.queue.get(task.id).status == TaskStatus.CANCELLED
    return out["reason"]


def t_manual_step():
    c, _ = company(plan(t("Pull EverBee data for 'wedding planner'", "research",
                          ["research_marketplace"])))
    p, (task,) = project_with(c)
    out = c.runner.run_once()
    assert out["outcome"] == "waiting (human_step)"
    c.runner.complete_manual(task.id, "EverBee: 1.2k searches/mo (estimate)", cost_eur=0)
    assert c.queue.get(task.id).status == TaskStatus.COMPLETED
    return "manual capability -> waits for human -> human completes it"


def t_ai_flag_and_expense():
    c, _ = company(plan(t("Publish listing", "content", ["publish_content"], approval=True)))
    p, (task,) = project_with(c, permissions=["publishing"])
    out = c.runner.run_once()
    assert "flagged as requiring approval" in out["reason"]
    c.runner.approve(task.id)
    out = c.runner.run_once()
    assert out["outcome"] == "waiting (human_step)"   # publishing is manual
    c.runner.complete_manual(task.id, "listed on Etsy", cost_eur=0.20)
    assert c.ledger.total(p.id) == 0.20 and c.orchestrator.budget(p.id)["spent"] == 0.20
    return "AI approval flag honoured; Etsy fee EUR 0.20 in ledger and budget"


def t_unknown_permission():
    c, _ = company()
    return expect(ManagementError, lambda: c.orchestrator.create_project(
        "P", "x", 5, permissions=["root_access"]))


check("approval tiers", t_tiers)
check("human approval flow", t_human_approval_flow)
check("insufficient budget waits", t_budget_insufficient)
check("missing permission denied", t_permission_denied)
check("manual capability = human step", t_manual_step)
check("AI approval flag + expense ledger", t_ai_flag_and_expense)
check("unknown permission refused", t_unknown_permission)

# --- Phase 11: experiments -----------------------------------------------------------

print("\nPhase 11: experiments")

SPEC = ExperimentSpec(
    name="First EUR 10", objective="Generate the first EUR 10 of revenue from a digital product.",
    budget_eur=20, max_duration_days=30, max_human_hours=5, max_pivots=1, paid_ads_allowed=False,
    success_criteria=[{"metric": "sales", "op": ">=", "target": 1},
                      {"metric": "revenue_eur", "op": ">=", "target": 10}],
    checkpoint_days=[7, 14, 21],
)


def decide(d, conf=0.9, direction=""):
    return {"decision": d, "reason": "test", "confidence": conf, "new_direction": direction}


def t_create_experiment():
    c, _ = company()
    exp = c.experiments.create(SPEC)
    p = c.projects.get(exp.project_id)
    assert "paid_ads" not in p.permissions and "paid advertising NOT allowed" in p.objective
    return f"{exp.id} owns {p.id}; constraints written into objective"


def t_success():
    c, _ = company()
    exp = c.experiments.create(SPEC)
    c.experiments.record_metric(exp.id, "sales", 1, "etsy_stats")
    assert c.experiments.evaluate(exp.id).action == "continue"   # revenue not reached yet
    c.experiments.record_metric(exp.id, "revenue_eur", 12.5, "etsy_stats")
    ev = c.experiments.evaluate(exp.id)
    assert ev.action == "succeeded"
    assert c.projects.get(exp.project_id).status == ProjectStatus.FINISHED
    msg = expect(ManagementError, lambda: c.experiments.evaluate(exp.id))
    return f"all criteria met -> SUCCEEDED, project FINISHED; re-evaluate: {msg}"


def t_stop_conditions():
    reasons = []
    c, _ = company()
    exp = c.experiments.create(SPEC)
    reasons.append(c.experiments.evaluate(exp.id, now=exp.started_at + timedelta(days=31)).reason)
    c2, _ = company()
    exp2 = c2.experiments.create(SPEC)
    c2.experiments.log_human_hours(exp2.id, 5.5, "manual listing work")
    reasons.append(c2.experiments.evaluate(exp2.id).reason)
    assert c2.experiments.get(exp2.id).status == ExperimentStatus.FAILED
    return " | ".join(reasons)


def t_checkpoint_pivot_limits():
    c, client = company(decide("pivot", direction="Switch to wedding budget planners"),
                        decide("pivot", direction="Switch again"))
    exp = c.experiments.create(SPEC)
    start = exp.started_at
    ev = c.experiments.evaluate(exp.id, now=start + timedelta(days=3))
    assert ev.action == "continue" and not client.prompts, "AI called before checkpoint"
    ev1 = c.experiments.evaluate(exp.id, now=start + timedelta(days=7.5))
    ev2 = c.experiments.evaluate(exp.id, now=start + timedelta(days=14.5))
    assert ev1.action == "pivot" and ev2.action == "continue" and "refused" in ev2.reason
    assert "Pivot 1: Switch to wedding" in c.projects.get(exp.project_id).objective
    return "no AI before day 7; pivot 1 applied; pivot 2 refused (max 1)"


def t_checkpoint_stop_and_low_conf():
    c, _ = company(decide("stop", conf=0.4), decide("stop", conf=0.9))
    exp = c.experiments.create(SPEC)
    ev1 = c.experiments.evaluate(exp.id, now=exp.started_at + timedelta(days=8))
    ev2 = c.experiments.evaluate(exp.id, now=exp.started_at + timedelta(days=15))
    assert ev1.action == "continue" and ev2.action == "stopped"
    assert c.experiments.get(exp.id).status == ExperimentStatus.STOPPED
    return "stop at 0.4 deferred; stop at 0.9 -> STOPPED"


def t_pivot_needs_direction():
    c, _ = company(decide("pivot", direction=""), decide("continue"))
    exp = c.experiments.create(SPEC)
    ev = c.experiments.evaluate(exp.id, now=exp.started_at + timedelta(days=8))
    assert ev.action == "continue"
    return "pivot without new_direction rejected by schema, model retried"


check("create experiment", t_create_experiment)
check("success criteria", t_success)
check("stopping conditions", t_stop_conditions)
check("checkpoints + max pivots", t_checkpoint_pivot_limits)
check("stop decision + low confidence", t_checkpoint_stop_and_low_conf)
check("pivot needs direction", t_pivot_needs_direction)

# --- Part B: live ----------------------------------------------------------------------

print("\nPart B: live experiment (real models, several minutes)")


def t_live():
    c = Company(":memory:", None, None)
    exp = c.experiments.create(SPEC)
    plan_result = c.orchestrator.plan_project(exp.project_id)
    print(f"       plan ({plan_result.model_key}): {len(plan_result.accepted)} accepted, "
          f"{len(plan_result.rejected)} rejected")
    for task in plan_result.accepted:
        print(f"         [{task.department}] {task.description[:60]} {task.required_capabilities}")
    for desc, reason in plan_result.rejected:
        print(f"         x {desc[:45]} -> {reason}")
    for out in c.runner.run(max_tasks=3):
        detail = out.get("summary") or out.get("reason", "")
        extra = f" ({out['model']}, {out['seconds']}s)" if "model" in out else ""
        print(f"       run: {out['outcome']}{extra}: {detail[:90]}")
    c.experiments.record_metric(exp.id, "listing_views", 40, "etsy_stats")
    c.experiments.record_metric(exp.id, "sales", 0, "etsy_stats")
    ev = c.experiments.evaluate(exp.id, now=exp.started_at + timedelta(days=7.5))
    print(f"       day-7 checkpoint ({ev.model_key}): {ev.action} - {ev.reason[:90]}")
    return None


if "--offline" in sys.argv:
    print("(live part skipped: --offline)")
elif OllamaClient().is_available():
    check("live experiment", t_live)
else:
    results.append(False)
    print("[FAIL] Ollama not reachable")

print(f"\n{sum(results)}/{len(results)} tests passed")
sys.exit(0 if all(results) else 1)
