"""
Phase 18 test: marketing campaigns, the Studio in the dashboard, and production
hardening.

Part A: campaign builder with a scripted model - channel limits enforced by code,
        dated calendar, images/video/emails/blog, zip pack; nothing published
Part B: dashboard Studio API - run tools as background jobs, file gallery, preview,
        download, approve/reject, path-traversal and CSRF protection, health check
Part C: production hardening - database backups with rotation, log rotation, doctor
Part D: live end-to-end through the real work loop: web research -> digital product
        -> campaign, with the real models choosing every tool's parameters

Run:  python test_phase18.py
"""

import csv
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from clients import ModelClient, ModelResponse
from core.backup import backup_database
from core.company import Company
from core.db import Database
from core.events import EventLog
from core.schemas import TaskProposal
from core.tasks import TaskStatus, utcnow
from dashboard.auth import AuthStore
from dashboard.config_admin import ConfigAdmin
from dashboard.server import create_app

results = []
TMP = Path(tempfile.mkdtemp(prefix="phase18_"))


def check(name, fn):
    try:
        detail = fn()
        results.append(True)
        print(f"[pass] {name}" + (f" -> {detail}" if detail else ""))
    except Exception as e:
        results.append(False)
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


class SchemaFake(ModelClient):
    provider = "ollama"

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls, self.prompts = [], []

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        title = (json_schema or {}).get("title", "")
        self.calls.append(title)
        self.prompts.append("\n".join(m.content for m in messages))
        h = self.handlers[title]
        reply = h(self.prompts[-1]) if callable(h) else (h.pop(0) if isinstance(h, list) and len(h) > 1
                                                          else h[0] if isinstance(h, list) else h)
        return ModelResponse(text=json.dumps(reply), model=model, provider="ollama",
                             duration_seconds=0, prompt_tokens=5, completion_tokens=5)


PRODUCT = {
    "ProductOutline": {"pages": [{"title": "Weekly Meals", "purpose": "plan 7 dinners"},
                                 {"title": "Shopping List", "purpose": "list groceries"},
                                 {"title": "Pantry", "purpose": "track staples"}]},
    "PageContent": {"blocks": [{"type": "table", "text": "Dinners", "columns": ["Day", "Meal"],
                                "count": 7}, {"type": "checklist", "items": ["Milk", "Eggs"]}]},
    "ProductListing": {"title": "Weekly Meal Planner Printable - Grocery List, Instant Download",
                       "description": "Plan a week of dinners in minutes with this printable meal "
                                      "planner and grocery list. " * 3,
                       "tags": ["meal planner", "grocery list", "printable", "menu planner",
                                "instant download"], "price_eur": 3.9},
}
BRIEF = {"product_type": "planner", "title": "Easy Weekly Meal Planner", "audience": "busy families",
         "pages": 3, "theme": "bold"}
LONG = "Plan dinners in ten minutes a week with our printable meal planner. " * 5
post = lambda day, hook, caption, visual="product", tags=("mealplanning",), title="": {
    "day": day, "hook": hook, "caption": caption, "hashtags": list(tags), "visual": visual,
    "photo_prompt": "family dinner table" if visual == "photo" else "", "title": title}
