"""
Phase 14 test: dashboard server (auth, security, API, background loop, config editing).

Runs the real FastAPI app in-process with a temporary database, a COPY of the
config folder (your real config is never touched) and a schema-aware fake model.

Run:  python test_phase14.py
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from clients import ModelClient, ModelResponse
from core.company import Company
from dashboard.auth import AuthStore
from dashboard.config_admin import ConfigAdmin
from dashboard.server import create_app

results = []
PASSWORD = "test-password-123"
H = {"X-Requested-With": "dashboard"}


def check(name, fn):
    try:
        detail = fn()
        results.append(True)
        print(f"[pass] {name}" + (f" -> {detail}" if detail else ""))
    except Exception as e:
        results.append(False)
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


class SchemaAwareFake(ModelClient):
    """Answers according to the requested schema, so thread timing doesn't matter."""
    provider = "ollama"
    REPLIES = {
        "ProjectPlan": {"tasks": [
            {"description": "Research printable planner niches", "department": "research",
             "priority": 1, "required_capabilities": ["market_research"], "estimated_cost": 0,
             "requires_approval": False, "depends_on": []},
            {"description": "Buy a mockup template", "department": "finance", "priority": 2,
             "required_capabilities": ["budget_analysis"], "estimated_cost": 5,
             "requires_approval": False, "depends_on": []}]},
        "TaskResult": {"success": True, "summary": "done", "output": "the deliverable"},
        "ProjectDecision": {"decision": "continue", "reason": "on track", "confidence": 0.8,
                            "next_actions": []},
    }

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def loaded_models(self):
        return [{"name": "qwen3:4b", "size": 3_000_000_000, "size_vram": 1_500_000_000}]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        title = (json_schema or {}).get("title", "")
        return ModelResponse(text=json.dumps(self.REPLIES[title]), model=model,
                             provider="ollama", duration_seconds=0)


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out waiting")


tmp = Path(tempfile.mkdtemp())
cfg = tmp / "config"
shutil.copytree("config", cfg, ignore=shutil.ignore_patterns("history"))
auth = AuthStore(tmp / "auth.json")
auth.set_password(PASSWORD)
fake = SchemaAwareFake()
make_company = lambda: Company(tmp / "company.db", {"ollama": fake}, None, config_dir=cfg)
app = create_app(make_company, auth, ConfigAdmin(cfg, tmp / "history"))

