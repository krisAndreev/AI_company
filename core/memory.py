"""
Memory, kept in three separate scopes that are never mixed automatically:

  personal     facts/preferences about the owner. Only a HUMAN may write these.
  project      knowledge about ONE project; only shown to that project.
  operational  how the company works (models, workflows, what worked/failed).
               Must cite evidence; confidence is capped by code from evidence count.

Improvement proposals (changes to config/prompts/code) are stored for a human to
decide. Nothing here changes the system automatically.
"""

import json
import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from core.db import Database
from core.tasks import utcnow

Scope = Literal["personal", "project", "operational"]
RETIRE_BELOW = 0.2


def evidence_cap(n_evidence: int) -> float:
    """Max confidence code allows for n independent pieces of evidence: 0.5, 0.75, 0.875..."""
    return 1 - 0.5 ** n_evidence


class MemoryItem(BaseModel):
    id: str
    scope: Scope
    project_id: str | None = None
    kind: str
    content: str
    source: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)  # task / experiment / tool-call ids
    status: Literal["active", "retired"] = "active"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    id          TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,
    project_id  TEXT,
    kind        TEXT NOT NULL,
    content     TEXT NOT NULL,
    source      TEXT NOT NULL,
    confidence  REAL NOT NULL,
    evidence    TEXT NOT NULL,     -- JSON list
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(scope, project_id);
CREATE TABLE IF NOT EXISTS improvements (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    target      TEXT NOT NULL,
    proposal    TEXT NOT NULL,
    rationale   TEXT NOT NULL,
    evidence    TEXT NOT NULL,     -- JSON list
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,     -- PROPOSED, ACCEPTED, REJECTED
    decided_by  TEXT,
    decided_at  TEXT
);
"""


class MemoryRuleError(ValueError):
    pass


class MemoryStore:
    def __init__(self, db: Database):
        self.db = db
        self.db.executescript(_SCHEMA)

    # --- writing ------------------------------------------------------------------

    def add(self, scope: Scope, kind: str, content: str, source: str,
            project_id: str | None = None, evidence: list[str] | None = None,
            confidence: float = 1.0) -> MemoryItem:
        evidence = list(dict.fromkeys(evidence or []))
        if scope == "personal" and source != "HUMAN":
            raise MemoryRuleError("personal memory can only be written by a HUMAN")
        if (scope == "project") != (project_id is not None):
            raise MemoryRuleError("project_id is required for project memory and forbidden otherwise")
        if scope == "operational":
            if not evidence:
                raise MemoryRuleError("operational memory needs evidence")
            if source != "HUMAN":
                confidence = min(confidence, evidence_cap(len(evidence)))
        item = MemoryItem(id="mem_" + uuid.uuid4().hex[:12], scope=scope, project_id=project_id,
                          kind=kind, content=content.strip(), source=source,
                          confidence=confidence, evidence=evidence)
        self._save(item)
        return item

    def confirm(self, memory_id: str, evidence_ref: str) -> MemoryItem:
        """New evidence supports the item: confidence may rise to the new evidence cap."""
        item = self.get(memory_id)
        if evidence_ref not in item.evidence:
            item.evidence.append(evidence_ref)
        if item.scope == "operational" and item.source != "HUMAN":
            item.confidence = max(item.confidence, evidence_cap(len(item.evidence)))
        item.updated_at = utcnow()
        self._save(item)
        return item

    def contradict(self, memory_id: str, evidence_ref: str) -> MemoryItem:
        """New evidence contradicts the item: halve confidence; retire when too low."""
        item = self.get(memory_id)
        item.evidence.append(f"contradicted_by:{evidence_ref}")
        item.confidence = round(item.confidence / 2, 4)
        if item.confidence < RETIRE_BELOW:
            item.status = "retired"
        item.updated_at = utcnow()
        self._save(item)
        return item

    def retire(self, memory_id: str) -> MemoryItem:
        item = self.get(memory_id)
        item.status = "retired"
        item.updated_at = utcnow()
        self._save(item)
        return item

    def find_same(self, scope: Scope, content: str, project_id: str | None = None) -> MemoryItem | None:
        norm = _normalize(content)
        for item in self.recall(scope, project_id=project_id, limit=500):
            if _normalize(item.content) == norm:
                return item
        return None

    # --- reading ------------------------------------------------------------------

    def get(self, memory_id: str) -> MemoryItem:
        row = self.db.execute("SELECT * FROM memory WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown memory: {memory_id}")
        return self._from_row(row)

    def recall(self, scope: Scope, project_id: str | None = None, query: str | None = None,
               limit: int = 10, min_confidence: float = 0.0) -> list[MemoryItem]:
        """Active items of ONE scope. Project scope always requires the project_id."""
        if scope == "project" and project_id is None:
            raise MemoryRuleError("project memory can only be recalled for a specific project")
        sql = "SELECT * FROM memory WHERE status = 'active' AND scope = ? AND confidence >= ?"
        args: list = [scope, min_confidence]
        if scope == "project":
            sql += " AND project_id = ?"
            args.append(project_id)
        words = [w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 3]
        if words:
            sql += " AND (" + " OR ".join("LOWER(content) LIKE ?" for _ in words) + ")"
            args += [f"%{w}%" for w in words]
        sql += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
        args.append(limit)
        return [self._from_row(r) for r in self.db.execute(sql, args)]

    def context_for(self, project_id: str, query: str = "") -> str:
        """Memory block for prompts: personal + THIS project's + relevant operational."""
        blocks = []
        for title, items in [
            ("Owner preferences (personal)", self.recall("personal", limit=10)),
            ("This project's memory", self.recall("project", project_id=project_id, limit=10)),
            ("Company lessons (operational, evidence-based)",
             self.recall("operational", query=query, limit=8, min_confidence=0.3)),
        ]:
            if items:
                blocks.append(title + ":\n" + "\n".join(
                    f"- {i.content} (confidence {i.confidence:.2f})" for i in items))
        return "\n".join(blocks)

    # --- internals ----------------------------------------------------------------

    def _save(self, item: MemoryItem) -> None:
        data = item.model_dump(mode="json")
        data["evidence"] = json.dumps(data["evidence"])
        cols = list(data)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "id")
        self.db.execute(
            f"INSERT INTO memory ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}", [data[c] for c in cols])

    @staticmethod
    def _from_row(row) -> MemoryItem:
        data = dict(row)
        data["evidence"] = json.loads(data["evidence"])
        return MemoryItem.model_validate(data)


