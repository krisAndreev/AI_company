"""
Config editing for the dashboard: validate -> back up -> save, with full history.

Only the known config files can be touched (no arbitrary paths). A save is
rejected unless the new file passes its Pydantic model AND the cross-file checks
together with all the other current config files.
"""

import difflib
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from core.company import check_config
from core.config import OrchestratorConfig
from core.departments import DepartmentRegistry
from core.model_registry import ModelRegistry
from core.permissions import ApprovalPolicy
from core.resources import NodesConfig
from core.tools import ToolsConfig

CONFIG_FILES = {
    "company.json": (OrchestratorConfig, "Company limits: total budget, active projects, "
                                         "confidence threshold, retries"),
    "policy.json": (ApprovalPolicy, "Spending approval tiers and project permissions"),
    "models.json": (ModelRegistry, "Model registry and task-type routing profiles"),
    "departments.json": (DepartmentRegistry, "Departments and their capabilities"),
    "tools.json": (ToolsConfig, "External tools: allowlists, limits, API key variables"),
    "nodes.json": (NodesConfig, "Computers running models: address, GPU slots"),
}


class ConfigError(ValueError):
    pass


class ConfigAdmin:
    def __init__(self, config_dir: str | Path = "config",
                 history_dir: str | Path = "config/history"):
        self.dir = Path(config_dir)
        self.history_dir = Path(history_dir)

    def files(self) -> list[dict]:
        return [{"name": name, "description": desc,
                 "modified": datetime.fromtimestamp((self.dir / name).stat().st_mtime)
                 .isoformat(timespec="seconds")}
                for name, (_, desc) in CONFIG_FILES.items()]

    def read(self, name: str) -> str:
        return self._path(name).read_text(encoding="utf-8")

    def validate(self, name: str, text: str) -> list[str]:
        """Return a list of problems (empty = valid)."""
        self._path(name)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return [f"invalid JSON: {e.msg} (line {e.lineno}, column {e.colno})"]
        model, _ = CONFIG_FILES[name]
        try:
            model.model_validate(data)
        except ValidationError as e:
            return [f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
                    for err in e.errors()]
        # Cross-file check: current files with this one replaced.
        with tempfile.TemporaryDirectory() as tmp:
            for other in CONFIG_FILES:
                shutil.copy(self.dir / other, Path(tmp) / other)
            (Path(tmp) / name).write_text(text, encoding="utf-8")
            try:
                check_config(DepartmentRegistry.load(Path(tmp) / "departments.json"),
                             ApprovalPolicy.load(Path(tmp) / "policy.json"),
                             ModelRegistry.load(Path(tmp) / "models.json"),
                             ToolsConfig.load(Path(tmp) / "tools.json"))
            except (ValueError, ValidationError) as e:
                return [f"cross-file check: {e}"]
        return []

    def save(self, name: str, text: str) -> dict:
        problems = self.validate(name, text)
        if problems:
            raise ConfigError("; ".join(problems))
        path = self._path(name)
        old = path.read_text(encoding="utf-8")
        if old == text:
            return {"changed": False, "backup": None, "diff": ""}
        backup = self._backup(name, old)
        path.write_text(text, encoding="utf-8")
        diff = "".join(difflib.unified_diff(old.splitlines(True), text.splitlines(True),
                                            f"{name} (before)", f"{name} (after)", n=1))
        return {"changed": True, "backup": backup, "diff": diff}

    def history(self, name: str) -> list[dict]:
        self._path(name)
        folder = self.history_dir / name
        if not folder.exists():
            return []
        return [{"version": p.stem, "saved": p.stem.replace("_", " ", 1)}
                for p in sorted(folder.glob("*.json"), reverse=True)]

    def read_version(self, name: str, version: str) -> str:
        self._path(name)
        if not all(ch.isalnum() or ch in "-_" for ch in version):
            raise ConfigError("invalid version id")
        path = self.history_dir / name / f"{version}.json"
        if not path.exists():
            raise ConfigError(f"no version {version} of {name}")
        return path.read_text(encoding="utf-8")

    def restore(self, name: str, version: str) -> dict:
        return self.save(name, self.read_version(name, version))

    def _backup(self, name: str, text: str) -> str:
        folder = self.history_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        version = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        (folder / f"{version}.json").write_text(text, encoding="utf-8")
        return version

    def _path(self, name: str) -> Path:
        if name not in CONFIG_FILES:
            raise ConfigError(f"unknown config file {name!r}")
        return self.dir / name
