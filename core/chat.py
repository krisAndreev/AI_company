"""
Talk to the Master Orchestrator - and let it run the company.

Chats ("threads"):
  main         the general Master Orchestrator: sees every project in brief, creates
               projects, answers "how is everything going", starts/pauses work
  <project id> one chat per project: sees THAT project in detail (every task with its
               result, waiting steps, files, the latest research report), so the owner
               can discuss progress without overloading the small model's context.
               Project updates from the autopilot are posted here.

The orchestrator (a model) answers from a code-built snapshot and decides on actions
from a fixed allowlist. Code validates every action against the current state, then:
  - in autopilot mode (config: autopilot = true) SAFE actions run at once: create /
    plan / review / pause / resume projects, add tasks, start or pause the work loop,
    mark a human step done that the owner reported, record a result the owner gave
  - actions that spend money, publish, end a project or write the owner's personal
    guidelines are only PROPOSED: the owner confirms them with a button
  - with autopilot off, every action waits for the owner (the original behaviour)
Code also refuses near-duplicate projects and tasks and fills in the project of a
project chat. Every action goes through the normal, rule-checked code paths.
"""

import json
import re

from core.projects import ProjectStatus
from core.research import RESEARCH_METHOD
from core.router import RouteRequest
from core.schemas import OrchestratorReply, ProposedAction, TaskProposal
from core.structured import generate_structured
from core.tasks import TaskStatus, utcnow

ACTOR = "MASTER_ORCHESTRATOR"
MAIN = "main"
HISTORY_FOR_MODEL = 8
AUTO = {"create_project", "plan_project", "review_project", "pause_project", "resume_project",
        "add_task", "start_work", "pause_work", "complete_task", "record_metric"}
ALWAYS_CONFIRM = {"finish_project", "approve_task", "add_guideline"}
STARTS_WORK = {"create_project", "plan_project", "add_task", "resume_project", "complete_task",
               "start_work"}
ACTION_TEXT = {"create_project": "create the project", "plan_project": "plan the project",
               "review_project": "review the project", "pause_project": "pause the project",
               "resume_project": "resume the project", "finish_project": "finish the project",
               "add_task": "add the task", "start_work": "start the work loop",
               "pause_work": "pause the work loop", "complete_task": "mark the task done",
               "approve_task": "approve the task", "record_metric": "record the result",
               "add_guideline": "save the guideline"}