class ImprovementStore:
    """AI-proposed system changes. Humans decide; accepted ones are implemented and
    deployed deliberately by a human - never applied by the system itself."""

    def __init__(self, db: Database):
        self.db = db
        self.db.executescript(_SCHEMA)

    def propose(self, target: str, proposal: str, rationale: str, evidence: list[str],
                source: str) -> str:
        imp_id = "imp_" + uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO improvements (id, ts, target, proposal, rationale, evidence, source, "
            "status) VALUES (?, ?, ?, ?, ?, ?, ?, 'PROPOSED')",
            (imp_id, utcnow().isoformat(), target, proposal, rationale, json.dumps(evidence),
             source))
        return imp_id

    def decide(self, imp_id: str, accept: bool, who: str = "HUMAN") -> None:
        if who != "HUMAN":
            raise MemoryRuleError("only a HUMAN can accept or reject improvements")
        cur = self.db.execute(
            "UPDATE improvements SET status = ?, decided_by = ?, decided_at = ? "
            "WHERE id = ? AND status = 'PROPOSED'",
            ("ACCEPTED" if accept else "REJECTED", who, utcnow().isoformat(), imp_id))
        if cur.rowcount != 1:
            raise MemoryRuleError(f"improvement {imp_id} not found or already decided")

    def list(self, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM improvements", []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY ts", args)]


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))
