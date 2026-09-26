"""
Phase 19 test: the orchestrator runs the company (autopilot) + dashboard responsiveness.

Part A: chat actions - safe ones run at once, money/publishing/guidelines wait for the
        owner, invalid ones are refused by code, autopilot off = confirm everything
Part B: autopilot rounds - plan new projects, review + plan the next stage, finish at
        the stage limit, pause and ask when no plan is possible, tell the owner once
        about tasks that wait for them
Part C: dashboard - talking to the orchestrator starts the work loop; slow Ollama /
        GPU look-ups never freeze other requests; localhost -> 127.0.0.1

All offline (scripted model), runs in seconds.  Run:  python test_phase19.py
"""

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from clients import ModelClient, ModelResponse, OllamaClient
from core.chat import ChatError
from core.company import Company
from core.projects import ProjectStatus
from core.schemas import TaskProposal
from core.tasks import TaskStatus
from dashboard.auth import AuthStore
from dashboard.config_admin import ConfigAdmin
from dashboard.server import create_app

results = []
TMP = Path(tempfile.mkdtemp(prefix="phase19_"))


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


class SchemaFake(ModelClient):
    provider = "ollama"

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []
        self.prompts = []

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        title = (json_schema or {}).get("title", "")
        self.calls.append(title)
        self.prompts.append("\n".join(m.content for m in messages))
        h = self.handlers[title]
        reply = h.pop(0) if isinstance(h, list) and len(h) > 1 else h[0] if isinstance(h, list) else h
        return ModelResponse(text=json.dumps(reply), model=model, provider="ollama", duration_seconds=0)


class FakeControl:
    def __init__(self):
        self.state = "paused"

    def start(self):
        self.state = "running"

    def pause(self):
        self.state = "paused"

    def status(self):
        return self.state


PLAN = {"tasks": [{"description": "Research what busy parents buy", "department": "research",
                   "priority": 1, "required_capabilities": ["market_research"],
                   "estimated_cost": 0, "requires_approval": False, "depends_on": []},
                  {"description": "Write the product listing", "department": "content",
                   "priority": 2, "required_capabilities": ["write_text"], "estimated_cost": 0,
                   "requires_approval": False, "depends_on": [1]}]}


def reply(text, *actions):
    return {"reply": text, "actions": list(actions)}


def act(type_, **kw):
    return {"type": type_, "reason": kw.pop("reason", "owner asked"), **kw}


def company(*chat_replies, plan=PLAN, decision=None, name="c"):
    handlers = {"OrchestratorReply": list(chat_replies) or [reply("ok")], "ProjectPlan": plan,
                "ProjectDecision": decision or {"decision": "continue", "reason": "going well",
                                                "confidence": 0.9, "next_actions": []},
                "TaskResult": {"success": True, "summary": "done", "output": "result"}}
    fake = SchemaFake(handlers)
    c = Company(":memory:", {"ollama": fake}, None, workspace_dir=TMP / name)
    c.control = FakeControl()
    return c, fake


def say(c, text):
    c.chat.add_owner_message(text)
    return c.chat.respond()


# --- Part A -------------------------------------------------------------------------------------

print("Part A: orchestrator actions")


def t_create_runs_itself():
    c, fake = company(reply("On it: I created the project and started work.",
                            act("create_project", name="Busy parents planner",
                                objective="Make and sell a printable weekly planner for busy parents",
                                budget_eur=15, priority=1)))
    m = say(c, "Make and launch a printable product for busy parents, budget 15 EUR")
    e = m["actions"][0]
    assert e["status"] == "executed" and not e["needs_owner"], e
    p = c.projects.list()[0]
    assert len(c.queue.list(project_id=p.id)) == 2 and "planned 2 tasks" in e["result"]
    assert c.control.status() == "running"
    assert any(x["action"] == "Carried out action" and x["actor"] == "MASTER_ORCHESTRATOR"
               for x in c.events.recent(30))
    return f"'{e['result']}'; work loop started - no clicks needed"


