"""
Compute resources: several machines (nodes) running Ollama, shared safely.

- nodes are registered in config/nodes.json (add a computer = add an entry)
- NodePool is a ModelClient: the rest of the system is unchanged, but every model
  call is sent to a node that has the model, preferring one where it is already
  loaded (avoids slow model swaps), then the least busy one
- every node has GPU slots (max_concurrent_gpu_tasks, 1 for a 4 GB card): a call
  must hold a slot, so the work loop, background jobs and chat never run two
  heavy models on one GPU at the same time - they queue instead
- slots and usage stats are process-wide, shared by all Company instances
- status() reports reachability, loaded models, VRAM, slots/queue and - for the
  local machine - GPU utilisation/temperature (nvidia-smi) and CPU/RAM (psutil)
"""

import json
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from clients import ChatMessage, ModelClient, ModelClientError, ModelResponse, OllamaClient


class NodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    description: str = ""
    ollama_url: str
    local: bool = False          # this machine: also read nvidia-smi + CPU/RAM
    vram_gb: float = Field(default=0, ge=0)
    max_concurrent_gpu_tasks: int = Field(default=1, ge=1, le=8)


class NodesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: dict[str, NodeConfig] = Field(min_length=1)

    @classmethod
    def load(cls, path: str | Path = "config/nodes.json") -> "NodesConfig":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


# --- process-wide slots and stats (shared by every Company in this process) -------------

_LOCK = threading.Lock()
_SLOTS: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
_STATS: dict[str, dict] = {}


def _slot(name: str, size: int) -> threading.BoundedSemaphore:
    with _LOCK:
        current = _SLOTS.get(name)
        if current is None or current[0] != size:
            _SLOTS[name] = (size, threading.BoundedSemaphore(size))
            _STATS.setdefault(name, {"active": 0, "waiting": 0, "calls": 0, "errors": 0,
                                     "busy_seconds": 0.0, "last_model": None, "last_call": None})
        return _SLOTS[name][1]


def _stat(name: str, **delta) -> None:
    with _LOCK:
        s = _STATS[name]
        for k, v in delta.items():
            s[k] = s[k] + v if isinstance(v, (int, float)) and k not in ("last_call",) else v


def reset_slots_for_tests() -> None:
    with _LOCK:
        _SLOTS.clear()
        _STATS.clear()


@contextmanager
def gpu_session(node: str, size: int = 1, timeout: float = 1800, pool=None,
                label: str = "media"):
    """Hold one GPU slot of a node for a non-LLM GPU program (e.g. image generation).
    Uses the SAME slots as model calls, so the two never overlap on one GPU. With a
    pool, the node's loaded Ollama models are unloaded first to free the VRAM."""
    slot = _slot(node, size)
    if not slot.acquire(blocking=False):
        _stat(node, waiting=1)
        try:
            if not slot.acquire(timeout=timeout):
                raise ModelClientError(f"node {node}: no GPU slot free after {timeout:.0f}s")
        finally:
            _stat(node, waiting=-1)
    _stat(node, active=1)
    start = time.perf_counter()
    try:
        if pool is not None:
            pool.unload_models(node)
        yield
        _stat(node, calls=1, last_model=label)
    finally:
        _stat(node, active=-1, busy_seconds=time.perf_counter() - start, last_call=time.time())
        slot.release()


# --- local machine metrics --------------------------------------------------------------

def parse_nvidia_smi(text: str) -> list[dict]:
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 5:
            continue
        name, util, used, total, temp = parts
        try:
            gpus.append({"name": name, "utilization_pct": float(util), "vram_used_mb": float(used),
                         "vram_total_mb": float(total), "temperature_c": float(temp)})
        except ValueError:
            continue
    return gpus


