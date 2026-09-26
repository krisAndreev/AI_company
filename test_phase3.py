"""
Phase 3 test: structured JSON output + Pydantic validation + safe retries.

Part A: offline checks of JSON extraction and schema rules (no model needed)
Part B: retry logic with a scripted fake model (deterministic)
Part C: live structured output from Gemma via Ollama

Run:  python test_phase3.py
"""

import sys
import time

from pydantic import ValidationError

from clients import ModelClient, ModelResponse, OllamaClient
from core.schemas import Decision, TaskPlan, TaskProposal
from core.structured import StructuredOutputError, extract_json, generate_structured

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


def expect_invalid(schema, data):
    try:
        schema.model_validate(data)
    except ValidationError as e:
        return e.errors()[0]["msg"]
    raise AssertionError(f"accepted invalid data: {data}")


GOOD_DECISION = {"decision": "continue", "reason": "on track", "confidence": 0.8,
                 "next_actions": ["write listing"]}

# --- Part A: offline -------------------------------------------------------

print("Part A: JSON extraction and schema rules")
check("plain JSON", lambda: extract_json('{"a": 1}'))
check("fenced JSON with chatter",
      lambda: extract_json('Sure! Here it is:\n```json\n{"a": 1}\n```\nHope that helps.'))
check("JSON embedded in text", lambda: extract_json('The answer is {"a": 1} ok'))


def t_garbage():
    for bad in ["no json here", "[1, 2, 3]", '{"a": ']:
        try:
            extract_json(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    return "rejected text, JSON array and truncated JSON"


check("garbage rejected", t_garbage)
check("valid Decision accepted", lambda: Decision.model_validate(GOOD_DECISION).decision)
check("confidence > 1 rejected", lambda: expect_invalid(Decision, {**GOOD_DECISION, "confidence": 1.5}))
check("unknown field rejected", lambda: expect_invalid(Decision, {**GOOD_DECISION, "spend": 500}))
check("missing field rejected", lambda: expect_invalid(Decision, {"decision": "x"}))
check("priority 9 rejected", lambda: expect_invalid(TaskProposal, {
    "description": "Research niches", "department": "research", "priority": 9,
    "estimated_cost": 0, "requires_approval": False}))
check("negative cost rejected", lambda: expect_invalid(TaskProposal, {
    "description": "Research niches", "department": "research", "priority": 1,
    "estimated_cost": -5, "requires_approval": False}))

# --- Part B: retry logic with a fake model ---------------------------------


class FakeClient(ModelClient):
    """Returns scripted replies in order, so retry behaviour is deterministic."""
    provider = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def is_available(self):
        return True

    def list_models(self):
        return ["fake"]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        self.calls.append(messages)
        return ModelResponse(text=self.replies.pop(0), model=model, provider="fake",
                             duration_seconds=0.0)


print("\nPart B: retry logic (fake model)")
GOOD_JSON = '{"decision": "continue", "reason": "ok", "confidence": 0.7, "next_actions": []}'


def t_retry_success():
    fake = FakeClient(["not json at all", '{"decision": "x", "confidence": 3}', GOOD_JSON])
    r = generate_structured(fake, "fake", "decide", Decision)
    assert r.attempts == 3 and r.value.confidence == 0.7
    last_feedback = fake.calls[2][-1].content
    assert "confidence" in last_feedback, "model was not told what was wrong"
    return f"succeeded on attempt {r.attempts}; model was shown its errors"


def t_retry_exhausted():
    fake = FakeClient(["nope", "still nope", "{}"])
    try:
        generate_structured(fake, "fake", "decide", Decision)
    except StructuredOutputError as e:
        assert len(e.errors) == 3
        return f"StructuredOutputError with {len(e.errors)} attempt errors"
    raise AssertionError("invalid output was accepted")


def t_first_try():
    r = generate_structured(FakeClient([GOOD_JSON]), "fake", "decide", Decision)
    assert r.attempts == 1
    return "no retry needed"


check("recovers after 2 bad replies", t_retry_success)
check("gives up safely after 3 bad replies", t_retry_exhausted)
check("valid first reply", t_first_try)

# --- Part C: live Gemma -----------------------------------------------------

print(f"\nPart C: live structured output ({MODEL})")
client = OllamaClient()


def t_live_decision():
    r = generate_structured(
        client, MODEL,
        "Experiment: sell a printable budget planner on Etsy. Budget EUR 20, EUR 3 spent, "
        "day 10 of 30, 40 listing views, 0 sales. Should we continue, pivot, or stop?",
        Decision,
        system="You are the head of a small online business. Be concise.",
    )
    d = r.value
    print(f"       decision={d.decision!r} confidence={d.confidence}")
    print(f"       reason={d.reason[:120]!r}")
    print(f"       next_actions={d.next_actions}")
    return f"valid in {r.attempts} attempt(s), {r.total_seconds:.1f}s"


def t_live_plan():
    r = generate_structured(
        client, MODEL,
        "Objective: earn the first EUR 10 from a digital product. Budget EUR 20, no paid ads. "
        "Propose 3 to 5 first tasks.",
        TaskPlan,
        system="You are a project manager. Departments: research, marketing, development, "
               "content, design, data_analysis, finance, automation.",
    )
    for t in r.value.tasks:
        print(f"       P{t.priority} [{t.department}] {t.description[:70]} "
              f"(EUR {t.estimated_cost}, approval={t.requires_approval})")
    return f"{len(r.value.tasks)} valid tasks in {r.attempts} attempt(s), {r.total_seconds:.1f}s"


if "--offline" in sys.argv:
    print("(live part skipped: --offline)")
elif client.is_available():
    check("live Decision", t_live_decision)
    check("live TaskPlan", t_live_plan)
else:
    results.append(False)
    print("[FAIL] Ollama not reachable; start Ollama and rerun")

print(f"\n{sum(results)}/{len(results)} tests passed")
sys.exit(0 if all(results) else 1)