def t_owner_gate():
    c, _ = company(reply("Here is my plan.",
                         act("create_project", name="Big", objective="Big bet", budget_eur=40),
                         act("add_guideline", content="Always prefer printables"),
                         act("start_work")))
    m = say(c, "go big")
    st = [(e["action"]["type"], e["status"]) for e in m["actions"]]
    assert st == [("create_project", "proposed"), ("add_guideline", "proposed"),
                  ("start_work", "executed")], st
    msg = expect(ChatError, lambda: c.chat.execute(m["id"], 0, actor="MASTER_ORCHESTRATOR"))
    e = c.chat.execute(m["id"], 1)                    # the owner clicks "Do it"
    assert e["status"] == "executed" and e["by"] == "HUMAN" and c.memory.recall("personal")
    return f"budget 40 > 25 and guidelines wait for the owner ('{msg}'); start_work ran"


def t_add_task_and_invalid():
    c, fake = company(name="add")
    p = c.orchestrator.create_project("Shop", "Sell planners", 10)
    fake.handlers["OrchestratorReply"] = [reply(
        "Adding tasks.",
        act("add_task", project_id=p.id, department="design", capability="create_graphics",
            description="Create 3 square Instagram images announcing the planner"),
        act("add_task", project_id=p.id, department="design", capability="hack_bank",
            description="x" * 10),
        act("pause_project", project_id="proj_invented"))]
    m = say(c, "Make some Instagram images")
    st = [e["status"] for e in m["actions"]]
    assert st == ["executed", "invalid", "invalid"], st
    task = c.queue.list(project_id=p.id)[0]
    assert task.status == TaskStatus.READY and task.required_capabilities == ["create_graphics"]
    fake.handlers["OrchestratorReply"] = [reply("Again.", act(
        "add_task", project_id=p.id, department="design", capability="create_graphics",
        description="Create 3 square Instagram images announcing the new planner"))]
    again = say(c, "what do you need?")["actions"][0]
    assert again["status"] == "invalid" and "already queued" in again["problem"]
    return f"task added READY with its tool capability; {m['actions'][1]['problem']}; " \
           f"{m['actions'][2]['problem']}; repeated task refused"


def t_complete_and_approve():
    c, _ = company(name="waiting")
    p = c.orchestrator.create_project("Shop", "Sell", 10)
    manual = c.queue.add(TaskProposal(description="Publish the listing on Etsy", department="content",
                                      priority=1, required_capabilities=["publish_content"],
                                      estimated_cost=0, requires_approval=False), p.id)
    money = c.queue.add(TaskProposal(description="Buy mockup bundle", department="finance",
                                     priority=1, required_capabilities=["budget_analysis"],
                                     estimated_cost=5, requires_approval=False), p.id)
    for t, why in ((manual, "manual capabilities ['publish_content']"), (money, "EUR 5 above threshold")):
        c.queue.start(t.id, "w")
        c.queue.wait(t.id, why)
    c.router.clients["ollama"].handlers["OrchestratorReply"] = [reply(
        "Great, marking it done.",
        act("complete_task", task_id=manual.id, result="Listed at etsy.com/listing/123"),
        act("complete_task", task_id=money.id, result="done"),
        act("approve_task", task_id=money.id))]
    m = say(c, "I published the listing: etsy.com/listing/123. And yes, buy the bundle.")
    st = [e["status"] for e in m["actions"]]
    assert st == ["executed", "invalid", "proposed"], st
    assert c.queue.get(manual.id).status == TaskStatus.COMPLETED
    assert "etsy.com/listing/123" in c.queue.get(manual.id).result
    assert c.queue.get(money.id).status == TaskStatus.WAITING       # money waits for a click
    c.chat.execute(m["id"], 2)
    assert c.queue.get(money.id).status == TaskStatus.READY
    return "owner's report closed the manual task; spending approval needed the owner's click"


def t_project_name_or_missing_id():
    c, fake = company(name="resolve")
    p = c.orchestrator.create_project("Meal Planner", "Sell meal planners", 10)
    fake.handlers["OrchestratorReply"] = [reply(
        "Adding.", act("add_task", project_id="", department="content", capability="write_text",
                       description="Write the Etsy listing"),
        act("add_task", project_id="meal planner", department="Research",
            capability="web_research (tool)", description="Research competitor prices"))]
    m = say(c, "add a listing and research prices")
    assert [e["status"] for e in m["actions"]] == ["executed", "executed"], m["actions"]
    assert {e["action"]["project_id"] for e in m["actions"]} == {p.id}
    c.orchestrator.create_project("Second", "x", 5)
    fake.handlers["OrchestratorReply"] = [reply("?", act("add_task", project_id="", department="content",
                                                         capability="write_text", description="Do it"))]
    m = say(c, "add a task")
    assert m["actions"][0]["status"] == "invalid"
    return "empty id -> the only active project; a name -> its id; ambiguous -> refused"


