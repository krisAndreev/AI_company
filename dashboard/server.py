"""
Dashboard web server (FastAPI).

Security:
  - every /api route except /api/login needs a valid session cookie
  - every state-changing request needs the header X-Requested-With: dashboard
    (blocks cross-site form posts; no CORS is enabled)
  - strict Content-Security-Policy: the page loads nothing from other sites
  - every human action is written to the event log as actor HUMAN
  - produced files are served only by asset id; the stored path is re-checked to
    lie inside the workspace (no path from the request is ever used)
  - /healthz is the only unauthenticated route and returns no company data
"""

import json
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from core import engines
from core.backup import backup_database
from core.experiments import ExperimentSpec, ExperimentStatus
from core.chat import ChatError
from core.voice import availability as voice_availability
from core.workspace import AssetRuleError
from core.memory import MemoryRuleError
from core.project_manager import ManagementError
from core.projects import ProjectStatus
from core.tasks import InvalidTransition, TaskStatus, utcnow
from dashboard.auth import AuthStore, LoginThrottle, Sessions
from dashboard.config_admin import ConfigAdmin, ConfigError
from dashboard.services import JobService, RunnerService

STATIC = Path(__file__).parent / "static"
COOKIE = "aic_session"
HUMAN = "HUMAN"


# --- request bodies ---------------------------------------------------------------------

class LoginBody(BaseModel):
    password: str = Field(max_length=200)


class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=2000)
    budget_eur: float = Field(ge=0)
    priority: int = Field(default=3, ge=1, le=5)
    permissions: list[str] = Field(default_factory=list)


class StatusBody(BaseModel):
    status: ProjectStatus
    reason: str = Field(default="changed from dashboard", max_length=500)


class ReviewBody(BaseModel):
    metrics: dict[str, float] = Field(default_factory=dict)


class ReasonBody(BaseModel):
    reason: str = Field(default="cancelled from dashboard", max_length=500)


class ManualBody(BaseModel):
    result: str = Field(min_length=1, max_length=8000)
    cost_eur: float = Field(default=0, ge=0)


class MetricBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    value: float
    source: str = Field(min_length=1, max_length=100)


class HoursBody(BaseModel):
    hours: float = Field(gt=0, le=100)
    note: str = Field(default="", max_length=300)


class GuidelineBody(BaseModel):
    content: str = Field(min_length=3, max_length=400)
    kind: str = Field(default="preference", max_length=30)


class LessonBody(BaseModel):
    content: str = Field(min_length=3, max_length=400)
    evidence: str = Field(min_length=1, max_length=200)


class DecideBody(BaseModel):
    accept: bool


class ConfigBody(BaseModel):
    text: str = Field(max_length=200_000)


class RestoreBody(BaseModel):
    version: str = Field(max_length=60)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread: str = Field(default="main", max_length=40)


class ImportBody(BaseModel):
    source: str = Field(min_length=1, max_length=60)
    csv_text: str = Field(min_length=1, max_length=500_000)
    is_estimate: bool = True


class StudioBody(BaseModel):
    project_id: str = Field(default="", max_length=40)
    params: dict = Field(default_factory=dict)   # validated by the tool's own schema


class AssetDecideBody(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=500)


STUDIO_TOOLS = ("web_research", "product_builder", "image_studio", "video_studio",
                "campaign_builder")


class LoopControl:
    """What the orchestrator may do with the work loop: start, pause, look."""

    def __init__(self, runner: RunnerService):
        self._runner = runner

    def start(self) -> None:
        self._runner.start()

    def pause(self) -> None:
        self._runner.pause()

    def status(self) -> str:
        return self._runner.status()["state"]


# --- app ---------------------------------------------------------------------------------

