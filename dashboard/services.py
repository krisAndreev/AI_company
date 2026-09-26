"""
Background services for the dashboard. Each runs in its own thread with its own
Company (own database connection), so slow model calls never block the web UI.

- RunnerService: the company's work loop (Start / Pause / Step) + periodic
  experiment evaluation.
- JobService: one-off slow AI actions requested from the UI (plan, review,
  evaluate, learn), executed one at a time. A second JobService runs studio jobs
  (web research, products, images, videos, campaigns) so a 5-minute product build
  never blocks a chat reply; GPU slots still keep model calls from overlapping.
- The runner also makes a daily database backup (data/backups, rotated).
"""

import queue
import threading
import time
import traceback
import uuid
from typing import Callable

from core.backup import maybe_backup
from core.experiments import ExperimentStatus
from core.power import KeepAwake
from core.tasks import TaskStatus, utcnow


class RunnerService:
    def __init__(self, make_company: Callable, idle_seconds: float = 5,
                 experiment_check_seconds: float = 60):
        self.make_company = make_company
        self.idle_seconds = idle_seconds
        self.experiment_check_seconds = experiment_check_seconds
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._reload = threading.Event()
        self._running = False
        self._step = False
        self._status = {"state": "stopped", "activity": "idle", "last_outcome": None,
                        "last_error": None, "tasks_run": 0, "since": None, "keep_awake": False}
        self._keep_awake = KeepAwake()
        self._thread = threading.Thread(target=self._main, name="runner", daemon=True)

    # --- control (called from web threads) --------------------------------------

    def launch(self) -> None:
        self._thread.start()

    def start(self) -> None:
        with self._lock:
            self._running = True
            self._status.update(state="running", since=utcnow().isoformat())
        self._wake.set()

    def pause(self) -> None:
        with self._lock:
            self._running = False
            self._status.update(state="paused", since=utcnow().isoformat())

    def step(self) -> None:
        with self._lock:
            self._step = True
        self._wake.set()

    def request_reload(self) -> None:
        self._reload.set()
        self._wake.set()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    # --- loop -------------------------------------------------------------------------

    def _main(self) -> None:
        company = self.make_company()
        self._recover(company)
        last_exp_check = 0.0
        last_backup_check = 0.0
        last_autopilot = 0.0
        last_busy = 0.0
        while not self._stop.is_set():
            if self._reload.is_set():
                self._reload.clear()
                company.close()
                company = self.make_company()
            with self._lock:
                work = self._running or self._step
                self._step = False
            if work and self._has_work(company):
                last_busy = time.time()
            self._power(company, last_busy)
            outcome = None
            if work:
                self._set(activity="working")
                try:
                    outcome = company.runner.run_once()
                    with self._lock:
                        self._status["last_outcome"] = outcome
                        if outcome:
                            self._status["tasks_run"] += 1
                except Exception as e:  # never let the loop die
                    self._set(last_error=f"{type(e).__name__}: {e}")
                    company.events.record("RUNNER", "Loop error", error=str(e)[:300],
                                          trace=traceback.format_exc()[-800:])
                self._set(activity="idle")
            if (self._running and outcome is None
                    and time.time() - last_autopilot > company.config.autopilot_interval_seconds):
                last_autopilot = time.time()
                if self._autopilot(company):      # it planned or decided something
                    last_busy = time.time()
            if self._running and time.time() - last_exp_check > self.experiment_check_seconds:
                last_exp_check = time.time()
                self._check_experiments(company)
            if time.time() - last_backup_check > 3600:
                last_backup_check = time.time()
                self._daily_backup(company)
            if not (self._running and outcome):
                self._wake.wait(timeout=self.idle_seconds if self._running else 1.0)
                self._wake.clear()
        self._keep_awake.set(False)
        company.close()

    @staticmethod
    def _has_work(company) -> bool:
        """Workable now. PENDING tasks may be stuck behind a step that waits for the owner,
        so they don't count (the PC may sleep while it waits for you)."""
        s = company.queue.stats()
        return bool(s["READY"] or s["RUNNING"])

    def _power(self, company, last_busy: float) -> None:
        """Keep the PC awake only while working (plus a short grace period so the
        orchestrator can plan the next stage). Idle or waiting for the owner: normal sleep."""
        cfg = company.config
        awake = (self._running and cfg.keep_awake_while_working
                 and time.time() - last_busy < cfg.keep_awake_grace_minutes * 60 + 1)
        self._keep_awake.set(awake)
        self._set(keep_awake=self._keep_awake.active)

    def _check_experiments(self, company) -> None:
        for exp in company.experiments.list(ExperimentStatus.RUNNING):
            try:
                self._set(activity="evaluating experiments")
                company.experiments.evaluate(exp.id)
            except Exception as e:
                company.events.record("RUNNER", "Experiment evaluation error",
                                      exp.project_id, error=str(e)[:300])
        self._set(activity="idle")

    def _autopilot(self, company) -> bool:
        """No task to run: let the orchestrator advance its projects (plan / review).
        Returns True if it did something."""
        if not company.config.autopilot:
            return False
        self._set(activity="orchestrator planning")
        notes = []
        try:
            notes = company.autopilot.step()
            if notes:
                with self._lock:
                    self._status["last_outcome"] = {"autopilot": notes[-1][:200]}
        except Exception as e:  # never let the loop die
            self._set(last_error=f"autopilot: {type(e).__name__}: {e}")
            company.events.record("RUNNER", "Autopilot error", error=str(e)[:300],
                                  trace=traceback.format_exc()[-800:])
        self._set(activity="idle")
        return bool(notes)

    @staticmethod
    def _daily_backup(company) -> None:
        try:
            path = maybe_backup(company.db, company.config.backup_keep)
            if path:
                company.events.record("RUNNER", "Database backup", file=str(path))
        except Exception as e:  # a failed backup must never stop the company
            company.events.record("RUNNER", "Database backup failed", error=str(e)[:300])

    @staticmethod
    def _recover(company) -> None:
        """Tasks left RUNNING by a crash/restart are failed so the PM can retry them."""
        for task in company.queue.list(TaskStatus.RUNNING):
            company.queue.fail(task.id, "interrupted: server restarted while running")
            company.events.record("RUNNER", "Recovered interrupted task", task.project_id,
                                  task.id)
        company.db.execute("UPDATE agents SET status = 'FAILED', error = 'interrupted' "
                           "WHERE status = 'RUNNING'")

    def _set(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)


