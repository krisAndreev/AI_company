"""
Phase 15 test: multi-computer resource management + talking to the orchestrator.

Part A: node pool (routing, GPU slots, failover) with fake nodes
Part B: orchestrator chat (proposals, validation, confirmation) with a scripted model
Part C: dashboard API for resources + chat
Part D: live - real node status (nvidia-smi, psutil, Ollama) and a real chat reply

Run:  python test_phase15.py
"""

import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from clients import ModelClient, ModelClientError, ModelResponse
from core.chat import ChatError
from core.company import Company
from core.resources import NodePool, NodesConfig, parse_nvidia_smi, reset_slots_for_tests
from dashboard.auth import AuthStore
from dashboard.config_admin import ConfigAdmin
from dashboard.server import create_app

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


# --- Part A: node pool ----------------------------------------------------------------------

class FakeNode(ModelClient):
    """A fake Ollama server: installed/loaded models, optional delay, can be 'down'."""
    registry: dict = {}

    def __init__(self, base_url):
        self.url = base_url
        self.spec = FakeNode.registry[base_url]
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def is_available(self):
        return not self.spec.get("down")

    def list_models(self):
        if self.spec.get("down"):
            raise ModelClientError("down")
        return self.spec["installed"]

    def loaded_models(self):
        return [{"name": m, "size": 1, "size_vram": 1} for m in self.spec.get("loaded", [])]

    def version(self):
        return "fake"

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.spec.get("delay", 0))
        with self.lock:
            self.active -= 1
        return ModelResponse(text="{}", model=model, provider="ollama", duration_seconds=0)


def pool(nodes: dict, specs: dict) -> NodePool:
    reset_slots_for_tests()
    FakeNode.registry = specs
    cfg = NodesConfig.model_validate({"nodes": nodes})
    made = {}

    def factory(base_url):
        made[base_url] = FakeNode(base_url)
        return made[base_url]

    p = NodePool(cfg, client_factory=factory, acquire_timeout=5, cache_seconds=0)
    p.made = made
    return p


def node(url, enabled=True, slots=1):
    return {"enabled": enabled, "ollama_url": url, "max_concurrent_gpu_tasks": slots}


print("Part A: node pool")


def t_union_and_routing():
    p = pool({"pc": node("a"), "laptop": node("b")},
             {"a": {"installed": ["gemma3:4b", "qwen3:4b"]}, "b": {"installed": ["phi:2b"]}})
    assert p.list_models() == ["gemma3:4b", "phi:2b", "qwen3:4b"]
    r = p.chat([], "phi:2b")
    assert r.raw["node"] == "laptop"
    return "models from all nodes; phi:2b routed to the only node that has it"


def t_prefer_loaded():
    p = pool({"pc": node("a"), "pc2": node("b")},
             {"a": {"installed": ["qwen3:4b"]}, "b": {"installed": ["qwen3:4b"], "loaded": ["qwen3:4b"]}})
    assert p.chat([], "qwen3:4b").raw["node"] == "pc2"
    return "node with the model already in memory preferred (no swap)"


