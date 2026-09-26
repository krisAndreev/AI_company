"""
Market research data: every number keeps its source, timestamp and whether it is
an estimate. Code compares signals across sources; one source is never "truth".
"""

import csv
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from core.db import Database
from core.tasks import utcnow

# Owner rule (2026-09-26): every market / product / niche research follows this method.
# Injected into the chat rules, research workers and the web_research report; the /25 totals
# and the winner are computed by code (web.score_pockets), never taken from the model.
RESEARCH_METHOD = (
    "When doing the research use this: Find 3 profitable pockets — problems asked repeatedly "
    "and already paid to solve. Exact buyer, quoted words, current price, score /5 on "
    "competition, longevity, effort, sells-while-asleep, repeat — total /25. Pick one winner.")
RESEARCH_SCALE = ("Score each criterion 1-5 where 5 is best for us: competition 5 = little "
                  "competition, longevity 5 = evergreen, effort 5 = easy to make, "
                  "sells-while-asleep 5 = fully passive, repeat 5 = repeat buyers / follow-up "
                  "products.")
RESEARCH_CAPABILITIES = {"market_research", "competitor_analysis", "keyword_research",
                         "web_research"}


class MarketObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=100)
    metric: str = Field(min_length=1, max_length=60)   # e.g. competition_listings, median_price
    value: float
    unit: str = ""
    source: str = Field(min_length=1)                   # e.g. etsy_api, everbee_export
    retrieved_at: datetime = Field(default_factory=utcnow)
    is_estimate: bool
    note: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_data (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword      TEXT NOT NULL,
    metric       TEXT NOT NULL,
    value        REAL NOT NULL,
    unit         TEXT NOT NULL,
    source       TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    is_estimate  INTEGER NOT NULL,
    note         TEXT NOT NULL,
    task_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_kw ON market_data(keyword, metric);
"""


class ResearchStore:
    def __init__(self, db: Database):
        self.db = db
        self.db.executescript(_SCHEMA)

    def add_many(self, observations: list[MarketObservation], task_id: str | None = None) -> int:
        with self.db.transaction():
            for o in observations:
                self.db.execute(
                    "INSERT INTO market_data (keyword, metric, value, unit, source, retrieved_at,"
                    " is_estimate, note, task_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (o.keyword.lower().strip(), o.metric, o.value, o.unit, o.source,
                     o.retrieved_at.isoformat(), int(o.is_estimate), o.note, task_id))
        return len(observations)

    def import_csv(self, path: str | Path, source: str, is_estimate: bool = True) -> int:
        """Import exported research (e.g. from EverBee). Columns: keyword,metric,value[,unit,note]"""
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        observations = [MarketObservation(
            keyword=r["keyword"], metric=r["metric"], value=float(r["value"]),
            unit=r.get("unit") or "", note=r.get("note") or "", source=source,
            is_estimate=is_estimate) for r in rows]
        return self.add_many(observations)

    def compare(self, keyword: str) -> dict[str, dict]:
        """Per metric: latest value from each source, spread, and a code-made assessment."""
        rows = self.db.execute(
            "SELECT * FROM market_data WHERE keyword = ? ORDER BY id", (keyword.lower().strip(),))
        latest: dict[str, dict[str, dict]] = {}
        for r in rows:
            latest.setdefault(r["metric"], {})[r["source"]] = dict(r)  # later rows win
        out = {}
        for metric, by_source in latest.items():
            values = [v["value"] for v in by_source.values()]
            lo, hi = min(values), max(values)
            spread = (hi - lo) / abs(hi) * 100 if hi else 0.0
            if len(values) == 1:
                assessment = "single source - unverified"
            elif spread <= 25:
                assessment = "sources agree"
            elif spread <= 50:
                assessment = "sources differ moderately"
            else:
                assessment = "sources disagree - treat with caution"
            out[metric] = {
                "values": {s: v["value"] for s, v in by_source.items()},
                "estimates": [s for s, v in by_source.items() if v["is_estimate"]],
                "retrieved": {s: v["retrieved_at"][:16] for s, v in by_source.items()},
                "n_sources": len(values), "spread_pct": round(spread, 1),
                "assessment": assessment,
            }
        return out

    def compare_text(self, keyword: str) -> str:
        lines = [f"Market data for '{keyword}' (third-party numbers are estimates):"]
        for metric, m in self.compare(keyword).items():
            vals = ", ".join(f"{s}={v:g}" + (" (est.)" if s in m["estimates"] else "")
                             for s, v in m["values"].items())
            lines.append(f"- {metric}: {vals} -> {m['assessment']}")
        return "\n".join(lines) if len(lines) > 1 else f"No market data for '{keyword}'."