def t_refusal_explained():
    c, _ = company(reply("Creating it.", act("create_project", name="Too big", objective="x",
                                             budget_eur=20)), name="refuse")
    c.config.total_budget_eur = 12
    c.orchestrator.create_project("Existing", "y", 8)
    m = say(c, "make another product")
    assert m["actions"][0]["status"] == "failed"
    follow = c.chat.history()[-1]
    assert follow["model"] == "autopilot" and "Only EUR 4.00 is free" in follow["content"], follow
    snap = json.loads(c.chat.snapshot())
    assert snap["company_budget"]["unallocated_eur"] == 4.0
    return follow["content"][:110]


def t_project_chat():
    c, fake = company(name="threads")
    p = c.orchestrator.create_project("Meal planner", "Sell a weekly meal planner", 10)
    t = c.queue.add(TaskProposal(description="Research what parents want", department="research",
                                 priority=1, required_capabilities=["market_research"],
                                 estimated_cost=0, requires_approval=False), p.id)
    c.queue.start(t.id, "w")
    c.queue.complete(t.id, "Parents want 15-minute recipes and a shopping list.\n\nFiles produced "
                           "(asset ids): - report: x")
    fake.handlers["OrchestratorReply"] = [reply(
        "Research says parents want 15-minute recipes and a shopping list.",
        act("add_task", department="content", capability="write_text",
            description="Write 20 fifteen-minute recipes for the planner"))]
    c.chat.add_owner_message("What did the research find? Add recipes.", thread=p.id)
    m = c.chat.respond(thread=p.id)
    prompt = fake.prompts[-1]
    assert "15-minute recipes" in prompt and "ONE project" in prompt and "Files produced" not in prompt
    assert m["actions"][0]["status"] == "executed" and m["actions"][0]["action"]["project_id"] == p.id
    assert c.chat.history() == [] and len(c.chat.history(thread=p.id)) == 2
    ids = [x["id"] for x in c.chat.threads()]
    assert ids == ["main", p.id], ids
    return "project chat sees task results; its actions default to its project; main chat untouched"


def t_no_duplicate_project():
    c, fake = company(name="dup")
    c.orchestrator.create_project("Digital Product 1", "Research PDF products for busy parents", 5)
    fake.handlers["OrchestratorReply"] = [reply("Creating.", act(
        "create_project", name="Digital Product 1", objective="Research PDF products for busy "
        "parents: find high-demand planners", budget_eur=5))]
    m = say(c, "Can you report back to me with the research results?")
    e = m["actions"][0]
    assert e["status"] == "invalid" and "already exists" in e["problem"] and len(c.projects.list()) == 1
    return e["problem"][:90]


def t_delete_project():
    c, _ = company(name="delete")
    p = c.orchestrator.create_project("Old", "x", 5)
    keep = c.orchestrator.create_project("Keep", "y", 5)
    c.autopilot.step()
    c.chat.add_owner_message("hello", thread=p.id)
    folder = c.workspace.dir_for(p.id, "images")
    img = folder / "a.png"
    from PIL import Image
    Image.new("RGB", (10, 10)).save(img)
    c.assets.register(img, "image", "a", "test", p.id)
    running = c.queue.list(TaskStatus.READY, p.id)[0]
    c.queue.start(running.id, "w")
    msg = expect(Exception, lambda: c.delete_project(p.id))
    c.queue.complete(running.id, "done")
    expect(Exception, lambda: c.delete_project(p.id, who="MASTER_ORCHESTRATOR"))
    r = c.delete_project(p.id)
    assert [x.id for x in c.projects.list()] == [keep.id]
    assert c.queue.list(project_id=p.id) == [] and c.assets.list(project_id=p.id) == []
    assert c.chat.history(thread=p.id) == [] and not folder.exists()
    assert any(e["action"] == "Deleted project" for e in c.events.recent(20))
    return f"refused while a task runs ('{msg[:40]}...') and for the AI; then removed {r}"


def t_confirm_mode():
    c, _ = company(reply("Proposal.", act("create_project", name="A", objective="B", budget_eur=5)),
                   name="confirm")
    c.config.autopilot = False
    m = say(c, "start something")
    assert m["actions"][0]["status"] == "proposed" and c.projects.list() == []
    assert c.control.status() == "paused"
    return "autopilot off -> every action waits for the owner (old behaviour)"