def t_gpu_slot_serialises():
    p = pool({"pc": node("a", slots=1)}, {"a": {"installed": ["m"], "delay": 0.3}})
    start = time.perf_counter()
    threads = [threading.Thread(target=p.chat, args=([], "m")) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    took = time.perf_counter() - start
    assert p.made["a"].max_active == 1 and took >= 0.85, (p.made["a"].max_active, took)
    return f"3 simultaneous calls, max 1 on the GPU at once ({took:.2f}s total)"


def t_two_nodes_parallel():
    p = pool({"pc": node("a"), "pc2": node("b")},
             {"a": {"installed": ["m"], "delay": 0.4}, "b": {"installed": ["m"], "delay": 0.4}})
    start = time.perf_counter()
    threads = [threading.Thread(target=p.chat, args=([], "m")) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    took = time.perf_counter() - start
    assert took < 0.75, took
    return f"2 calls spread over 2 nodes in parallel ({took:.2f}s)"


def t_failover_and_disabled():
    p = pool({"pc": node("a"), "old": node("c", enabled=False), "pc2": node("b")},
             {"a": {"installed": ["m"], "down": True}, "b": {"installed": ["m"]},
              "c": {"installed": ["m"]}})
    assert p.chat([], "m").raw["node"] == "pc2" and "c" not in p.made
    msg = expect(ModelClientError, lambda: p.chat([], "missing:1b"))
    return f"down node skipped, disabled node never contacted; {msg}"


def t_status_and_smi():
    p = pool({"pc": node("a")}, {"a": {"installed": ["m"], "loaded": ["m"]}})
    s = p.status()[0]
    gpus = parse_nvidia_smi("NVIDIA GeForce GTX 1650 SUPER, 15, 907, 4096, 43\n")
    assert s["reachable"] and s["loaded"] and gpus[0]["vram_total_mb"] == 4096
    return "node status + nvidia-smi parsing"


check("model union + routing", t_union_and_routing)
check("prefer already-loaded model", t_prefer_loaded)
check("GPU slot serialises calls", t_gpu_slot_serialises)
check("two nodes run in parallel", t_two_nodes_parallel)
check("failover + disabled nodes", t_failover_and_disabled)
check("status + nvidia-smi parser", t_status_and_smi)

# --- Part B: chat -----------------------------------------------------------------------------


class ChatFake(ModelClient):
    provider = "ollama"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        self.prompts.append(messages[-1].content)
        return ModelResponse(text=json.dumps(self.replies.pop(0)), model=model,
                             provider="ollama", duration_seconds=0)


print("\nPart B: talking to the orchestrator")


def t_chat_flow():
    fake = ChatFake([])
    c = Company(":memory:", {"ollama": fake}, None)
    c.config.autopilot = False   # the confirm-everything mode
    p = c.orchestrator.create_project("Planner shop", "Sell planners", 10)
    fake.replies.append({"reply": "Planner shop has no tasks yet. I suggest planning it.",
                         "actions": [
                             {"type": "plan_project", "project_id": p.id, "reason": "no tasks"},
                             {"type": "pause_project", "project_id": "proj_invented", "reason": "x"},
                             {"type": "add_guideline", "content": "Keep weekly effort under 2 h",
                              "reason": "you said so"},
                             ]})
    c.chat.add_owner_message("How is the planner shop doing?")
    reply = c.chat.respond()
    assert "Planner shop" in fake.prompts[-1] and p.id in fake.prompts[-1], "snapshot missing"
    statuses = [a["status"] for a in reply["actions"]]
    assert statuses == ["proposed", "invalid", "proposed"], statuses
    assert c.queue.list(project_id=p.id) == [], "an action ran without confirmation"
    assert c.memory.recall("personal") == []
    return "reply stored; invented project id marked invalid by code; nothing ran unconfirmed"


def t_confirm_and_dismiss():
    fake = ChatFake([])
    c = Company(":memory:", {"ollama": fake}, None)
    c.config.autopilot = False   # the confirm-everything mode
    fake.replies.append({"reply": "Here are two ideas.", "actions": [
        {"type": "create_project", "name": "Ebook", "objective": "Sell an ebook",
         "budget_eur": 15, "priority": 2, "reason": "low effort"},
        {"type": "add_guideline", "content": "Prefer ebooks", "reason": "fits you"}]})
    c.chat.add_owner_message("Ideas?")
    m = c.chat.respond()
    e = c.chat.execute(m["id"], 0)
    assert e["status"] == "executed" and c.projects.list()[0].name == "Ebook"
    expect(ChatError, lambda: c.chat.execute(m["id"], 0))   # no double execution
    c.chat.dismiss(m["id"], 1)
    assert c.memory.recall("personal") == []
    humans = [x["action"] for x in c.events.recent(20) if x["actor"] == "HUMAN"]
    assert "Confirmed orchestrator action" in humans and "Dismissed orchestrator action" in humans
    return "confirmed create_project ran through normal rules; dismissed one did nothing; both logged"


def t_rules_still_apply():
    fake = ChatFake([{"reply": "Big idea.", "actions": [
        {"type": "create_project", "name": "Huge", "objective": "x", "budget_eur": 999,
         "reason": "why not"}]}])
    c = Company(":memory:", {"ollama": fake}, None)
    c.config.autopilot = False   # the confirm-everything mode
    c.chat.add_owner_message("Go big")
    m = c.chat.respond()
    msg = expect(Exception, lambda: c.chat.execute(m["id"], 0))
    assert c.chat.get(m["id"])["actions"][0]["status"] == "failed"
    return f"company budget rule still enforced: {msg[:70]}"


check("chat: reply + validated proposals", t_chat_flow)
check("chat: confirm / dismiss", t_confirm_and_dismiss)
check("chat: company rules still apply", t_rules_still_apply)

# --- Part C: API ------------------------------------------------------------------------------

print("\nPart C: dashboard API")
tmp = Path(tempfile.mkdtemp())
cfg = tmp / "config"
shutil.copytree("config", cfg, ignore=shutil.ignore_patterns("history"))
auth = AuthStore(tmp / "auth.json")
auth.set_password("test-password-123")
reset_slots_for_tests()
FakeNode.registry = {"http://127.0.0.1:11434": {"installed": ["gemma3:4b", "qwen3:4b"]}}
H = {"X-Requested-With": "dashboard"}


def make_company():
    """A real NodePool (real GPU slots) over a fake node that answers with a fixed reply."""
    nodes = NodesConfig.load(cfg / "nodes.json")
    p = NodePool(nodes, client_factory=lambda base_url: ReplyNode(base_url), cache_seconds=0)
    return Company(tmp / "c.db", {"ollama": p}, None, config_dir=cfg)


class ReplyNode(FakeNode):
    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        return ModelResponse(text=json.dumps({"reply": "All quiet on the fleet.", "actions": [
            {"type": "add_guideline", "content": "Check the fleet daily", "reason": "habit"}]}),
            model=model, provider="ollama", duration_seconds=0)


app = create_app(make_company, auth, ConfigAdmin(cfg, tmp / "history"))
with TestClient(app) as client:
    client.post("/api/login", json={"password": "test-password-123"})

    def wait_job(job_id):
        for _ in range(75):
            j = client.get(f"/api/jobs/{job_id}").json()
            if j["status"] in ("done", "failed"):
                return j
            time.sleep(0.2)
        raise AssertionError("job timeout")

    def t_api_resources():
        d = client.get("/api/resources").json()
        main = next(n for n in d["nodes"] if n["name"] == "main-pc")
        laptop = next(n for n in d["nodes"] if n["name"] == "laptop")
        assert main["reachable"] and not laptop["enabled"] and "metrics" in main
        return f"{len(d['nodes'])} nodes; local metrics: {sorted(main['metrics'])}"

    def t_api_chat():
        r = client.post("/api/chat", headers=H, json={"message": "Status?"}).json()
        assert wait_job(r["job"]["id"])["status"] == "done"
        msgs = client.get("/api/chat").json()
        assert [m["role"] for m in msgs] == ["owner", "orchestrator"]
        j = client.post(f"/api/chat/{msgs[1]['id']}/actions/0/execute", headers=H).json()
        assert wait_job(j["id"])["status"] == "done"
        again = client.post(f"/api/chat/{msgs[1]['id']}/actions/0/execute", headers=H)
        items = client.get("/api/memory").json()["items"]
        assert again.status_code == 400 and any(i["content"] == "Check the fleet daily" for i in items)
        return "send -> background reply -> confirm action -> guideline saved; re-confirm refused"

    def t_nodes_config_editable():
        text = client.get("/api/config/nodes.json").json()["text"]
        bad = client.post("/api/config/nodes.json/validate", headers=H,
                          json={"text": text.replace('"max_concurrent_gpu_tasks": 1', '"max_concurrent_gpu_tasks": 0', 1)}).json()
        assert bad["problems"]
        return "nodes.json editable + validated in the dashboard"

    check("API: resources", t_api_resources)
    check("API: chat end to end", t_api_chat)
    check("API: nodes.json config", t_nodes_config_editable)
shutil.rmtree(tmp, ignore_errors=True)

# --- Part D: live -------------------------------------------------------------------------------

print("\nPart D: live (real machine + real model)")
reset_slots_for_tests()


def t_live_status():
    c = Company(":memory:", None, None)
    s = next(n for n in c.resources.status() if n["name"] == "main-pc")
    g = (s["metrics"].get("gpus") or [{}])[0]
    print(f"       main-pc reachable={s['reachable']} models={s['installed']}")
    print(f"       GPU {g.get('name')}: {g.get('utilization_pct')}% util, "
          f"{g.get('vram_used_mb')}/{g.get('vram_total_mb')} MB, {g.get('temperature_c')}°C; "
          f"CPU {s['metrics'].get('cpu_pct')}%, RAM {s['metrics'].get('ram_used_gb')}/"
          f"{s['metrics'].get('ram_total_gb')} GB")
    assert s["reachable"] and g.get("vram_total_mb")
    return None


def t_live_chat():
    c = Company(":memory:", None, None)
    c.orchestrator.create_project("Printable planners", "Earn first EUR 10 on Etsy", 20, 1)
    c.chat.add_owner_message("What should I focus on first, and is anything waiting for me?")
    m = c.chat.respond()
    print(f"       reply ({m['model']}): {m['content'][:200]}")
    for a in m["actions"]:
        print(f"       proposal: {a['action']['type']} -> {a['status']} {a['problem'] or ''}")
    node = c.resources.status()[0]["slots"]
    return f"{node['calls']} call(s) through the node pool on main-pc"


try:
    live_ok = Company(":memory:", None, None).resources.is_available()
except Exception:
    live_ok = False
if "--offline" in sys.argv:
    print("(live part skipped: --offline)")
elif live_ok:
    check("live node status", t_live_status)
    check("live chat reply", t_live_chat)
else:
    results.append(False)
    print("[FAIL] Ollama not reachable")

print(f"\n{sum(results)}/{len(results)} tests passed")
sys.exit(0 if all(results) else 1)