CAMPAIGN = {
    "CampaignStrategy": {"angle": "Stop the 5pm what's-for-dinner panic",
                         "pillars": ["time saved", "less food waste"],
                         "hooks": ["Dinner decided in 10 minutes", "No more 5pm panic", "Save EUR 30 a week"],
                         "kpis": ["link clicks", "sales"]},
    "InstagramPlan": {"posts": [post(1, "Dinner, decided", "Meet the planner #tag", "video"),
                                post(3, "No more 5pm panic", "Plan once, relax all week", "quote")]},
    "XPlan": [{"posts": [post(2, "Too long", LONG)]},             # > 280 chars -> retry
              {"posts": [post(2, "Dinner in 10 min", "Plan a week of dinners in 10 minutes.")]}],
    "PinterestPlan": [{"posts": [post(4, "Printable meal planner", "Free up your evenings")]},  # no title
                      {"posts": [post(4, "Printable meal planner", "Free up your evenings",
                                      title="Weekly Meal Planner Printable")]}],
    "EmailSequence": {"emails": [{"day": 1, "subject": "Dinner, decided", "preview": "Your planner",
                                  "body": "Hi! Here is how to plan a week of dinners in ten minutes. " * 3
                                          + "\nUnsubscribe any time."},
                                 {"day": 5, "subject": "Last day of launch pricing", "preview": "",
                                  "body": "A quick reminder that launch pricing ends today. " * 3}]},
    "BlogPost": {"title": "How to Plan a Week of Dinners in 10 Minutes",
                 "meta_description": "A simple weekly meal planning routine for busy families, "
                                     "with a free printable template.",
                 "body": "## Why plan\n" + "Meal planning saves time and money. " * 30},
    "VideoParams": {"title": "Dinner decided", "format": "square", "scenes": [
        {"caption": "5pm panic?", "narration": "", "seconds": 2},
        {"caption": "Plan once", "narration": "", "seconds": 2}], "cta": "", "voiceover": False},
}
CAMPAIGN_BRIEF = {"name": "Meal planner launch", "goal": "first 20 sales in 7 days",
                  "audience": "busy families", "key_message": "Dinner decided in 10 minutes",
                  "offer": "30% off launch week",
                  "channels": ["instagram", "x", "pinterest", "email", "blog"],
                  "duration_days": 7, "posts_per_channel": 2, "videos": 1, "theme": "bold"}


def no_ai(c):
    t = c.tools.tools["image_studio"]
    for prov in ("sdcpp", "a1111", "openai", "pollinations"):
        getattr(t.settings, prov).enabled = False
    c.tools.tools["video_studio"].settings.voice.engine = "none"


# --- Part A ----------------------------------------------------------------------------------------

print("Part A: campaign builder")


def t_campaign():
    fake = SchemaFake({**PRODUCT, **{k: (list(v) if isinstance(v, list) else v)
                                     for k, v in CAMPAIGN.items()}})
    c = Company(":memory:", {"ollama": fake}, None, workspace_dir=TMP / "campaign")
    no_ai(c)
    p = c.orchestrator.create_project("Meal planner", "Sell meal planners", 10)
    c.run_tool("product_builder", BRIEF, p.id)
    out = c.run_tool("campaign_builder", CAMPAIGN_BRIEF, p.id)
    s = out["summary"]
    assert s["posts"] == 4 and s["emails"] == 2 and s["blog"] and s["videos"] == 1, s
    assert fake.calls.count("XPlan") == 2 and fake.calls.count("PinterestPlan") == 2
    assert "Easy Weekly Meal Planner" in next(pr for pr in fake.prompts if "campaign strategy" in pr)
    pack = c.assets.list(project_id=p.id, kind="pack")[0]
    z = zipfile.ZipFile(c.assets.file_path(pack["id"]))
    names = z.namelist()
    assert {"calendar.csv", "campaign.md", "emails/day-01.md", "emails/day-05.md"} <= set(names)
    assert any(n.startswith("blog/") for n in names) and any(n.startswith("media/") and
                                                             n.endswith(".mp4") for n in names)
    rows = list(csv.DictReader(io.StringIO(z.read("calendar.csv").decode("utf-8"))))
    start = (utcnow() + timedelta(days=1)).date()
    assert rows[0]["date"] == start.isoformat() and len(rows) == 6
    x = next(r for r in rows if r["channel"] == "x")
    assert len(x["caption"]) + len(x["hashtags"]) + 1 <= 280
    for r in rows:
        if r["channel"] not in ("email",):
            for m in r["media"].split(";"):
                assert m in names, (m, names[:10])
    assert all(a["status"] == "DRAFT" for a in c.assets.list(project_id=p.id))
    assert not any("ublish" in e["action"] for e in c.events.recent(300))
    return (f"4 posts (X too-long + pin without title rejected and retried), 1 video, 2 emails, "
            f"blog, {len(names)}-file pack; calendar starts {start}; nothing published")