check("create_project runs itself: created, planned, loop started", t_create_runs_itself)
check("owner gate: budget above limit / guidelines", t_owner_gate)
check("add_task + invalid actions refused by code", t_add_task_and_invalid)
check("complete_task from the owner's report; approvals need a click", t_complete_and_approve)
check("project given by name / missing id", t_project_name_or_missing_id)
check("refused action -> explanation + question for the owner", t_refusal_explained)
check("project chat: details, defaults, separate from main", t_project_chat)
check("question does not create a duplicate project", t_no_duplicate_project)
check("delete project (owner only, not while running)", t_delete_project)
check("autopilot off = confirm everything", t_confirm_mode)

# --- Part B --------------------------------------------------------------------------------------

print("\nPart B: autopilot rounds")


def finish_all(c, project_id):
    """Complete every task of the project (dependencies first), as the work loop would."""
    for _ in range(10):
        ready = c.queue.list(TaskStatus.READY, project_id)
        if not ready:
            break
        for t in ready:
            c.queue.start(t.id, "w")
            c.queue.complete(t.id, "done")


def t_stages():
    c, fake = company(name="stages")
    p = c.orchestrator.create_project("Planner shop", "Sell planners", 10)
    notes = c.autopilot.step()
    assert len(c.queue.list(project_id=p.id)) == 2 and "stage 1 planned" in notes[0]
    assert c.autopilot.step() == []                  # tasks open: nothing to do
    finish_all(c, p.id)
    notes = c.autopilot.step()
    assert "ProjectDecision" in fake.calls and "stage 2 planned" in notes[0], notes
    msgs = [m["content"] for m in c.chat.history(thread=p.id) if m["model"] == "autopilot"]
    assert len(msgs) == 2 and all("Planner shop" in x for x in msgs)
    assert c.chat.history() == []                      # the main chat stays uncluttered
    return "new project -> stage 1; all done -> AI review -> stage 2; updates in the project's chat"


def t_finish_by_review_and_limit():
    c, _ = company(decision={"decision": "finish", "reason": "product is live and selling",
                             "confidence": 0.9, "next_actions": []}, name="finish")
    p = c.orchestrator.create_project("P", "x", 5)
    c.autopilot.step()
    finish_all(c, p.id)
    notes = c.autopilot.step()
    assert c.projects.get(p.id).status == ProjectStatus.FINISHED and "finished" in notes[0]
    c2, _ = company(name="limit")
    c2.config.max_stages_per_project = 1
    p2 = c2.orchestrator.create_project("Q", "y", 5)
    c2.autopilot.step()
    finish_all(c2, p2.id)
    notes = c2.autopilot.step()
    assert c2.projects.get(p2.id).status == ProjectStatus.FINISHED and "stage limit" in notes[0]
    return "AI 'finish' ends the project; the code stage limit ends it too"


def t_empty_plan_asks():
    bad = {"tasks": [{"description": "Hack a bank", "department": "crime", "priority": 1,
                      "required_capabilities": [], "estimated_cost": 0, "requires_approval": False,
                      "depends_on": []}]}
    c, _ = company(plan=bad, name="empty")
    p = c.orchestrator.create_project("P", "x", 5)
    notes = c.autopilot.step()
    assert c.projects.get(p.id).status == ProjectStatus.PAUSED and "What should we do next?" in notes[0]
    return "no usable tasks -> paused + a question for the owner"


def t_notify_once():
    c, _ = company(name="notify")
    p = c.orchestrator.create_project("P", "x", 5)
    t = c.queue.add(TaskProposal(description="Publish the listing", department="content", priority=1,
                                 required_capabilities=["publish_content"], estimated_cost=0,
                                 requires_approval=False), p.id)
    c.queue.start(t.id, "w")
    c.queue.wait(t.id, "manual capabilities ['publish_content']: a human must do this")
    first, second = c.autopilot.step(), c.autopilot.step()
    assert len(first) == 1 and "I need you" in first[0] and t.id in first[0] and second == []
    return "waiting task announced once in the chat, with its id"


check("stage by stage planning", t_stages)
check("finish by AI review / by stage limit", t_finish_by_review_and_limit)
check("empty plan -> pause + ask", t_empty_plan_asks)
check("tell the owner once about waiting tasks", t_notify_once)