with TestClient(app) as client:
    anon = TestClient(app)

    print("Security")

    def t_auth_required():
        assert anon.get("/api/state").status_code == 401
        assert anon.post("/api/login", json={"password": "wrong"}).status_code == 401
        return "401 without session; wrong password rejected"

    def t_throttle():
        other = TestClient(app)
        codes = [other.post("/api/login", json={"password": "nope"}).status_code for _ in range(6)]
        assert codes[-1] == 429, codes
        return f"after 5 failures -> {codes[-1]} (locked)"

    def t_login_and_headers():
        r = client.post("/api/login", json={"password": PASSWORD})
        assert r.status_code == 200 and "aic_session" in r.cookies
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        csp = client.get("/").headers["content-security-policy"]
        assert "default-src 'self'" in csp
        return "session cookie HttpOnly + SameSite=Strict; strict CSP"

    def t_csrf():
        r = client.post("/api/projects", json={"name": "x", "objective": "y", "budget_eur": 1})
        assert r.status_code == 403
        return "state change without X-Requested-With -> 403"

    def t_frontend():
        html = client.get("/").text
        assert "Mission Control" in html
        for f in ("app.js", "ship.js", "styles.css"):
            assert client.get(f"/static/{f}").status_code == 200
        return "index + app.js + ship.js + styles.css served"

    check("auth required", t_auth_required)
    check("login + security headers", t_login_and_headers)
    check("login throttling", t_throttle)
    check("CSRF header required", t_csrf)
    check("frontend served", t_frontend)

    print("\nProjects, planning, loop")
    state = {}

    def t_create_and_plan():
        r = client.post("/api/projects", headers=H, json={"name": "Planner shop",
                        "objective": "Sell a printable planner", "budget_eur": 20, "priority": 1})
        assert r.status_code == 200, r.text
        pid = state["pid"] = r.json()["id"]
        job = client.post(f"/api/projects/{pid}/plan", headers=H).json()
        done = wait_for(lambda: (j := client.get(f"/api/jobs/{job['id']}").json())["status"]
                        in ("done", "failed") and j)
        assert done["status"] == "done", done
        s = client.get("/api/state").json()
        p = next(p for p in s["projects"] if p["id"] == pid)
        assert p["counts"]["READY"] == 2
        return f"plan job ran in background: {len(done['result']['accepted'])} tasks accepted"

    def t_bad_input():
        r = client.post("/api/projects", headers=H, json={"name": "x", "objective": "y",
                                                           "budget_eur": 999})
        assert r.status_code == 400 and "exceeds unallocated" in r.json()["detail"]
        r2 = client.post("/api/projects", headers=H, json={"name": "", "objective": "y",
                                                            "budget_eur": 1})
        assert r2.status_code == 422
        return "rule violation -> 400 with reason; invalid body -> 422"

    def t_loop_step():
        client.post("/api/runner/step", headers=H)
        tasks = wait_for(lambda: [t for t in client.get("/api/tasks").json()
                                  if t["status"] == "COMPLETED"])
        client.post("/api/runner/step", headers=H)
        waiting = wait_for(lambda: [t for t in client.get("/api/tasks?status=WAITING").json()])
        state["waiting"] = waiting[0]["id"]
        agents = client.get("/api/state").json()["agents"]
        assert any(a["status"] == "COMPLETED" for a in agents)
        return (f"step 1 completed '{tasks[0]['description'][:30]}'; step 2 -> WAITING "
                f"({waiting[0]['wait_reason'][:45]})")

    def t_approve_and_run():
        r = client.post(f"/api/tasks/{state['waiting']}/approve", headers=H)
        assert r.status_code == 200 and r.json()["approved_by"] == "HUMAN"
        client.post("/api/runner/step", headers=H)
        wait_for(lambda: client.get(f"/api/tasks/{state['waiting']}").json()["status"]
                 == "COMPLETED")
        humans = [e for e in client.get("/api/events?actor=HUMAN").json()
                  if e["action"] == "Approved task"]
        assert humans
        return "approved from dashboard (logged as HUMAN) -> executed on next step"

    def t_start_pause():
        s1 = client.post("/api/runner/start", headers=H).json()
        s2 = client.post("/api/runner/pause", headers=H).json()
        assert s1["state"] == "running" and s2["state"] == "paused"
        return "start -> running, pause -> paused"

    def t_project_detail_and_status():
        d = client.get(f"/api/projects/{state['pid']}").json()
        assert d["report"]["progress_percent"] == 100
        r = client.post(f"/api/projects/{state['pid']}/status", headers=H,
                        json={"status": "PAUSED", "reason": "test"})
        assert r.json()["status"] == "PAUSED"
        client.post(f"/api/projects/{state['pid']}/status", headers=H,
                    json={"status": "ACTIVE", "reason": "test"})
        return "detail report 100%; pause/resume from dashboard"

    check("create + plan (background job)", t_create_and_plan)
    check("validation errors", t_bad_input)
    check("work loop step", t_loop_step)
    check("approve from dashboard", t_approve_and_run)
    check("start / pause", t_start_pause)
    check("project detail + status", t_project_detail_and_status)

    print("\nExperiments, memory, research, models")

    def t_experiment():
        spec = {"name": "First EUR 10", "objective": "Earn EUR 10", "budget_eur": 10,
                "max_duration_days": 30, "max_human_hours": 5, "max_pivots": 1,
                "success_criteria": [{"metric": "revenue_eur", "op": ">=", "target": 10}],
                "checkpoint_days": [7]}
        e = client.post("/api/experiments", headers=H, json=spec).json()
        client.post(f"/api/experiments/{e['id']}/metric", headers=H,
                    json={"name": "revenue_eur", "value": 12, "source": "etsy_stats"})
        job = client.post(f"/api/experiments/{e['id']}/evaluate", headers=H).json()
        done = wait_for(lambda: (j := client.get(f"/api/jobs/{job['id']}").json())["status"]
                        == "done" and j)
        assert done["result"]["action"] == "succeeded"
        return "created, metric recorded, evaluated -> succeeded"

    def t_memory():
        g = client.post("/api/memory/guideline", headers=H,
                        json={"content": "Prefer products that need < 2 h/week"}).json()
        client.post("/api/memory/lesson", headers=H,
                    json={"content": "Etsy fees are EUR 0.20 per listing", "evidence": "etsy invoice"})
        client.post(f"/api/memory/{g['id']}/retire", headers=H)
        items = client.get("/api/memory").json()["items"]
        assert any(i["scope"] == "operational" and i["source"] == "HUMAN" for i in items)
        assert next(i for i in items if i["id"] == g["id"])["status"] == "retired"
        return "guideline added + retired; human lesson with evidence"

    def t_research():
        csv = "keyword,metric,value,unit\nwedding planner,median_price,5.2,EUR\n"
        r = client.post("/api/research/import", headers=H,
                        json={"source": "everbee_export", "csv_text": csv}).json()
        cmp = client.get("/api/research?keyword=wedding planner").json()["comparison"]
        bad = client.post("/api/research/import", headers=H,
                          json={"source": "x", "csv_text": "a,b\n1,2\n"})
        assert r["imported"] == 1 and "median_price" in cmp and bad.status_code == 400
        return "CSV import + comparison; malformed CSV -> 400"

    def t_models():
        m = client.get("/api/models").json()
        assert m["loaded"] and m["tools"][0]["name"] == "etsy_listings"
        return f"{len(m['models'])} models, loaded VRAM info, {len(m['tools'])} tool(s)"

    check("experiment via API", t_experiment)
    check("guidelines + lessons", t_memory)
    check("research import", t_research)
    check("models + tools", t_models)

    print("\nConfiguration editing")

    def t_config_validation():
        bad_json = client.post("/api/config/company.json/validate", headers=H,
                               json={"text": "{nope"}).json()["problems"]
        bad_schema = client.post("/api/config/company.json/validate", headers=H,
                                 json={"text": '{"total_budget_eur": -5}'}).json()["problems"]
        depts = json.loads((cfg / "departments.json").read_text(encoding="utf-8"))
        depts["departments"]["research"]["task_type"] = "does_not_exist"
        cross = client.post("/api/config/departments.json/validate", headers=H,
                            json={"text": json.dumps(depts)}).json()["problems"]
        save = client.put("/api/config/company.json", headers=H, json={"text": "{nope"})
        assert bad_json and bad_schema and "cross-file" in cross[0] and save.status_code == 400
        return f"JSON / schema / cross-file problems caught; invalid save refused"

    def t_config_save_restore():
        text = (cfg / "company.json").read_text(encoding="utf-8")
        new = text.replace('"max_task_retries": 2', '"max_task_retries": 3')
        r = client.put("/api/config/company.json", headers=H, json={"text": new}).json()
        assert r["changed"] and "+  \"max_task_retries\": 3" in r["diff"]
        assert '"max_task_retries": 3' in (cfg / "company.json").read_text(encoding="utf-8")
        hist = client.get("/api/config/company.json").json()["history"]
        client.post("/api/config/company.json/restore", headers=H,
                    json={"version": hist[0]["version"]})
        assert (cfg / "company.json").read_text(encoding="utf-8") == text
        ev = [e for e in client.get("/api/events?actor=HUMAN").json()
              if e["action"] == "Changed config"]
        assert len(ev) == 2
        return "saved with backup + diff logged, applied live, restored from history"

    def t_config_paths():
        assert client.get("/api/config/..%2F..%2Fsecrets.json").status_code in (400, 404)
        assert client.get("/api/config/auth.json").status_code == 400
        return "only known config files are reachable"

    check("config validation", t_config_validation)
    check("config save + history + restore", t_config_save_restore)
    check("config path safety", t_config_paths)

print(f"\n{sum(results)}/{len(results)} tests passed")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(results) else 1)