def t_campaign_written():
    fake = SchemaFake(dict(PRODUCT))
    c = Company(":memory:", {"ollama": fake}, None, workspace_dir=TMP / "written")
    no_ai(c)
    p = c.orchestrator.create_project("Meal planner", "Sell meal planners", 10)
    c.run_tool("product_builder", BRIEF, p.id)
    before = len(fake.calls)
    brief = {**CAMPAIGN_BRIEF, "channels": ["pinterest", "email"], "videos": 0,
             "strategy": CAMPAIGN["CampaignStrategy"], "emails": CAMPAIGN["EmailSequence"],
             "written_posts": {"pinterest": [post(2, "Printable meal planner", "Free up evenings",
                                                 title="Weekly Meal Planner Printable")]}}
    out = c.run_tool("campaign_builder", brief, p.id)
    assert len(fake.calls) == before and out["summary"]["posts"] == 1, fake.calls[before:]
    bad = {**brief, "written_posts": {"pinterest": [post(2, "No title", "Free up evenings")]}}
    from core.net import ToolError
    try:
        c.run_tool("campaign_builder", bad, p.id)
        raise AssertionError("pin without a title was accepted")
    except ToolError as e:
        assert "channel rules" in str(e)
    return "finished strategy/posts/emails used without model calls; pin without title refused"


def t_campaign_in_loop():
    handlers = {**PRODUCT, **{k: (list(v) if isinstance(v, list) else v) for k, v in CAMPAIGN.items()},
                "ProjectPlan": {"tasks": [
                    {"description": "Build the meal planner product", "department": "content",
                     "priority": 1, "required_capabilities": ["create_digital_product"],
                     "estimated_cost": 0, "requires_approval": False, "depends_on": []},
                    {"description": "Build the launch campaign", "department": "marketing",
                     "priority": 1, "required_capabilities": ["create_campaign"],
                     "estimated_cost": 0, "requires_approval": False, "depends_on": [1]}]},
                "ProductBrief": BRIEF,
                "CampaignBrief": {**CAMPAIGN_BRIEF, "channels": ["instagram"], "videos": 0},
                "TaskResult": {"success": True, "summary": "done", "output": "see files"}}
    fake = SchemaFake(handlers)
    c = Company(":memory:", {"ollama": fake}, None, workspace_dir=TMP / "loop")
    no_ai(c)
    p = c.orchestrator.create_project("Meal planner", "Sell meal planners", 10)
    c.orchestrator.plan_project(p.id)
    outs = c.runner.run(max_tasks=5)
    assert [o["outcome"] for o in outs] == ["completed", "completed"], outs
    brief_prompt = next(pr for pr in fake.prompts if "campaign_builder" in pr)
    assert "product_pdf" in brief_prompt, "campaign did not see the product task's results"
    kinds = {a["kind"] for a in c.assets.list(project_id=p.id)}
    assert {"product_pdf", "pack", "image"} <= kinds
    return "product task -> campaign task (saw the product's files) -> pack, all through the runner"


check("campaign pack with channel limits", t_campaign)
check("product + campaign through the work loop", t_campaign_in_loop)
check("campaign with finished writing (no model)", t_campaign_written)

# --- Part B -----------------------------------------------------------------------------------------

print("\nPart B: dashboard Studio API")
cfg = TMP / "config"
shutil.copytree("config", cfg, ignore=shutil.ignore_patterns("history"))
auth = AuthStore(TMP / "auth.json")
auth.set_password("test-password-123")
H = {"X-Requested-With": "dashboard"}
API_FAKE = SchemaFake({**PRODUCT})


def make_company():
    c = Company(TMP / "api.db", {"ollama": API_FAKE}, None, config_dir=cfg, workspace_dir=TMP / "ws")
    no_ai(c)
    return c