# --- Part C -----------------------------------------------------------------------------------------

print("\nPart C: dashboard")
cfg = TMP / "config"
shutil.copytree("config", cfg, ignore=shutil.ignore_patterns("history"))
auth = AuthStore(TMP / "auth.json")
auth.set_password("test-password-123")
H = {"X-Requested-With": "dashboard"}
API_FAKE = SchemaFake({"OrchestratorReply": [reply("Creating it now.", act(
    "create_project", name="Parents planner", objective="Sell a weekly planner", budget_eur=10))],
    "ProjectPlan": PLAN})


class SlowPool:
    """A node pool whose status look-up takes 2 s (like a slow or remote node)."""
    def status(self):
        time.sleep(2)
        return [{"name": "slow-node"}]


def make_company():
    c = Company(TMP / "api.db", {"ollama": API_FAKE}, None, config_dir=cfg, workspace_dir=TMP / "ws")
    c.resources = SlowPool()
    return c


app = create_app(make_company, auth, ConfigAdmin(cfg, TMP / "history"), start_services=False)
with TestClient(app) as client:
    client.post("/api/login", json={"password": "test-password-123"})

    def t_chat_starts_work():
        app.state.jobs.launch()
        r = client.post("/api/chat", headers=H, json={"message": "Make a planner for parents"}).json()
        for _ in range(100):
            j = client.get(f"/api/jobs/{r['job']['id']}").json()
            if j["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert j["status"] == "done", j
        state = client.get("/api/state").json()
        assert state["runner"]["state"] == "running" and state["autopilot"] is True
        assert [p["name"] for p in state["projects"]] == ["Parents planner"]
        return "chat message -> project created + planned -> work loop running"

    def t_no_freeze():
        took = {}

        def slow():
            t = time.perf_counter()
            client.get("/api/resources")
            took["resources"] = time.perf_counter() - t
        th = threading.Thread(target=slow)
        th.start()
        time.sleep(0.3)
        t = time.perf_counter()
        client.get("/api/state")
        took["state"] = time.perf_counter() - t
        th.join()
        t = time.perf_counter()
        client.get("/api/resources")                  # cached for 3 s
        took["cached"] = time.perf_counter() - t
        assert took["state"] < 0.8 and took["cached"] < 0.5, took
        return (f"slow node look-up {took['resources']:.1f}s did not block /api/state "
                f"({took['state']:.2f}s); repeat {took['cached']:.2f}s from cache")

    check("chat starts the work loop (API)", t_chat_starts_work)
    check("slow look-ups never freeze the dashboard", t_no_freeze)


def t_keep_awake():
    from core.power import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake
    from dashboard.services import RunnerService
    calls = []
    ka = KeepAwake(setter=calls.append)
    ka.set(True), ka.set(True), ka.set(False)
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS], calls
    c, _ = company(name="power")
    rs = RunnerService(lambda: c)
    rs._keep_awake = KeepAwake(setter=lambda f: None)
    rs._running = True
    p = c.orchestrator.create_project("P", "x", 5)
    t = c.queue.add(TaskProposal(description="Write listing", department="content", priority=1,
                                 required_capabilities=["write_text"], estimated_cost=0,
                                 requires_approval=False), p.id)
    now = time.time()
    rs._power(c, now)
    working = rs._has_work(c) and rs._keep_awake.active
    rs._power(c, now - 3 * 60 - 5)
    after_grace = rs._keep_awake.active
    c.queue.start(t.id, "w")
    c.queue.wait(t.id, "manual: publish")
    waiting = rs._has_work(c)
    rs._running = False
    rs._power(c, now)
    paused = rs._keep_awake.active
    real = KeepAwake()
    real.set(True), real.set(False)                    # the real Windows call works
    assert working and not after_grace and not waiting and not paused
    return "awake while tasks run; sleep allowed after 3 min idle, when only waiting for you, or paused"


def t_localhost():
    assert OllamaClient("http://localhost:11434").base_url == "http://127.0.0.1:11434"
    return "localhost rewritten to 127.0.0.1 (avoids the 2 s IPv6 fallback on Windows)"


check("Ollama URL uses IPv4 loopback", t_localhost)
check("keep PC awake only while working", t_keep_awake)

print(f"\n{sum(results)}/{len(results)} tests passed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if all(results) else 1)
