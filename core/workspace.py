"""
Workspace + asset store: every file the company produces (PDFs, images, videos,
audio, reports, campaign packs) lives under ONE folder and is registered in the
`assets` table with its hash, size, source and project.

- Paths are chosen by code (slugified), never by the model, and always checked to
  stay inside the workspace root.
- Assets start as DRAFT. Only a HUMAN may approve or reject them: approval is the
  quality gate before anything is published (publishing itself stays manual).
"""

from __future__ import annotations  # lets `list[...]` hints work after the list() method

import hashlib
import json
import mimetypes
import re
import unicodedata
import uuid
from pathlib import Path

from core.db import Database
from core.tasks import utcnow

ASSET_STATUSES = ("DRAFT", "APPROVED", "REJECTED")
ALLOWED_ASSET_TRANSITIONS = {
    "DRAFT": {"APPROVED", "REJECTED"},
    "APPROVED": {"REJECTED"},
    "REJECTED": {"DRAFT", "APPROVED"},
}
THUMB_SIZE = 480

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    project_id  TEXT,
    task_id     TEXT,
    group_id    TEXT,              -- product / campaign / research run that produced it
    kind        TEXT NOT NULL,     -- product_pdf, bundle, image, mockup, video, audio, report, copy, pack
    title       TEXT NOT NULL,
    path        TEXT NOT NULL,     -- relative to the workspace root
    mime        TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    source      TEXT NOT NULL,     -- tool / engine that made it (template, sdcpp, piper, ...)
    meta        TEXT NOT NULL,     -- JSON
    status      TEXT NOT NULL,     -- DRAFT, APPROVED, REJECTED
    decided_by  TEXT,
    decided_at  TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_project ON assets(project_id, kind);
CREATE INDEX IF NOT EXISTS idx_assets_group ON assets(group_id);
"""

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("image/webp", ".webp")


class AssetRuleError(ValueError):
    pass


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text[:max_len].rstrip("-")) or "item"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Workspace:
    def __init__(self, root: str | Path = "workspace"):
        self.root = Path(root).resolve()

    def dir_for(self, project_id: str | None, *parts: str) -> Path:
        """A folder for a project's output (created on demand)."""
        path = self.safe_path(project_id and f"projects/{slugify(project_id, 40)}"
                              or "shared", *[slugify(p, 60) for p in parts])
        path.mkdir(parents=True, exist_ok=True)
        return path

    def safe_path(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts).resolve()
        if path != self.root and self.root not in path.parents:
            raise AssetRuleError("path escapes the workspace")
        return path

    def relative(self, path: Path) -> str:
        path = Path(path).resolve()
        if self.root not in path.parents:
            raise AssetRuleError("file is outside the workspace")
        return path.relative_to(self.root).as_posix()

    def thumbs_dir(self) -> Path:
        path = self.root / ".thumbs"
        path.mkdir(parents=True, exist_ok=True)
        return path


class AssetStore:
    def __init__(self, db: Database, workspace: Workspace, events=None):
        self.db = db
        self.workspace = workspace
        self.events = events
        self.db.executescript(_SCHEMA)

    # --- writing ------------------------------------------------------------------

    def register(self, path: Path, kind: str, title: str, source: str,
                 project_id: str | None = None, task_id: str | None = None,
                 group_id: str | None = None, meta: dict | None = None,
                 thumb_from: Path | None = None) -> dict:
        """Record a file the company produced. thumb_from: image to make the thumbnail from
        (defaults to the file itself for images)."""
        path = Path(path)
        rel = self.workspace.relative(path)
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        asset_id = new_id("asset")
        self.db.execute(
            "INSERT INTO assets (id, ts, project_id, task_id, group_id, kind, title, path, mime, "
            "bytes, sha256, source, meta, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'DRAFT')",
            (asset_id, utcnow().isoformat(), project_id, task_id, group_id, kind, title[:200], rel,
             mime, len(data), hashlib.sha256(data).hexdigest(), source,
             json.dumps(meta or {}, default=str)))
        source_img = thumb_from or (path if mime.startswith("image/") else None)
        if source_img:
            self._make_thumb(asset_id, Path(source_img))
        return self.get(asset_id)

    def decide(self, asset_id: str, approve: bool, who: str = "HUMAN", note: str = "") -> dict:
        if who != "HUMAN":
            raise AssetRuleError("only a HUMAN can approve or reject assets")
        asset = self.get(asset_id)
        new = "APPROVED" if approve else "REJECTED"
        if new not in ALLOWED_ASSET_TRANSITIONS[asset["status"]]:
            raise AssetRuleError(f"asset {asset_id}: {asset['status']} -> {new} is not allowed")
        self.db.execute("UPDATE assets SET status = ?, decided_by = ?, decided_at = ?, note = ? "
                        "WHERE id = ?", (new, who, utcnow().isoformat(), note[:500], asset_id))
        return self.get(asset_id)

    # --- reading ------------------------------------------------------------------

    def get(self, asset_id: str) -> dict:
        row = self.db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown asset {asset_id}")
        return self._row(row)

    def list(self, project_id: str | None = None, kind: str | None = None,
             status: str | None = None, group_id: str | None = None,
             limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM assets WHERE 1=1", []
        for col, val in (("project_id", project_id), ("kind", kind), ("status", status),
                         ("group_id", group_id)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        sql += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self.db.execute(sql, args)]

    def file_path(self, asset_id: str) -> Path:
        """Absolute path of an asset's file, re-checked to be inside the workspace."""
        asset = self.get(asset_id)
        path = self.workspace.safe_path(*asset["path"].split("/"))
        if not path.is_file():
            raise KeyError(f"file for asset {asset_id} is missing")
        return path

    def thumb_path(self, asset_id: str) -> Path | None:
        path = self.workspace.thumbs_dir() / f"{asset_id}.jpg"
        return path if path.is_file() else None

    def summary_lines(self, assets: list[dict]) -> list[str]:
        return [f"{a['kind']}: {a['title']} ({a['path']}, {a['bytes'] // 1024} KB, id {a['id']})"
                for a in assets]

    # --- internals ----------------------------------------------------------------

    def _make_thumb(self, asset_id: str, source: Path) -> None:
        try:
            from PIL import Image
            with Image.open(source) as im:
                im = im.convert("RGB")
                im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                im.save(self.workspace.thumbs_dir() / f"{asset_id}.jpg", quality=82)
        except Exception:  # a missing thumbnail never fails the asset
            pass

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["meta"] = json.loads(d["meta"])
        return d
