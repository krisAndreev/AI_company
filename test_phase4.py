"""
Phase 4 test: task model + persistent task queue.

Part A: queue rules, lifecycle and persistence (no model needed)
Part B: live - Gemma proposes a plan, code accepts it into the queue

Run:  python test_phase4.py
"""

import sys
import tempfile
from pathlib import Path

from clients import OllamaClient
from core.schemas import TaskPlan, TaskProposal
from core.structured import generate_structured
from core.task_queue import TaskQueue
from core.tasks import InvalidTransition, TaskStatus

MODEL = "gemma3:4b"
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append(True)
        print(f"[pass] {name}" + (f" -> {detail}" if detail else ""))
    except Exception as e:
        results.append(False)
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


def proposal(desc, priority=3, dept="research"):
    return TaskProposal(description=desc, department=dept, priority=priority,
                        estimated_cost=0, requires_approval=False)


def expect(exc_type, fn):
    try:
        fn()
    except exc_type as e:
        return str(e)
    raise AssertionError(f"expected {exc_type.__name__}")


# --- Part A ---------------------------------------------------------------

print("Part A: queue rules and lifecycle")
q = TaskQueue(":memory:")


def t_add():
    t = q.add(proposal("Research printable planner niches"), project_id="proj_demo")
    assert t.id.startswith("task_") and t.status == TaskStatus.READY
    assert q.get(t.id).description == t.description
    return f"id {t.id} assigned by code, status READY"


def t_unknown_dependency():
    return expect(ValueError, lambda: q.add(proposal("Write listing"), "proj_demo",
                                            dependencies=["task_doesnotexist"]))


def t_priority_order():
    q2 = TaskQueue(":memory:")
    q2.add(proposal("Low priority task", priority=4), "p")
    q2.add(proposal("High priority task", priority=1), "p")
    q2.add(proposal("Second high priority task", priority=1), "p")
    order = [q2.claim_next("w").description for _ in range(3)]
    assert order == ["High priority task", "Second high priority task", "Low priority task"], order
    assert q2.claim_next("w") is None
    return "P1 (oldest first), P1, P4, then None"


def t_dependencies():
    q3 = TaskQueue(":memory:")
    research = q3.add(proposal("Research niche"), "p")
    design = q3.add(proposal("Design product"), "p")
    listing = q3.add(proposal("Write listing"), "p", dependencies=[research.id, design.id])
    assert listing.status == TaskStatus.PENDING

    q3.claim_next("w1")
    q3.complete(research.id, "niche: wedding planners")
    assert q3.get(listing.id).status == TaskStatus.PENDING, "promoted too early"

    q3.claim_next("w1")
    q3.complete(design.id, "design done")
    assert q3.get(listing.id).status == TaskStatus.READY
    return "PENDING until BOTH dependencies completed, then READY"


def t_complete_fields():
    q4 = TaskQueue(":memory:")
    q4.add(proposal("Some task"), "p")
    t = q4.claim_next("worker_7")
    assert t.status == TaskStatus.RUNNING and t.assigned_worker == "worker_7" and t.started_at
    t = q4.complete(t.id, "done well", actual_cost=0.02)
    assert t.completed_at and t.result == "done well" and t.actual_cost == 0.02
    return "worker, started/completed times, result and actual cost recorded"


def t_invalid_transitions():
    q5 = TaskQueue(":memory:")
    t = q5.add(proposal("Some task"), "p")
    e1 = expect(InvalidTransition, lambda: q5.complete(t.id, "skipped running"))
    q5.claim_next("w")
    q5.complete(t.id, "ok")
    expect(InvalidTransition, lambda: q5.complete(t.id, "twice"))
    expect(InvalidTransition, lambda: q5.cancel(t.id))
    assert q5.get(t.id).result == "ok", "refused change still modified the task"
    return e1


def t_fail_retry():
    q6 = TaskQueue(":memory:")
    q6.add(proposal("Flaky task"), "p")
    t = q6.claim_next("w")
    t = q6.fail(t.id, "timeout talking to model")
    assert t.status == TaskStatus.FAILED and t.error
    t = q6.retry(t.id)
    assert t.status == TaskStatus.READY and t.error is None and t.assigned_worker is None
    return "FAILED (error kept) -> retry -> READY"


def t_wait_resume_cancel():
    q7 = TaskQueue(":memory:")
    t = q7.add(proposal("Buy EverBee subscription"), "p")
    t = q7.wait(t.id, "needs human approval")
    assert t.status == TaskStatus.WAITING and t.wait_reason
    t = q7.resume(t.id)
    assert t.status == TaskStatus.READY
    t = q7.cancel(t.id, "not needed")
    assert t.status == TaskStatus.CANCELLED
    return "READY -> WAITING -> READY -> CANCELLED"


def t_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        qa = TaskQueue(path)
        t = qa.add(proposal("Survive a restart", priority=2), "p")
        qa.close()
        qb = TaskQueue(path)
        loaded = qb.get(t.id)
        qb.close()
        assert loaded == t, "task changed after reload"
    return "task identical after closing and reopening the database"


def t_stats():
    s = q.stats()
    assert s["READY"] == 1
    return {k: v for k, v in s.items() if v}


check("add assigns id and status", t_add)
check("unknown dependency rejected", t_unknown_dependency)
check("priority ordering", t_priority_order)
check("dependencies gate READY", t_dependencies)
check("completion recorded", t_complete_fields)
check("invalid transitions refused", t_invalid_transitions)
check("fail and retry", t_fail_retry)
check("wait, resume, cancel", t_wait_resume_cancel)
check("persists across restart", t_persistence)
check("stats", t_stats)

# --- Part B: live ---------------------------------------------------------

print(f"\nPart B: AI proposes, code queues ({MODEL})")


def t_live():
    client = OllamaClient()
    plan = generate_structured(
        client, MODEL,
        "Objective: earn the first EUR 10 from a digital product. Budget EUR 20, no paid ads. "
        "Propose 3 to 5 first tasks.",
        TaskPlan,
        system="You are a project manager. Departments: research, marketing, development, "
               "content, design, data_analysis, finance, automation.",
    ).value
    lq = TaskQueue(":memory:")
    # Code decides structure: the first proposed task gates all the others.
    first = lq.add(plan.tasks[0], "proj_first_10_eur")
    for p in plan.tasks[1:]:
        lq.add(p, "proj_first_10_eur", dependencies=[first.id])
    for t in lq.list():
        print(f"       {t.status.value:<8} P{t.priority} [{t.department}] {t.description[:60]}")
    assert lq.stats()["READY"] == 1 and lq.stats()["PENDING"] == len(plan.tasks) - 1
    return f"{len(plan.tasks)} tasks queued: 1 READY, {len(plan.tasks) - 1} PENDING"


if "--offline" in sys.argv:
    print("(live part skipped: --offline)")
elif OllamaClient().is_available():
    check("live plan -> queue", t_live)
else:
    results.append(False)
    print("[FAIL] Ollama not reachable; start Ollama and rerun")

print(f"\n{sum(results)}/{len(results)} tests passed")
sys.exit(0 if all(results) else 1)