def nvidia_smi() -> list[dict] | None:
    """Fixed, code-controlled command (never built from model output)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
             "temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True)
        return parse_nvidia_smi(out.stdout)
    except (OSError, subprocess.SubprocessError):
        return None


def local_metrics() -> dict:
    try:
        import psutil
        mem = psutil.virtual_memory()
        cpu = {"cpu_pct": psutil.cpu_percent(interval=None), "cpu_cores": psutil.cpu_count(),
               "ram_used_gb": round(mem.used / 1e9, 1), "ram_total_gb": round(mem.total / 1e9, 1)}
    except ImportError:
        cpu = {}
    return {**cpu, "gpus": nvidia_smi()}


# --- the pool ---------------------------------------------------------------------------------

class NodePool(ModelClient):
    provider = "ollama"

    def __init__(self, config: NodesConfig, client_factory: Callable = OllamaClient,
                 acquire_timeout: float = 900, cache_seconds: float = 5):
        self.config = config
        self.acquire_timeout = acquire_timeout
        self.cache_seconds = cache_seconds
        self.clients: dict[str, ModelClient] = {
            name: client_factory(base_url=cfg.ollama_url)
            for name, cfg in config.nodes.items() if cfg.enabled}
        for name in self.clients:
            _slot(name, config.nodes[name].max_concurrent_gpu_tasks)
        self._cache: dict[str, tuple[float, set | None]] = {}

    # --- ModelClient interface ---------------------------------------------------------------

    def is_available(self) -> bool:
        return any(self._installed(n) is not None for n in self.clients)

    def list_models(self) -> list[str]:
        models: set[str] = set()
        reachable = False
        for name in self.clients:
            installed = self._installed(name)
            if installed is not None:
                reachable = True
                models |= installed
        if not reachable:
            raise ModelClientError("no compute node reachable")
        return sorted(models)

    def loaded_models(self) -> list[dict]:
        out = []
        for name, client in self.clients.items():
            try:
                out += [{**m, "node": name} for m in client.loaded_models()]
            except (ModelClientError, AttributeError):
                continue
        return out

    def chat(self, messages: list[ChatMessage], model: str, temperature: float | None = None,
             json_schema: dict | None = None, think: bool | None = None) -> ModelResponse:
        node = self._choose(model)
        slot = _slot(node, self.config.nodes[node].max_concurrent_gpu_tasks)
        if not slot.acquire(blocking=False):
            _stat(node, waiting=1)
            try:
                if not slot.acquire(timeout=self.acquire_timeout):
                    raise ModelClientError(f"node {node}: no GPU slot free after "
                                           f"{self.acquire_timeout:.0f}s")
            finally:
                _stat(node, waiting=-1)
        _stat(node, active=1)
        start = time.perf_counter()
        try:
            response = self.clients[node].chat(messages, model=model, temperature=temperature,
                                               json_schema=json_schema, think=think)
            response.raw = {**(response.raw or {}), "node": node}
            _stat(node, calls=1, last_model=model)
            return response
        except ModelClientError:
            _stat(node, errors=1)
            self._cache.pop(node, None)   # re-check reachability next time
            raise
        finally:
            _stat(node, active=-1, busy_seconds=time.perf_counter() - start,
                  last_call=time.time())
            slot.release()

    def unload_models(self, node: str, wait_seconds: float = 15) -> list[str]:
        """Unload every model loaded on a node (call while holding its GPU slot)."""
        client = self.clients.get(node)
        if client is None or not hasattr(client, "unload"):
            return []
        try:
            loaded = [m["name"] for m in client.loaded_models()]
            for name in loaded:
                client.unload(name)
            deadline = time.time() + wait_seconds
            while loaded and client.loaded_models() and time.time() < deadline:
                time.sleep(0.5)
        except ModelClientError:
            return []
        return loaded

    def node_for_slot(self, preferred: str | None = None) -> tuple[str, int]:
        """(node name, slot size) for a GPU program: the preferred node or the local one."""
        names = [n for n in self.config.nodes if self.config.nodes[n].enabled]
        name = preferred if preferred in names else next(
            (n for n in names if self.config.nodes[n].local), names[0] if names else "local")
        cfg = self.config.nodes.get(name)
        return name, cfg.max_concurrent_gpu_tasks if cfg else 1

    # --- monitoring ----------------------------------------------------------------------------

    def status(self) -> list[dict]:
        rows = []
        for name, cfg in self.config.nodes.items():
            row = {"name": name, **cfg.model_dump(), "reachable": False, "installed": [],
                   "loaded": [], "version": None}
            with _LOCK:
                row["slots"] = dict(_STATS.get(name, {}))
            client = self.clients.get(name)
            if client is not None:
                installed = self._installed(name, fresh=True)
                row["reachable"] = installed is not None
                row["installed"] = sorted(installed or [])
                if installed is not None:
                    try:
                        row["loaded"] = client.loaded_models()
                        row["version"] = client.version() if hasattr(client, "version") else None
                    except ModelClientError:
                        pass
            if cfg.local and cfg.enabled:
                row["metrics"] = local_metrics()
            rows.append(row)
        return rows

    # --- internals -----------------------------------------------------------------------------

    def _installed(self, name: str, fresh: bool = False) -> set | None:
        cached = self._cache.get(name)
        if cached and not fresh and time.time() - cached[0] < self.cache_seconds:
            return cached[1]
        try:
            models = set(self.clients[name].list_models())
        except ModelClientError:
            models = None
        self._cache[name] = (time.time(), models)
        return models

    def _choose(self, model: str) -> str:
        candidates = [n for n in self.clients if model in (self._installed(n) or set())]
        if not candidates:
            raise ModelClientError(f"no reachable node has model {model!r}")

        def loaded(n):
            try:
                return any(m["name"] == model for m in self.clients[n].loaded_models())
            except (ModelClientError, AttributeError):
                return False

        def busy(n):
            with _LOCK:
                s = _STATS[n]
                return (s["active"] + s["waiting"]) / self.config.nodes[n].max_concurrent_gpu_tasks

        # Prefer: free node with the model already loaded > free node > least busy.
        return min(candidates, key=lambda n: (busy(n) >= 1, not loaded(n), busy(n)))
