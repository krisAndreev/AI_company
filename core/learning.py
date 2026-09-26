"""
Learning from results.

- operational_stats(): code-computed performance per model and department
  (from the agents table) - hard evidence, no AI involved.
- learn_from_experiment(): after an experiment ends, the AI proposes lessons and
  improvement ideas. Code stores lessons with the experiment as evidence (confidence
  capped by evidence count), merges duplicates, and files improvements as PROPOSED.
"""

import json
from dataclasses import dataclass, field

from core.experiments import ExperimentStatus
from core.memory import MemoryItem
from core.project_manager import ManagementError
from core.router import RouteRequest
from core.schemas import LearningReport
from core.structured import generate_structured

ACTOR = "LEARNING"


@dataclass
class LearningOutcome:
    new_lessons: list[MemoryItem] = field(default_factory=list)
    confirmed: list[MemoryItem] = field(default_factory=list)
    improvement_ids: list[str] = field(default_factory=list)
    model_key: str | None = None


class LearningEngine:
    def __init__(self, company):
        self.c = company

    def operational_stats(self) -> list[dict]:
        rows = self.c.db.execute("""
            SELECT model_key, department,
                   COUNT(*) AS runs,
                   SUM(status = 'COMPLETED') AS completed,
                   SUM(status = 'FAILED') AS failed,
                   AVG((julianday(finished_at) - julianday(created_at)) * 86400) AS avg_seconds
            FROM agents WHERE finished_at IS NOT NULL
            GROUP BY model_key, department ORDER BY runs DESC""")
        return [{**dict(r), "success_rate": round(r["completed"] / r["runs"], 2),
                 "avg_seconds": round(r["avg_seconds"] or 0, 1)} for r in rows]

    def learn_from_experiment(self, exp_id: str) -> LearningOutcome:
        exp = self.c.experiments.get(exp_id)
        if exp.status == ExperimentStatus.RUNNING:
            raise ManagementError("learning only happens after an experiment has ended")
        source = f"LEARNING:{exp_id}"
        already = self.c.db.execute("SELECT 1 FROM memory WHERE source = ? LIMIT 1",
                                    (source,)).fetchone()
        if already:
            raise ManagementError(f"already learned from {exp_id}")

        tasks = self.c.queue.list(project_id=exp.project_id)
        facts = {
            "experiment": exp.spec.model_dump(),
            "outcome": {"status": exp.status.value, "reason": exp.final_result,
                        "pivots_used": exp.pivots_used, "human_hours": exp.human_hours_used},
            "metrics": self.c.experiments.latest_metrics(exp_id),
            "tasks": [{"task": t.description, "department": t.department,
                       "status": t.status.value, "result": (t.result or t.error or "")[:200]}
                      for t in tasks],
            "model_stats": self.operational_stats(),
        }
        route = self.c.router.select(RouteRequest(task_type="decision"))
        report = generate_structured(
            route.client, route.model,
            "An experiment has ended. Extract short, reusable lessons: 'project' lessons are "
            "about this product/market; 'operational' lessons are about how our company, "
            "models and workflow perform. Only state what the facts support. Optionally "
            "propose improvements to our configuration or prompts.\n"
            + json.dumps(facts, indent=2, default=str),
            LearningReport,
            system="You are a careful analyst. Evidence first; no speculation.",
            think=route.spec.think,
        ).value

        out = LearningOutcome(model_key=route.model_key)
        for lesson in report.lessons:
            project_id = exp.project_id if lesson.scope == "project" else None
            same = self.c.memory.find_same(lesson.scope, lesson.content, project_id)
            if same:
                out.confirmed.append(self.c.memory.confirm(same.id, exp_id))
                continue
            item = self.c.memory.add(lesson.scope, lesson.kind, lesson.content, source,
                                     project_id=project_id, evidence=[exp_id],
                                     confidence=lesson.confidence)
            out.new_lessons.append(item)
            self.c.events.record(ACTOR, "Stored lesson", exp.project_id, memory=item.id,
                                 scope=item.scope, confidence=item.confidence,
                                 content=item.content)
        for idea in report.improvements:
            imp_id = self.c.improvements.propose(idea.target, idea.proposal, idea.rationale,
                                                 [exp_id], source)
            out.improvement_ids.append(imp_id)
            self.c.events.record(ACTOR, "Proposed improvement (awaiting human)", exp.project_id,
                                 improvement=imp_id, target=idea.target, proposal=idea.proposal)
        return out