_OPEN = (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.WAITING)
_LIVE = (ProjectStatus.ACTIVE, ProjectStatus.PAUSED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    role     TEXT NOT NULL,        -- owner, orchestrator
    content  TEXT NOT NULL,
    actions  TEXT NOT NULL,        -- JSON list of {action, status, problem, result}
    model    TEXT
);
"""

RULES = (
    "How to act:\n"
    "- Questions about progress, results or files: ANSWER them from the snapshot (task "
    "results, research, files). Do NOT create a project or task to answer a question.\n"
    "- When the owner asks for something NEW to be made or done: create_project (name, "
    "objective with the owner's goal and constraints, budget_eur, priority) - it is planned "
    "and started automatically - or add_task to an existing project (department + one of "
    "its capabilities + a concrete description). Never create a project that already exists.\n"
    "- Capabilities marked (tool) are done for real by code: web research, building product "
    "PDFs, images, videos, campaign packs.\n"
    "- Missing information you really need (budget, niche, audience, style)? Ask the owner "
    "ONE clear question instead of guessing.\n"
    "- When the owner reports that they did a waiting task: complete_task with its task_id "
    "and what they did. Numbers they report (sales, views, revenue): record_metric.\n"
    "- start_work / pause_work run or stop the work loop. When autopilot is true your safe "
    "actions run immediately: never ask the owner to confirm them.\n"
    "- Check company_budget.unallocated_eur and limits before creating a project; if there "
    "is no room, say so and ask (smaller budget, or pause / finish another project).\n"
    "- RESEARCH (always, owner rule): when the owner asks for research, a niche, a new "
    "product idea or market analysis, the research task / project objective must say to use "
    "this method: " + RESEARCH_METHOD + " Report the 3 pockets and the winner from the "
    "research results.\n"
    "- Never invent ids, numbers or results. You cannot publish or spend money yourself.\n"
    "- `reply` is what the owner reads: 1-4 short sentences, plain language. Answer the "
    "owner's LAST message; do not repeat your earlier replies.")
SYSTEM_MAIN = ("You are the Master Orchestrator: you RUN a small AI company for its owner and "
               "see all its projects. The owner gives goals and answers your questions.\n" + RULES)
SYSTEM_PROJECT = ("You are the Master Orchestrator, talking with the owner about ONE project "
                  "(details in the snapshot). Report progress and results precisely, take "
                  "the owner's feedback into account and act on this project.\n" + RULES)


class ChatError(ValueError):
    pass


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _similar(a: set[str], b: set[str], threshold: float = 0.75) -> bool:
    """Near-duplicate texts (small models like to repeat actions)."""
    return bool(a and b) and len(a & b) / len(a | b) >= threshold


class OrchestratorChat:
    def __init__(self, company):
        self.c = company
        self.c.db.executescript(_SCHEMA)
        cols = {r["name"] for r in self.c.db.execute("PRAGMA table_info(chat_messages)")}
        if "thread" not in cols:   # older databases: everything so far was the main chat
            self.c.db.execute("ALTER TABLE chat_messages ADD COLUMN thread TEXT NOT NULL "
                              "DEFAULT 'main'")
        self.c.db.executescript("CREATE INDEX IF NOT EXISTS idx_chat_thread "
                                "ON chat_messages(thread, id)")

    # --- threads + messages ----------------------------------------------------------

    def check_thread(self, thread: str) -> str:
        if thread == MAIN:
            return thread
        try:
            self.c.projects.get(thread)
        except KeyError:
            raise ChatError(f"unknown chat {thread!r}")
        return thread

    def threads(self) -> list[dict]:
        """The main chat + one per project (live projects first), with the last message."""
        last = {r["thread"]: dict(r) for r in self.c.db.execute(
            "SELECT thread, MAX(id) AS last_id, MAX(ts) AS last_ts, COUNT(*) AS n "
            "FROM chat_messages GROUP BY thread")}
        out = [{"id": MAIN, "name": "Master Orchestrator", "status": None,
                **{k: last.get(MAIN, {}).get(k) for k in ("last_id", "last_ts", "n")}}]
        projects = sorted(self.c.projects.list(), key=lambda p: (p.status not in _LIVE,
                                                                 -p.created_at.timestamp()))
        for p in projects:
            info = last.get(p.id, {})
            if p.status not in _LIVE and not info:
                continue
            out.append({"id": p.id, "name": p.name, "status": p.status.value,
                        "last_id": info.get("last_id"), "last_ts": info.get("last_ts"),
                        "n": info.get("n", 0)})
        return out

    def history(self, limit: int = 100, thread: str = MAIN) -> list[dict]:
        rows = self.c.db.execute("SELECT * FROM chat_messages WHERE thread = ? "
                                 "ORDER BY id DESC LIMIT ?", (thread, limit))
        return [self._row(r) for r in reversed(rows.fetchall())]

    def add_owner_message(self, text: str, thread: str = MAIN) -> dict:
        text = text.strip()
        if not text:
            raise ChatError("empty message")
        self.check_thread(thread)
        msg_id = self._insert("owner", text, [], None, thread)
        self.c.events.record("HUMAN", "Message to orchestrator",
                             None if thread == MAIN else thread, text=text[:300])
        return self.get(msg_id)

    def post_update(self, text: str, thread: str = MAIN, source: str = "autopilot") -> dict:
        """A message the orchestrator's code posts on its own (no model call)."""
        msg_id = self._insert("orchestrator", text.strip(), [], source, thread)
        return self.get(msg_id)

    def respond(self, thread: str = MAIN) -> dict:
        """Generate the orchestrator's answer in a chat and carry out its safe actions."""
        self.check_thread(thread)
        history = self.history(HISTORY_FOR_MODEL, thread)
        if not history or history[-1]["role"] != "owner":
            raise ChatError("nothing to answer")
        transcript = "\n".join(self._line(m, last=i == len(history) - 1)
                               for i, m in enumerate(history))
        auto = self.c.config.autopilot
        route = self.c.router.select(RouteRequest(task_type="chat"))
        snapshot = self.snapshot() if thread == MAIN else self.project_snapshot(thread)
        reply = generate_structured(
            route.client, route.model,
            f"COMPANY SNAPSHOT (facts computed by code - the only facts you may rely on):\n"
            f"{snapshot}\n\nCONVERSATION (oldest first):\n{transcript}\n\n"
            + ("Take the actions needed (max 4): safe ones run immediately; money, publishing, "
               "finishing projects and guidelines wait for the owner's confirmation. "
               if auto else
               "Propose actions (max 4) - the owner confirms each with a button. ")
            + "Only use project and task ids that appear in the snapshot.",
            OrchestratorReply, system=SYSTEM_MAIN if thread == MAIN else SYSTEM_PROJECT,
            think=route.spec.think,
        ).value
        entries = []
        for a in reply.actions:
            a = self._resolve_project(a, thread)
            problem = self._problem(a)
            entries.append({"action": a.model_dump(), "status": "invalid" if problem else "proposed",
                            "problem": problem, "result": None,
                            "needs_owner": bool(not problem and self.needs_owner(a))})
        msg_id = self._insert("orchestrator", reply.reply, entries, route.model_key, thread)
        self.c.events.record(ACTOR, "Replied to owner", None if thread == MAIN else thread,
                             reply=reply.reply[:300], model=route.model_key,
                             actions=[e["action"]["type"] for e in entries],
                             thinking=reply.thinking[:600])
        started_work, problems = False, []
        for i, e in enumerate(entries):
            if e["status"] == "invalid":
                problems.append((e["action"], e["problem"]))
            elif e["status"] == "proposed" and not e["needs_owner"]:
                try:
                    self.execute(msg_id, i, actor=ACTOR)
                    started_work |= e["action"]["type"] in STARTS_WORK
                except Exception as ex:   # also stored on the action card
                    problems.append((e["action"], str(ex)))
        if started_work and auto:
            self._start_loop()
        if problems and auto:
            self.post_update(self._explain(problems), thread)
        return self.get(msg_id)

    def needs_owner(self, a: ProposedAction) -> bool:
        """Code decides which decisions the owner must confirm."""
        if not self.c.config.autopilot or a.type in ALWAYS_CONFIRM:
            return True
        if a.type == "create_project" and a.budget_eur > self.c.config.autopilot_max_project_budget_eur:
            return True
        return a.type not in AUTO

    # --- carrying out actions ------------------------------------------------------------

    def execute(self, msg_id: int, index: int, actor: str = "HUMAN") -> dict:
        msg, entry = self._entry(msg_id, index)
        if entry["status"] != "proposed":
            raise ChatError(f"action is {entry['status']}, not proposed")
        action = ProposedAction.model_validate(entry["action"])
        if actor != "HUMAN" and self.needs_owner(action):
            raise ChatError("this action needs the owner's confirmation")
        problem = self._problem(action)          # re-check: the company may have changed
        if problem:
            entry.update(status="invalid", problem=problem)
            self._save_actions(msg)
            raise ChatError(problem)
        entry["status"] = "executing"
        self._save_actions(msg)
        try:
            result = self._run(action, actor)
            entry.update(status="executed", result=result, by=actor)
        except Exception as e:
            entry.update(status="failed", problem=str(e))
            raise
        finally:
            self._save_actions(msg)
            self.c.events.record(actor, "Confirmed orchestrator action" if actor == "HUMAN"
                                 else "Carried out action", proposal=action.type,
                                 status=entry["status"], project_id_arg=action.project_id,
                                 result=str(entry.get("result") or entry.get("problem"))[:300])
        if actor == "HUMAN" and action.type in STARTS_WORK and self.c.config.autopilot:
            self._start_loop()
        return entry

    def dismiss(self, msg_id: int, index: int) -> dict:
        msg, entry = self._entry(msg_id, index)
        if entry["status"] != "proposed":
            raise ChatError(f"action is {entry['status']}, not proposed")
        entry["status"] = "dismissed"
        self._save_actions(msg)
        self.c.events.record("HUMAN", "Dismissed orchestrator action",
                             proposal=entry["action"]["type"])
        return entry

    # --- snapshots ---------------------------------------------------------------------------

    def _common(self) -> dict:
        c = self.c
        live = [p for p in c.projects.list() if p.status in _LIVE]
        control = getattr(c, "control", None)
        return {
            "date": utcnow().date().isoformat(),
            "work_loop": control.status() if control else "unknown",
            "autopilot": c.config.autopilot,
            "company_budget": {"total_eur": c.config.total_budget_eur,
                               "allocated_eur": sum(p.budget_eur for p in live),
                               "unallocated_eur": round(c.config.total_budget_eur
                                                        - sum(p.budget_eur for p in live), 2)},
            "limits": {"active_projects": f"{sum(p.status == ProjectStatus.ACTIVE for p in live)}"
                                          f" of max {c.config.max_active_projects}",
                       "max_budget_without_owner_eur": c.config.autopilot_max_project_budget_eur},
            "departments": {   # for add_task: department -> capabilities
                name: [cap + (" (tool)" if s.tool and c.tools.available(s.tool)
                              else "" if s.automated else " (manual)")
                       for cap, s in d.capabilities.items()]
                for name, d in c.departments.departments.items()},
            "owner_guidelines": [m.content for m in c.memory.recall("personal", limit=6)],
        }

    def snapshot(self) -> str:
        """The main chat: every project in brief."""
        c = self.c
        all_projects = c.projects.list()
        live = [p for p in all_projects if p.status in _LIVE]
        shown = live + [p for p in all_projects if p not in live][-3:]
        projects = []
        for p in shown:
            tasks = c.queue.list(project_id=p.id)
            counts = {}
            for t in tasks:
                counts[t.status.value] = counts.get(t.status.value, 0) + 1
            spent, _ = c.queue.project_costs(p.id)
            done = [t for t in tasks if t.status == TaskStatus.COMPLETED]
            projects.append({
                "id": p.id, "name": p.name, "status": p.status.value, "priority": p.priority,
                "objective": p.objective[:200], "budget_eur": p.budget_eur,
                "spent_eur": round(spent, 2), "tasks": counts,
                "stage": c.autopilot.stages(p.id),
                "waiting_for_owner": [{"task_id": t.id, "task": t.description[:80]}
                                      for t in tasks if t.status == TaskStatus.WAITING][:4],
                "working_on": [t.description[:70] for t in tasks
                               if t.status in (TaskStatus.RUNNING, TaskStatus.READY)][:3],
                "latest_result": (f"{done[-1].description[:60]}: {_result_text(done[-1])[:220]}"
                                  if done else None),
                "files_to_review": len(c.assets.list(project_id=p.id, status="DRAFT", limit=1000)),
            })
        return json.dumps({**self._common(), "projects": projects,
                           "note": "Each project also has its own chat with full details."},
                          default=str)

    def project_snapshot(self, project_id: str) -> str:
        """A project chat: this project in detail."""
        c = self.c
        p = c.projects.get(project_id)
        tasks = c.queue.list(project_id=p.id)
        spent, planned = c.queue.project_costs(p.id)
        order = {TaskStatus.WAITING: 0, TaskStatus.RUNNING: 1, TaskStatus.READY: 2,
                 TaskStatus.PENDING: 3, TaskStatus.FAILED: 4, TaskStatus.COMPLETED: 5,
                 TaskStatus.CANCELLED: 6}
        rows, budget = [], 3600    # characters of task results the small model can take
        for t in sorted(tasks, key=lambda t: (order[t.status], -(t.completed_at or t.created_at).timestamp())):
            row = {"task_id": t.id, "status": t.status.value, "department": t.department,
                   "task": t.description[:110]}
            if t.status == TaskStatus.COMPLETED and budget > 0:
                row["result"] = _result_text(t)[:min(600, budget)]
                budget -= len(row["result"])
            elif t.status == TaskStatus.FAILED:
                row["error"] = (t.error or "")[:160]
            elif t.status == TaskStatus.WAITING:
                row["waiting_because"] = (t.wait_reason or "")[:120]
            rows.append(row)
        files = c.assets.list(project_id=p.id, limit=500)
        kinds = {}
        for a in files:
            kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
        report = next((a for a in files if a["kind"] == "report"), None)
        research = None
        if report:
            try:
                text = c.assets.file_path(report["id"]).read_text(encoding="utf-8")
                research = text.split("## Sources")[0][:1500]
            except (KeyError, OSError, ValueError):
                research = None
        exp = c.db.execute("SELECT id, status FROM experiments WHERE project_id = ?",
                           (p.id,)).fetchone()
        return json.dumps({
            **self._common(),
            "project": {"id": p.id, "name": p.name, "status": p.status.value,
                        "objective": p.objective[:600], "priority": p.priority,
                        "budget_eur": p.budget_eur, "spent_eur": round(spent, 2),
                        "stage": f"{c.autopilot.stages(p.id)} of max "
                                 f"{c.config.max_stages_per_project}",
                        "experiment": dict(exp) if exp else None,
                        "metrics": c.experiments.latest_metrics(exp["id"]) if exp else {}},
            "tasks": rows[:16],
            "files": {"count_by_kind": kinds,
                      "waiting_for_owner_review": sum(a["status"] == "DRAFT" for a in files),
                      "latest": [f"{a['title'][:70]} ({a['kind']}, {a['status']})"
                                 for a in files if a["kind"] not in ("preview", "captions")][:8]},
            "latest_research_report": research,
        }, default=str)

    # --- internals -------------------------------------------------------------------------

    @staticmethod
    def _line(m: dict, last: bool) -> str:
        if m["role"] == "owner":
            return f"{'OWNER (answer this)' if last else 'OWNER'}: {m['content'][:700]}"
        who = "UPDATE (code)" if m["model"] == "autopilot" else "YOU (earlier)"
        done = [f"{e['action']['type']}={e['status']}" for e in m["actions"]]
        return f"{who}: {m['content'][:220]}" + (f" [actions: {', '.join(done)}]" if done else "")

    _NEEDS_PROJECT = {"plan_project", "review_project", "pause_project", "resume_project",
                      "finish_project", "add_task", "record_metric"}

    def _resolve_project(self, a: ProposedAction, thread: str = MAIN) -> ProposedAction:
        """Small models sometimes give a project's NAME, or nothing, instead of its id, or
        copy a label like 'create_graphics (tool)'. Code fixes that only when unambiguous;
        in a project chat the project is that chat's project."""
        if a.capability or a.department:
            a = a.model_copy(update={"capability": a.capability.split("(")[0].strip().lower(),
                                     "department": a.department.split("(")[0].strip().lower()})
        if a.type not in self._NEEDS_PROJECT:
            return a
        projects = self.c.projects.list()
        if any(p.id == a.project_id for p in projects):
            return a
        if thread != MAIN:
            return a.model_copy(update={"project_id": thread})
        wanted = a.project_id.strip().lower()
        by_name = [p for p in projects if wanted and p.name.lower() == wanted]
        active = [p for p in projects if p.status == ProjectStatus.ACTIVE]
        match = by_name[0] if len(by_name) == 1 else (active[0] if not wanted and len(active) == 1
                                                       else None)
        return a.model_copy(update={"project_id": match.id}) if match else a

    def _problem(self, a: ProposedAction) -> str | None:
        """Why this action cannot run now (None = OK). Code, not the model, decides."""
        c = self.c
        if a.type == "create_project":
            if not a.name.strip() or not a.objective.strip():
                return "create_project needs a name and an objective"
            name, goal = _words(a.name), _words(a.objective)
            for p in c.projects.list(list(_LIVE)):
                if _similar(name, _words(p.name), 0.6) or _similar(goal, _words(p.objective), 0.6):
                    return (f"a similar project already exists: {p.name} ({p.id}) - add tasks "
                            f"to it or use its chat")
            return None
        if a.type == "add_guideline":
            return None if len(a.content.strip()) >= 3 else "add_guideline needs content"
        if a.type in ("start_work", "pause_work"):
            return None if getattr(c, "control", None) else "work loop control not available here"
        if a.type in ("complete_task", "approve_task"):
            task = c.queue.get(a.task_id)
            if task is None:
                return f"unknown task id {a.task_id!r}"
            if task.status != TaskStatus.WAITING:
                return f"task is {task.status.value}, not waiting for the owner"
            manual = (task.wait_reason or "").startswith("manual")
            if a.type == "complete_task" and not manual:
                return "this task waits for an approval, not for work: use approve_task"
            if a.type == "complete_task" and len(a.result.strip()) < 3:
                return "complete_task needs the result the owner reported"
            if a.type == "approve_task" and manual:
                return "this task needs the owner to do it: use complete_task when done"
            return None
        try:
            p = c.projects.get(a.project_id)
        except KeyError:
            return f"unknown project id {a.project_id!r}"
        if a.type == "record_metric":
            exp = c.db.execute("SELECT id FROM experiments WHERE project_id = ?", (p.id,)).fetchone()
            if exp is None:
                return "this project has no experiment to record metrics on"
            return None if a.metric.strip() else "record_metric needs a metric name"
        if a.type == "add_task":
            if p.status != ProjectStatus.ACTIVE:
                return f"project is {p.status.value}; add_task needs ACTIVE"
            dept = c.departments.get(a.department.strip().lower())
            if dept is None:
                return f"unknown department {a.department!r}"
            if a.capability and a.capability not in dept.capabilities:
                return f"{a.department} has no capability {a.capability!r}"
            if len(a.description.strip()) < 5:
                return "add_task needs a description"
            words = _words(a.description)
            for t in c.queue.list(project_id=p.id):
                if t.status in _OPEN and _similar(words, _words(t.description)):
                    return f"the same task is already queued ({t.id})"
            return None
        need = {"plan_project": [ProjectStatus.ACTIVE], "review_project": [ProjectStatus.ACTIVE],
                "pause_project": [ProjectStatus.ACTIVE], "resume_project": [ProjectStatus.PAUSED],
                "finish_project": [ProjectStatus.ACTIVE, ProjectStatus.PAUSED]}
        if p.status not in need[a.type]:
            return f"project is {p.status.value}; {a.type} needs {need[a.type][0].value}"
        return None

    def _run(self, a: ProposedAction, actor: str):
        c, o = self.c, self.c.orchestrator
        if a.type == "create_project":
            p = o.create_project(a.name.strip(), a.objective.strip(), a.budget_eur, a.priority)
            self.post_update(f"New project **{p.name}** - budget EUR {p.budget_eur:g}. "
                             f"Goal: {p.objective[:300]}\nAsk me here about its progress.", p.id)
            if not c.config.autopilot:
                return f"created {p.name} ({p.id})"
            try:
                r = o.plan_project(p.id)
                self.post_update("▶ Stage 1 planned: " + "; ".join(
                    t.description[:70] for t in r.accepted[:6]), p.id)
                return f"created {p.name} ({p.id}) and planned {len(r.accepted)} tasks"
            except Exception as e:   # the autopilot plans it on its next round
                return f"created {p.name} ({p.id}); planning will be retried ({str(e)[:80]})"
        if a.type == "plan_project":
            r = o.plan_project(a.project_id)
            return f"{len(r.accepted)} tasks accepted, {len(r.rejected)} rejected"
        if a.type == "review_project":
            r = o.review_project(a.project_id)
            return f"{r.decision} (confidence {r.confidence:.2f}, applied={r.applied})"
        if a.type == "pause_project":
            o.set_project_status(a.project_id, ProjectStatus.PAUSED, a.reason, actor)
            return "paused"
        if a.type == "resume_project":
            o.set_project_status(a.project_id, ProjectStatus.ACTIVE, a.reason, actor)
            return "resumed"
        if a.type == "finish_project":
            o.set_project_status(a.project_id, ProjectStatus.FINISHED, a.reason, actor)
            return "finished"
        if a.type == "add_task":
            dept = a.department.strip().lower()
            task = c.queue.add(TaskProposal(
                description=a.description.strip(), department=dept, priority=a.priority,
                required_capabilities=[a.capability] if a.capability else [],
                estimated_cost=0, requires_approval=False), a.project_id)
            c.events.record(actor, f"Created {dept} task", a.project_id, task.id,
                            description=task.description, capability=a.capability,
                            reason=a.reason)
            return f"task {task.id} added ({dept}{', ' + a.capability if a.capability else ''})"
        if a.type == "start_work":
            c.control.start()
            return "work loop running"
        if a.type == "pause_work":
            c.control.pause()
            return "work loop paused"
        if a.type == "complete_task":
            t = c.runner.complete_manual(a.task_id, f"Reported by the owner in chat: {a.result.strip()}",
                                         0.0, who="HUMAN")
            return f"marked done: {t.description[:60]}"
        if a.type == "approve_task":
            t = c.runner.approve(a.task_id, "HUMAN")
            return f"approved: {t.description[:60]}"
        if a.type == "record_metric":
            exp = c.db.execute("SELECT id FROM experiments WHERE project_id = ?",
                               (a.project_id,)).fetchone()
            c.experiments.record_metric(exp["id"], a.metric.strip(), a.value,
                                        "owner (reported in chat)")
            return f"recorded {a.metric} = {a.value:g}"
        if a.type == "add_guideline":
            m = c.memory.add("personal", "preference", a.content.strip(), "HUMAN")
            return f"guideline saved ({m.id})"
        raise ChatError(f"unsupported action {a.type}")

    def _explain(self, problems: list[tuple[dict, str]]) -> str:
        """Code-written follow-up: what could not be done, and what the owner can decide."""
        lines = []
        for action, why in problems[:3]:
            what = ACTION_TEXT.get(action["type"], action["type"])
            name = action.get("name") or action.get("description") or ""
            hint = ""
            if "unallocated company budget" in why:
                free = self.c.config.total_budget_eur - sum(
                    p.budget_eur for p in self.c.projects.list(list(_LIVE)))
                hint = (f" Only EUR {max(free, 0):.2f} is free. Shall I use that, or should I "
                        f"finish or shrink another project?")
            elif "max_active_projects" in why:
                hint = " Shall I pause or finish one of the running projects first?"
            lines.append(f"⚠ I could not {what}{f' ({name[:60]})' if name else ''}: "
                         f"{why.split('refused: ')[-1]}.{hint}")
        return "\n".join(lines)

    def _start_loop(self) -> None:
        control = getattr(self.c, "control", None)
        if control is not None and control.status() != "running":
            control.start()
            self.c.events.record(ACTOR, "Started the work loop")

    def get(self, msg_id: int) -> dict:
        row = self.c.db.execute("SELECT * FROM chat_messages WHERE id = ?", (msg_id,)).fetchone()
        if row is None:
            raise ChatError(f"unknown message {msg_id}")
        return self._row(row)

    def delete_thread(self, thread: str) -> int:
        return self.c.db.execute("DELETE FROM chat_messages WHERE thread = ?", (thread,)).rowcount

    def _entry(self, msg_id: int, index: int):
        msg = self.get(msg_id)
        if not 0 <= index < len(msg["actions"]):
            raise ChatError("unknown action")
        return msg, msg["actions"][index]

    def _insert(self, role, content, actions, model, thread=MAIN) -> int:
        cur = self.c.db.execute(
            "INSERT INTO chat_messages (ts, role, content, actions, model, thread) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (utcnow().isoformat(), role, content, json.dumps(actions), model, thread))
        return cur.lastrowid

    def _save_actions(self, msg) -> None:
        self.c.db.execute("UPDATE chat_messages SET actions = ? WHERE id = ?",
                          (json.dumps(msg["actions"]), msg["id"]))

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["actions"] = json.loads(d["actions"])
        return d


def _result_text(task) -> str:
    """A task's result without the file list the runner appends (files are listed apart)."""
    return (task.result or "").split("\n\nFiles produced")[0].strip()