app = create_app(make_company, auth, ConfigAdmin(cfg, TMP / "history"))
with TestClient(app) as client:
    def wait_job(job_id):
        for _ in range(300):
            j = client.get(f"/api/jobs/{job_id}").json()
            if j["status"] in ("done", "failed"):
                return j
            time.sleep(0.2)
        raise AssertionError("job timeout")

    def t_auth_and_health():
        assert client.get("/healthz").json()["ok"] is True
        assert client.get("/api/assets").status_code == 401
        client.post("/api/login", json={"password": "test-password-123"})
        assert client.post("/api/studio/product_builder", json={"params": {}}).status_code == 403
        return "/healthz open (no data); /api needs login; POST without CSRF header refused"

    def t_studio_job():
        pid = client.post("/api/projects", headers=H, json={"name": "Meal planner",
                                                             "objective": "Sell", "budget_eur": 5}).json()["id"]
        s = client.get("/api/studio").json()
        assert {t["name"] for t in s["tools"]} == {"web_research", "product_builder", "image_studio",
                                                   "video_studio", "campaign_builder"}
        assert client.post("/api/studio/shell", headers=H, json={}).status_code == 404
        assert client.post("/api/studio/product_builder", headers=H,
                           json={"project_id": "proj_nope", "params": BRIEF}).status_code == 400
        job = client.post("/api/studio/product_builder", headers=H,
                          json={"project_id": pid, "params": BRIEF}).json()
        j = wait_job(job["id"])
        assert j["status"] == "done", j
        bad = wait_job(client.post("/api/studio/image_studio", headers=H,
                                   json={"project_id": pid, "params": {"format": "huge"}}).json()["id"])
        assert bad["status"] == "failed" and "invalid params" in bad["error"]
        return f"product built as a background job ({len(j['result']['assets'])} files); bad params -> failed job"

    def t_gallery_and_files():
        assets = client.get("/api/assets?status=DRAFT").json()
        pdf = next(a for a in assets if a["kind"] == "product_pdf")
        r = client.get(f"/api/assets/{pdf['id']}/file")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert r.content[:5] == b"%PDF-" and "inline" in r.headers["content-disposition"]
        d = client.get(f"/api/assets/{pdf['id']}/file?download=true")
        assert "attachment" in d.headers["content-disposition"]
        assert "default-src 'self'" in r.headers["content-security-policy"]
        assert client.get(f"/api/assets/{pdf['id']}/thumb").headers["content-type"] == "image/jpeg"
        listing = next(a for a in assets if a["kind"] == "listing")
        assert "Suggested price" in client.get(f"/api/assets/{listing['id']}/text").json()["text"]
        assert client.get(f"/api/assets/{pdf['id']}/text").status_code == 400
        return f"{len(assets)} drafts; PDF inline + download, thumbnail, listing text preview"

    def t_decide_and_traversal():
        pdf = next(a for a in client.get("/api/assets").json() if a["kind"] == "product_pdf")
        a = client.post(f"/api/assets/{pdf['id']}/decide", headers=H, json={"approve": True}).json()
        assert a["status"] == "APPROVED" and a["decided_by"] == "HUMAN"
        again = client.post(f"/api/assets/{pdf['id']}/decide", headers=H, json={"approve": True})
        assert again.status_code == 400
        db = Database(TMP / "api.db")                  # an attacker-crafted row
        db.execute("INSERT INTO assets (id, ts, kind, title, path, mime, bytes, sha256, source, meta, "
                   "status) VALUES ('asset_evil', '2026', 'copy', 'x', '../config/tools.json', "
                   "'text/plain', 1, 'x', 'x', '{}', 'DRAFT')")
        db.close()
        r1 = client.get("/api/assets/asset_evil/file")
        r2 = client.get("/api/assets/asset_evil/text")
        assert r1.status_code == 404 and r2.status_code == 404, (r1.status_code, r2.status_code)
        events = [e["action"] for e in client.get("/api/events?actor=HUMAN").json()]
        assert "Approved asset" in events
        return "approve logged as HUMAN; double-approve refused; '../' path refused (404)"

    def t_backup_endpoint():
        r = client.post("/api/backup", headers=H).json()
        path = Path(r["file"])
        con = __import__("sqlite3").connect(str(path))
        n = con.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        con.close()
        return f"online backup {path.name} holds {n} asset rows"

    check("auth, CSRF header, health check", t_auth_and_health)
    check("studio jobs (valid + invalid)", t_studio_job)
    check("file gallery, preview, download", t_gallery_and_files)
    check("approve/reject + path traversal refused", t_decide_and_traversal)
    check("backup endpoint", t_backup_endpoint)