def create_app(make_company: Callable, auth: AuthStore, config_admin: ConfigAdmin,
               session_hours: float = 12, start_services: bool = True,
               local_preview_no_auth: bool = False) -> FastAPI:
    """local_preview_no_auth: skip login (ONLY allowed when bound to 127.0.0.1; enforced
    in run_dashboard.py). Meant for previewing demo data on this PC."""
    sessions, throttle = Sessions(session_hours), LoginThrottle()
    runner = RunnerService(make_company)
    control = LoopControl(runner)

    def make_controlled():
        """Companies used by the UI and jobs may start/pause the work loop (orchestrator)."""
        c = make_company()
        c.control = control
        return c

    jobs = JobService(make_controlled)
    studio = JobService(make_controlled)       # long media jobs never block chat/plan jobs
    api_lock = threading.RLock()
    holder = {"company": make_controlled()}

    @asynccontextmanager
    async def lifespan(_app):
        if start_services:
            runner.launch()
            jobs.launch()
            studio.launch()
        yield
        runner.shutdown()
        jobs.shutdown()
        studio.shutdown()

    def all_jobs() -> list[dict]:
        return sorted(jobs.recent() + studio.recent(), key=lambda j: j["created"], reverse=True)

    app = FastAPI(title="AI Company Dashboard", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.runner, app.state.jobs, app.state.studio = runner, jobs, studio

    def company():
        return holder["company"]

    # --- middleware -----------------------------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/login":
            if not local_preview_no_auth and not sessions.valid(request.cookies.get(COOKIE)):
                return JSONResponse({"detail": "login required"}, status_code=401)
            if request.method not in ("GET", "HEAD") and \
                    request.headers.get("x-requested-with") != "dashboard":
                return JSONResponse({"detail": "missing X-Requested-With header"},
                                    status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store" if path.startswith("/api/") else "no-cache"
        return response

    def act(fn):
        """Run a state-changing action under the API lock, mapping rule errors to 400."""
        with api_lock:
            try:
                return fn(company())
            except (ManagementError, InvalidTransition, MemoryRuleError, ConfigError,
                    ChatError, AssetRuleError, ValueError, KeyError) as e:
                raise HTTPException(400, str(e).strip("'\""))

    def job(kind: str, label: str, fn):
        return jobs.submit(kind, label, fn)

    @app.get("/healthz")
    def healthz():
        """Unauthenticated liveness check for monitoring: no company data."""
        with api_lock:
            ok = company().db.execute("SELECT 1").fetchone() is not None
        return {"ok": ok, "runner": runner.status()["state"]}

    # --- auth -----------------------------------------------------------------------

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response):
        ip = request.client.host if request.client else "?"
        wait = throttle.wait_seconds(ip)
        if wait:
            raise HTTPException(429, f"too many attempts; try again in {wait}s")
        if not auth.check(body.password):
            throttle.failed(ip)
            raise HTTPException(401, "wrong password")
        throttle.succeeded(ip)
        response.set_cookie(COOKIE, sessions.create(), httponly=True, samesite="strict",
                            max_age=int(session_hours * 3600))
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        sessions.revoke(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # --- overview state (polled by the UI) ------------------------------------------------

    @app.get("/api/state")
    def state():
        with api_lock:
            c = company()
            projects = []
            for p in c.projects.list():
                tasks = c.queue.list(project_id=p.id)
                counts = {s.value: 0 for s in TaskStatus}
                for t in tasks:
                    counts[t.status.value] += 1
                countable = len(tasks) - counts["CANCELLED"]
                spent, open_est = c.queue.project_costs(p.id)
                exp = c.db.execute("SELECT id, status FROM experiments WHERE project_id = ?",
                                   (p.id,)).fetchone()
                projects.append({
                    **p.model_dump(mode="json", include={"id", "name", "status", "priority",
                                                         "budget_eur", "permissions"}),
                    "spent": spent, "estimated_future": open_est, "counts": counts,
                    "progress": round(100 * counts["COMPLETED"] / countable) if countable else 0,
                    "experiment": dict(exp) if exp else None,
                    "tasks": [{"id": t.id, "department": t.department,
                               "description": t.description, "status": t.status.value,
                               "priority": t.priority, "wait_reason": t.wait_reason,
                               "estimated_cost": t.estimated_cost}
                              for t in tasks if p.status in (ProjectStatus.ACTIVE,
                                                             ProjectStatus.PAUSED)][:80],
                })
            since = (utcnow() - timedelta(seconds=120)).isoformat()
            agents = [dict(r) for r in c.db.execute(
                "SELECT a.*, t.description AS task FROM agents a LEFT JOIN tasks t "
                "ON t.id = a.task_id WHERE a.status = 'RUNNING' OR a.finished_at >= ? "
                "ORDER BY a.created_at", (since,))]
            last = c.db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()["m"]
            live = [p for p in projects if p["status"] in ("ACTIVE", "PAUSED")]
            return {
                "server_time": utcnow().isoformat(),
                "runner": runner.status(),
                "autopilot": c.config.autopilot,
                "jobs": [j for j in all_jobs() if j["status"] in ("queued", "running")],
                "projects": projects,
                "agents": agents,
                "departments": {n: d.description for n, d in c.departments.departments.items()},
                "waiting": sum(p["counts"]["WAITING"] for p in projects),
                "budget": {"company_total": c.config.total_budget_eur,
                           "allocated": sum(p["budget_eur"] for p in live),
                           "spent": sum(p["spent"] for p in projects)},
                "last_event_id": last,
            }

    @app.get("/api/events")
    def events(after: int = 0, limit: int = 100, actor: str | None = None,
               project_id: str | None = None):
        with api_lock:
            sql, args = "SELECT * FROM events WHERE id > ?", [after]
            if actor:
                sql += " AND actor = ?"
                args.append(actor)
            if project_id:
                sql += " AND project_id = ?"
                args.append(project_id)
            rows = company().db.execute(sql + " ORDER BY id DESC LIMIT ?",
                                        args + [min(limit, 500)])
            return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    # --- runner + jobs ----------------------------------------------------------------------

    @app.get("/api/runner")
    def runner_status():
        return runner.status()

    @app.post("/api/runner/{command}")
    def runner_command(command: str):
        if command not in ("start", "pause", "step"):
            raise HTTPException(404, "unknown command")
        getattr(runner, command)()
        with api_lock:
            company().events.record(HUMAN, f"Runner {command}")
        return runner.status()

    @app.get("/api/jobs")
    def list_jobs():
        return all_jobs()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        j = jobs.get(job_id) or studio.get(job_id)
        if j is None:
            raise HTTPException(404, "unknown job")
        return j

    # --- projects -----------------------------------------------------------------------------

    @app.post("/api/projects")
    def create_project(body: ProjectBody):
        return act(lambda c: c.orchestrator.create_project(
            body.name, body.objective, body.budget_eur, body.priority, body.permissions)
            .model_dump(mode="json"))

    @app.get("/api/projects/{project_id}")
    def project_detail(project_id: str):
        def fn(c):
            p = c.projects.get(project_id)
            exp = c.db.execute("SELECT id FROM experiments WHERE project_id = ?",
                               (project_id,)).fetchone()
            return {
                "project": p.model_dump(mode="json"),
                "report": c.orchestrator.manager(project_id).report(),
                "tasks": [t.model_dump(mode="json") for t in c.queue.list(project_id=project_id)],
                "expenses": c.ledger.list(project_id),
                "memory": [m.model_dump(mode="json")
                           for m in c.memory.recall("project", project_id=project_id, limit=50)],
                "experiment_id": exp["id"] if exp else None,
                "files_total": len(c.assets.list(project_id=project_id, limit=100_000)),
                "files_draft": len(c.assets.list(project_id=project_id, status="DRAFT",
                                                 limit=100_000)),
            }
        return act(fn)

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str):
        return act(lambda c: c.delete_project(project_id, HUMAN))

    @app.post("/api/projects/{project_id}/status")
    def project_status(project_id: str, body: StatusBody):
        return act(lambda c: c.orchestrator.set_project_status(
            project_id, body.status, body.reason, actor=HUMAN).model_dump(mode="json"))

    @app.post("/api/projects/{project_id}/plan")
    def plan(project_id: str):
        def fn(c):
            r = c.orchestrator.plan_project(project_id)
            return {"accepted": [t.description for t in r.accepted],
                    "rejected": [{"task": d, "reason": why} for d, why in r.rejected],
                    "model": r.model_key}
        with api_lock:
            name = company().projects.get(project_id).name
        return job("plan", f"Plan: {name}", fn)

    @app.post("/api/projects/{project_id}/review")
    def review(project_id: str, body: ReviewBody):
        def fn(c):
            r = c.orchestrator.review_project(project_id, metrics=body.metrics)
            return r.__dict__
        with api_lock:
            name = company().projects.get(project_id).name
        return job("review", f"Review: {name}", fn)

    # --- tasks ------------------------------------------------------------------------------------

    @app.get("/api/tasks")
    def tasks(status: TaskStatus | None = None, project_id: str | None = None):
        with api_lock:
            c = company()
            names = {p.id: p.name for p in c.projects.list()}
            return [{**t.model_dump(mode="json"), "project_name": names.get(t.project_id, "?")}
                    for t in c.queue.list(status=status, project_id=project_id)]

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str):
        def fn(c):
            t = c.queue.get(task_id)
            if t is None:
                raise KeyError(f"unknown task {task_id}")
            agents = [dict(r) for r in c.db.execute(
                "SELECT * FROM agents WHERE task_id = ? ORDER BY created_at", (task_id,))]
            deps = [{"id": d, "description": (c.queue.get(d).description if c.queue.get(d)
                                              else "?"),
                     "status": (c.queue.get(d).status.value if c.queue.get(d) else "?")}
                    for d in t.dependencies]
            return {**t.model_dump(mode="json"), "project_name": c.projects.get(t.project_id).name,
                    "workers": agents, "dependency_info": deps}
        return act(fn)

    @app.post("/api/tasks/{task_id}/approve")
    def approve(task_id: str):
        return act(lambda c: c.runner.approve(task_id, HUMAN).model_dump(mode="json"))

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str, body: ReasonBody):
        def fn(c):
            t = c.queue.cancel(task_id, body.reason)
            c.events.record(HUMAN, "Cancelled task", t.project_id, task_id, reason=body.reason)
            return t.model_dump(mode="json")
        return act(fn)

    @app.post("/api/tasks/{task_id}/retry")
    def retry(task_id: str):
        def fn(c):
            t = c.queue.retry(task_id)
            c.events.record(HUMAN, "Retried task", t.project_id, task_id)
            return t.model_dump(mode="json")
        return act(fn)

    @app.post("/api/tasks/{task_id}/complete")
    def complete_manual(task_id: str, body: ManualBody):
        return act(lambda c: c.runner.complete_manual(task_id, body.result, body.cost_eur, HUMAN)
                   .model_dump(mode="json"))

    # --- experiments -------------------------------------------------------------------------------

    @app.get("/api/experiments")
    def experiments():
        with api_lock:
            c = company()
            return [{**e.model_dump(mode="json"), "metrics": c.experiments.latest_metrics(e.id)}
                    for e in c.experiments.list()]

    @app.post("/api/experiments")
    def create_experiment(spec: ExperimentSpec):
        def fn(c):
            exp = c.experiments.create(spec)
            c.events.record(HUMAN, "Created experiment", exp.project_id, experiment=exp.id)
            return exp.model_dump(mode="json")
        return act(fn)

    @app.post("/api/experiments/{exp_id}/metric")
    def metric(exp_id: str, body: MetricBody):
        return act(lambda c: c.experiments.record_metric(exp_id, body.name, body.value,
                                                         body.source) or {"ok": True})

    @app.post("/api/experiments/{exp_id}/hours")
    def hours(exp_id: str, body: HoursBody):
        return act(lambda c: c.experiments.log_human_hours(exp_id, body.hours, body.note)
                   or {"ok": True})

    @app.post("/api/experiments/{exp_id}/evaluate")
    def evaluate(exp_id: str):
        return job("evaluate", f"Evaluate {exp_id}",
                   lambda c: c.experiments.evaluate(exp_id).model_dump())

    @app.post("/api/experiments/{exp_id}/learn")
    def learn(exp_id: str):
        def fn(c):
            out = c.learning.learn_from_experiment(exp_id)
            return {"lessons": [i.content for i in out.new_lessons],
                    "confirmed": [i.content for i in out.confirmed],
                    "improvements": out.improvement_ids, "model": out.model_key}
        return job("learn", f"Learn from {exp_id}", fn)

    # --- memory + guidelines ------------------------------------------------------------------------

    @app.get("/api/memory")
    def memory():
        with api_lock:
            c = company()
            rows = c.db.execute("SELECT * FROM memory ORDER BY scope, confidence DESC, "
                                "updated_at DESC").fetchall()
            names = {p.id: p.name for p in c.projects.list()}
            items = [c.memory._from_row(r).model_dump(mode="json") for r in rows]
            for i in items:
                i["project_name"] = names.get(i["project_id"])
            return {"items": items, "improvements": c.improvements.list()}

    @app.post("/api/memory/guideline")
    def add_guideline(body: GuidelineBody):
        def fn(c):
            item = c.memory.add("personal", body.kind, body.content, HUMAN)
            c.events.record(HUMAN, "Added guideline", memory=item.id, content=item.content)
            return item.model_dump(mode="json")
        return act(fn)

    @app.post("/api/memory/lesson")
    def add_lesson(body: LessonBody):
        def fn(c):
            item = c.memory.add("operational", "lesson", body.content, HUMAN,
                                evidence=[f"human:{body.evidence}"])
            c.events.record(HUMAN, "Added operational lesson", memory=item.id)
            return item.model_dump(mode="json")
        return act(fn)

    @app.post("/api/memory/{memory_id}/retire")
    def retire(memory_id: str):
        def fn(c):
            item = c.memory.retire(memory_id)
            c.events.record(HUMAN, "Retired memory", memory=memory_id, content=item.content)
            return item.model_dump(mode="json")
        return act(fn)

    @app.post("/api/improvements/{imp_id}/decide")
    def decide(imp_id: str, body: DecideBody):
        def fn(c):
            c.improvements.decide(imp_id, body.accept, HUMAN)
            c.events.record(HUMAN, "Accepted improvement" if body.accept
                            else "Rejected improvement", improvement=imp_id)
            return {"ok": True}
        return act(fn)

    # --- models, stats, research ----------------------------------------------------------------------

    # Network look-ups (Ollama, nvidia-smi) happen OUTSIDE api_lock and are cached briefly,
    # so a slow node can never freeze the rest of the dashboard.
    slow_cache: dict[str, tuple[float, object]] = {}
    slow_lock = threading.Lock()

    def cached(key: str, seconds: float, fn):
        with slow_lock:
            hit = slow_cache.get(key)
            if hit and time.time() - hit[0] < seconds:
                return hit[1]
            value = fn()
            slow_cache[key] = (time.time(), value)
            return value

    @app.get("/api/models")
    def models():
        with api_lock:
            c = company()
            specs = {k: v.model_dump() for k, v in c.router.registry.models.items()}
            stats = c.learning.operational_stats()
            profiles = {k: v.model_dump() for k, v in c.router.registry.task_profiles.items()}
            tools = [{"name": n, "available": t.availability()[0], "reason": t.availability()[1],
                      **t.config.model_dump()} for n, t in c.tools.tools.items()]

        def ollama():
            loaded, error = [], None
            try:
                client = c.router.clients.get("ollama")
                loaded = client.loaded_models() if client else []
            except Exception as e:
                error = str(e)
            return c.router.status(), loaded, error
        status, loaded, error = cached("models", 3, ollama)
        return {"models": [{**s, **specs[s["key"]]} for s in status], "loaded": loaded,
                "ollama_error": error, "stats": stats, "profiles": profiles, "tools": tools}

    @app.get("/api/resources")
    def resources():
        with api_lock:
            c = company()
            counts = c.queue.stats()
            running = [dict(r) for r in c.db.execute(
                "SELECT a.id, a.department, a.project_id, a.created_at, t.description AS task "
                "FROM agents a LEFT JOIN tasks t ON t.id = a.task_id WHERE a.status = 'RUNNING'")]
        nodes = cached("nodes", 3, lambda: c.resources.status() if c.resources else [])
        return {"nodes": nodes, "queue": {k: counts[k] for k in ("READY", "PENDING", "RUNNING",
                                                                 "WAITING")},
                "running": running, "runner": runner.status(),
                "jobs": [j for j in all_jobs() if j["status"] in ("queued", "running")]}

    # --- talk to the orchestrator ----------------------------------------------------------------

    @app.get("/api/chat/threads")
    def chat_threads():
        with api_lock:
            return company().chat.threads()

    @app.get("/api/chat")
    def chat_history(thread: str = "main"):
        return act(lambda c: c.chat.check_thread(thread) and c.chat.history(200, thread))

    @app.post("/api/chat")
    def chat_send(body: ChatBody):
        msg = act(lambda c: c.chat.add_owner_message(body.message, body.thread))
        return {"message": msg, "job": job("chat", "Orchestrator is thinking",
                                           lambda c: c.chat.respond(body.thread))}

    @app.post("/api/chat/{msg_id}/actions/{index}/execute")
    def chat_execute(msg_id: int, index: int):
        entry = act(lambda c: c.chat.get(msg_id)["actions"])
        if not 0 <= index < len(entry) or entry[index]["status"] != "proposed":
            raise HTTPException(400, "action is not waiting for confirmation")
        return job("action", f"Orchestrator action: {entry[index]['action']['type']}",
                   lambda c: c.chat.execute(msg_id, index))

    @app.post("/api/chat/{msg_id}/actions/{index}/dismiss")
    def chat_dismiss(msg_id: int, index: int):
        return act(lambda c: c.chat.dismiss(msg_id, index))

    @app.get("/api/research")
    def research(keyword: str | None = None):
        with api_lock:
            c = company()
            keywords = [r["keyword"] for r in c.db.execute(
                "SELECT keyword, MAX(retrieved_at) AS last FROM market_data GROUP BY keyword "
                "ORDER BY last DESC")]
            return {"keywords": keywords,
                    "comparison": c.research.compare(keyword) if keyword else None,
                    "tool_calls": c.tools.recent(30)}

    @app.post("/api/research/import")
    def research_import(body: ImportBody):
        def fn(c):
            with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                             encoding="utf-8") as f:
                f.write(body.csv_text)
            try:
                n = c.research.import_csv(f.name, body.source, body.is_estimate)
            except (KeyError, ValidationError) as e:
                raise ValueError(f"CSV needs columns keyword,metric,value[,unit,note]: {e}")
            finally:
                Path(f.name).unlink(missing_ok=True)
            c.events.record(HUMAN, "Imported research data", source=body.source, rows=n)
            return {"imported": n}
        return act(fn)

    # --- studio: web research, products, images, videos, campaigns ---------------------------------------

    @app.get("/api/studio")
    def studio_status():
        with api_lock:
            c = company()
            tools = [{"name": n, "available": t.availability()[0],
                      "reason": t.availability()[1], "description": t.config.description,
                      "capability": t.config.capability, "calls_today": c.tools._calls_today(n),
                      "max_calls_per_day": t.config.max_calls_per_day}
                     for n, t in c.tools.tools.items() if n in STUDIO_TOOLS]
            projects = [{"id": p.id, "name": p.name, "status": p.status.value}
                        for p in c.projects.list()]
            brand = c.config.brand_name
        image_tool = c.tools.tools.get("image_studio")
        video_tool = c.tools.tools.get("video_studio")
        web_tool = c.tools.tools.get("web_research")
        probes = cached("studio", 10, lambda: {
            "image_providers": image_tool.engine(c).status() if image_tool else [],
            "voice": dict(zip(("available", "reason"),
                              voice_availability(video_tool.settings.voice))) if video_tool else None,
            "search_provider": web_tool.client().provider() if web_tool else None,
            "engines": engines.status()})
        return {"tools": tools, **probes, "brand_name": brand, "projects": projects,
                "jobs": studio.recent()[:20]}

    @app.post("/api/studio/{tool}")
    def studio_run(tool: str, body: StudioBody):
        if tool not in STUDIO_TOOLS:
            raise HTTPException(404, "unknown studio tool")
        with api_lock:
            c = company()
            if body.project_id and body.project_id not in {p.id for p in c.projects.list()}:
                raise HTTPException(400, "unknown project")
            if not c.tools.available(tool):
                raise HTTPException(400, f"{tool} unavailable: {c.tools.tools[tool].availability()[1]}")
            c.events.record(HUMAN, f"Requested {tool}", body.project_id)
        label = {"web_research": "Web research", "product_builder": "Build product",
                 "image_studio": "Create images", "video_studio": "Create video",
                 "campaign_builder": "Build campaign"}[tool]
        return studio.submit("studio", f"{label}", lambda c: c.run_tool(
            tool, body.params, body.project_id or None, HUMAN))

    @app.get("/api/assets")
    def assets(project_id: str | None = None, kind: str | None = None, status: str | None = None,
               group_id: str | None = None, limit: int = 200):
        with api_lock:
            c = company()
            names = {p.id: p.name for p in c.projects.list()}
            rows = c.assets.list(project_id, kind, status, group_id, min(limit, 1000))
            for a in rows:
                a["project_name"] = names.get(a["project_id"])
                a["has_thumb"] = c.assets.thumb_path(a["id"]) is not None
            return rows

    @app.get("/api/assets/{asset_id}")
    def asset_detail(asset_id: str):
        return act(lambda c: {**c.assets.get(asset_id),
                              "has_thumb": c.assets.thumb_path(asset_id) is not None})

    @app.get("/api/assets/{asset_id}/file")
    def asset_file(asset_id: str, download: bool = False):
        with api_lock:
            c = company()
            try:
                asset, path = c.assets.get(asset_id), c.assets.file_path(asset_id)
            except (KeyError, ValueError) as e:
                raise HTTPException(404, str(e).strip("'\""))
        return FileResponse(path, media_type=asset["mime"], filename=path.name,
                            content_disposition_type="attachment" if download else "inline")

    @app.get("/api/assets/{asset_id}/thumb")
    def asset_thumb(asset_id: str):
        with api_lock:
            path = company().assets.thumb_path(asset_id)
        if path is None:
            raise HTTPException(404, "no thumbnail")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/assets/{asset_id}/text")
    def asset_text(asset_id: str):
        with api_lock:
            c = company()
            try:
                asset, path = c.assets.get(asset_id), c.assets.file_path(asset_id)
            except (KeyError, ValueError) as e:
                raise HTTPException(404, str(e).strip("'\""))
        if not (asset["mime"].startswith("text/") or path.suffix in (".md", ".csv", ".srt")):
            raise HTTPException(400, "not a text file")
        return {"text": path.read_text(encoding="utf-8", errors="replace")[:200_000]}

    @app.post("/api/assets/{asset_id}/decide")
    def asset_decide(asset_id: str, body: AssetDecideBody):
        def fn(c):
            a = c.assets.decide(asset_id, body.approve, HUMAN, body.note)
            c.events.record(HUMAN, "Approved asset" if body.approve else "Rejected asset",
                            a["project_id"], a["task_id"], asset=asset_id, title=a["title"],
                            note=body.note)
            return a
        return act(fn)

    @app.get("/api/web_sources")
    def web_sources(project_id: str | None = None, limit: int = 100):
        with api_lock:
            return company().web_sources.recent(min(limit, 500), project_id)

    @app.post("/api/backup")
    def backup_now():
        def fn(c):
            path = backup_database(c.db, c.config.backup_keep)
            c.events.record(HUMAN, "Database backup", file=str(path))
            return {"file": str(path)}
        return act(fn)

    # --- configuration ----------------------------------------------------------------------------------

    @app.get("/api/config")
    def config_files():
        return config_admin.files()

    @app.get("/api/config/{name}")
    def config_read(name: str):
        return act(lambda c: {"name": name, "text": config_admin.read(name),
                              "history": config_admin.history(name)})

    @app.post("/api/config/{name}/validate")
    def config_validate(name: str, body: ConfigBody):
        return act(lambda c: {"problems": config_admin.validate(name, body.text)})

    @app.get("/api/config/{name}/version/{version}")
    def config_version(name: str, version: str):
        return act(lambda c: {"text": config_admin.read_version(name, version)})

    def _after_config_change(name, result):
        if result["changed"]:
            with api_lock:
                old = holder["company"]
                holder["company"] = make_controlled()
                old.close()
                holder["company"].events.record(HUMAN, "Changed config", file=name,
                                                backup=result["backup"],
                                                diff=result["diff"][:1500])
            runner.request_reload()
            jobs.request_reload()
            studio.request_reload()
            with slow_lock:
                slow_cache.clear()
        return result

    @app.put("/api/config/{name}")
    def config_save(name: str, body: ConfigBody):
        result = act(lambda c: config_admin.save(name, body.text))
        return _after_config_change(name, result)

    @app.post("/api/config/{name}/restore")
    def config_restore(name: str, body: RestoreBody):
        result = act(lambda c: config_admin.restore(name, body.version))
        return _after_config_change(name, result)

    # --- static frontend ---------------------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app