class JobService:
    """Runs slow AI actions one at a time in the background; the UI polls the job."""

    def __init__(self, make_company: Callable, keep: int = 50):
        self.make_company = make_company
        self.keep = keep
        self._jobs: dict[str, dict] = {}
        self._order: list[str] = []
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._reload = threading.Event()
        self._thread = threading.Thread(target=self._main, name="jobs", daemon=True)

    def launch(self) -> None:
        self._thread.start()

    def submit(self, kind: str, label: str, fn: Callable) -> dict:
        """fn(company) -> JSON-able result."""
        job = {"id": "job_" + uuid.uuid4().hex[:10], "kind": kind, "label": label,
               "status": "queued", "result": None, "error": None,
               "created": utcnow().isoformat(), "finished": None}
        with self._lock:
            self._jobs[job["id"]] = job
            self._order.append(job["id"])
            for old in self._order[:-self.keep]:
                self._jobs.pop(old, None)
            self._order = self._order[-self.keep:]
        self._queue.put((job["id"], fn))
        return dict(job)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def recent(self) -> list[dict]:
        with self._lock:
            return [dict(self._jobs[j]) for j in reversed(self._order) if j in self._jobs]

    def request_reload(self) -> None:
        self._reload.set()

    def shutdown(self) -> None:
        self._queue.put(None)

    def _main(self) -> None:
        company = self.make_company()
        while True:
            item = self._queue.get()
            if item is None:
                break
            if self._reload.is_set():
                self._reload.clear()
                company.close()
                company = self.make_company()
            job_id, fn = item
            self._update(job_id, status="running")
            awake = KeepAwake()             # a product build or video must not be cut by sleep
            awake.set(company.config.keep_awake_while_working)
            try:
                result = fn(company)
                self._update(job_id, status="done", result=result, finished=utcnow().isoformat())
            except Exception as e:
                self._update(job_id, status="failed", error=f"{type(e).__name__}: {e}",
                             finished=utcnow().isoformat())
            finally:
                awake.set(False)
        company.close()

    def _update(self, job_id: str, **kw) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(kw)