# --- Part C ---------------------------------------------------------------------------------------------

print("\nPart C: production hardening")


def t_backup_rotation():
    db = Database(TMP / "rot" / "company.db")
    db.execute("CREATE TABLE t (x)")
    made = []
    for _ in range(4):
        made.append(backup_database(db, keep=2))
        time.sleep(1.05)
    left = sorted((TMP / "rot" / "backups").glob("company-*.db"))
    db.close()
    assert len(left) == 2 and left[-1] == made[-1]
    return "4 backups made, newest 2 kept"


def t_log_rotation():
    EventLog.MAX_LOG_BYTES = 2000
    log = TMP / "logs" / "company.log"
    events = EventLog(Database(":memory:"), log)
    for i in range(200):
        events.record("TEST", f"line {i}", detail="x" * 40)
    EventLog.MAX_LOG_BYTES = 5_000_000
    files = sorted(p.name for p in log.parent.iterdir())
    assert "company.log.1" in files and len(files) <= EventLog.KEEP_LOGS + 1
    return f"log rotated: {files}"


def t_doctor():
    proc = subprocess.run([sys.executable, "doctor.py"], capture_output=True, text=True, timeout=180)
    last = proc.stdout.strip().splitlines()[-1]
    assert proc.returncode == 0, proc.stdout[-800:]
    return last


check("database backup rotation", t_backup_rotation)
check("log file rotation", t_log_rotation)
check("doctor.py passes", t_doctor)

# --- Part D -----------------------------------------------------------------------------------------------

print("\nPart D: live end-to-end (real models choose every tool's parameters)")


def t_live_e2e():
    c = Company(TMP / "live.db", None, None, workspace_dir=TMP / "live_ws")
    c.tools.tools["campaign_builder"].settings.max_ai_photos = 1
    p = c.orchestrator.create_project(
        "Meal planner shop", "Sell a printable weekly meal planner for busy families on Etsy", 10)
    tasks = [
        ("research", "Research online what printable meal planners sell best and what buyers "
                     "want in them", ["web_research"], []),
        ("content", "Build a printable weekly meal planner product (about 4 pages) for busy "
                    "families, based on the research", ["create_digital_product"], [0]),
        ("marketing", "Build a 7-day launch campaign for the meal planner on Instagram and "
                      "email, with at most 1 video", ["create_campaign"], [1]),
    ]
    ids = []
    for dept, desc, caps, deps in tasks:
        t = c.queue.add(TaskProposal(description=desc, department=dept, priority=1,
                                     required_capabilities=caps, estimated_cost=0,
                                     requires_approval=False), p.id, [ids[d] for d in deps])
        ids.append(t.id)
    start = time.time()
    for out in c.runner.run(max_tasks=8):
        print(f"       {out['outcome']:9} {out.get('summary') or out.get('reason', '')}"[:160])
    for tid in ids:
        t = c.queue.get(tid)
        print(f"       [{t.status.value}] {t.description[:50]} -> {(t.result or t.error or '')[:110]}")
    kinds = {}
    for a in c.assets.list(project_id=p.id, limit=1000):
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    print(f"       files: {kinds}")
    print(f"       workspace: {c.workspace.root}")
    assert all(c.queue.get(t).status == TaskStatus.COMPLETED for t in ids)
    assert {"report", "product_pdf", "pack"} <= set(kinds)
    return f"3 tasks completed in {time.time() - start:.0f}s"


try:
    live_ok = Company(":memory:", None, None).resources.is_available()
except Exception:
    live_ok = False
if "--offline" in sys.argv:
    print("(live part skipped: --offline)")
elif live_ok:
    check("live: research -> product -> campaign", t_live_e2e)
else:
    results.append(False)
    print("[FAIL] Ollama not reachable")

print(f"\n{sum(results)}/{len(results)} tests passed")
if all(results):
    shutil.rmtree(TMP, ignore_errors=True)
else:
    print(f"(files kept for inspection in {TMP})")
sys.exit(0 if all(results) else 1)
