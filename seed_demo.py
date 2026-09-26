"""Seed data/demo.db (+ data/demo_workspace) with sample projects AND real sample files
(a printable product, marketing images, a short video, a campaign pack) for previewing the
dashboard. No Ollama needed: a scripted stand-in model supplies the text.

    python seed_demo.py
    python run_dashboard.py --local-preview-no-auth --host 127.0.0.1 --db data/demo.db --workspace data/demo_workspace
"""
import json
import shutil
from pathlib import Path

from clients import ModelClient, ModelResponse
from core.company import Company
from core.experiments import ExperimentSpec
from core.schemas import TaskProposal


class DemoModel(ModelClient):
    """Canned replies by JSON-schema title (so the studio tools run without Ollama)."""
    provider = "ollama"
    REPLIES = {
        "ProductOutline": {"pages": [
            {"title": "Wedding Budget Overview", "purpose": "total budget, priorities, who pays"},
            {"title": "Expense Tracker", "purpose": "track every deposit and payment"},
            {"title": "Vendor Contacts", "purpose": "keep vendor details in one place"},
            {"title": "12-Month Checklist", "purpose": "what to book and when"}]},
        "ProductListing": {
            "title": "Wedding Budget Planner Printable - Expense Tracker, Vendor List, Checklist PDF",
            "description": "Keep your wedding spending calm and clear. This printable wedding budget "
                           "planner includes a budget overview, an expense tracker, a vendor contact "
                           "list and a 12-month checklist. Instant download PDF in A4 and US Letter - "
                           "print at home as often as you need. For personal use only.",
            "tags": ["wedding budget", "wedding planner", "budget tracker", "printable planner",
                     "wedding checklist", "vendor list", "instant download", "bride to be",
                     "engagement gift", "wedding organizer", "expense tracker", "pdf planner",
                     "planning binder"], "price_eur": 5.9},
        "CampaignStrategy": {"angle": "Plan the wedding you want without money stress",
                             "pillars": ["budget clarity", "calm planning"],
                             "hooks": ["Where did the wedding budget go?", "One page, zero surprises",
                                       "Plan calm, not cheap"],
                             "kpis": ["listing visits", "sales", "email sign-ups"]},
        "InstagramPlan": {"posts": [
            {"day": 1, "hook": "Where did the budget go?", "caption": "Most couples overspend by "
             "the third vendor. Track every deposit on one page.", "hashtags": ["weddingbudget",
             "weddingplanning", "bridetobe"], "visual": "product"},
            {"day": 4, "hook": "Plan calm, not cheap", "caption": "A budget is a plan for what "
             "matters most to you both.", "hashtags": ["weddingplanner"], "visual": "quote"}]},
        "PinterestPlan": {"posts": [
            {"day": 2, "hook": "Printable wedding budget planner", "caption": "Budget overview, "
             "expense tracker and vendor list in one printable kit.", "hashtags": ["wedding"],
             "visual": "product", "title": "Wedding Budget Planner Printable"}]},
        "EmailSequence": {"emails": [
            {"day": 1, "subject": "Your wedding budget, on one page", "preview": "Launch week",
             "body": "Hi there,\n\nWedding costs add up quietly. Our printable budget planner keeps "
                     "every deposit and payment on one page, so there are no surprises.\n\n"
                     "Launch week price ends Sunday.\n\nUnsubscribe any time."}]},
    }

    def is_available(self):
        return True

    def list_models(self):
        return ["gemma3:4b", "qwen3:4b"]

    def chat(self, messages, model, temperature=None, json_schema=None, think=None):
        title = (json_schema or {}).get("title", "")
        prompt = messages[-1].content
        if title == "PageContent":
            reply = self._page(prompt)
        else:
            reply = self.REPLIES[title]
        return ModelResponse(text=json.dumps(reply), model=model, provider="ollama",
                             duration_seconds=0)

    @staticmethod
    def _page(prompt):
        if "Page 1 of" in prompt:
            return {"blocks": [{"type": "table", "text": "Budget by category",
                                "columns": ["Category", "Budget", "Actual", "Difference"], "count": 12},
                               {"type": "lines", "text": "Our top three priorities", "count": 3}]}
        if "Page 2 of" in prompt:
            return {"blocks": [{"type": "table", "text": "Payments",
                                "columns": ["Date", "Vendor", "Item", "Amount", "Paid"], "count": 18}]}
        if "Page 3 of" in prompt:
            return {"blocks": [{"type": "table", "text": "Vendors",
                                "columns": ["Vendor", "Contact", "Phone", "Deposit"], "count": 10},
                               {"type": "notes", "text": "Questions to ask", "count": 6}]}
        return {"blocks": [{"type": "heading", "text": "12 months before"},
                           {"type": "checklist", "items": ["Set the budget together", "Choose a date",
                                                           "Book the venue", "Draft the guest list"]},
                           {"type": "heading", "text": "6 months before"},
                           {"type": "checklist", "items": ["Book photographer", "Order invitations",
                                                           "Plan the menu"]}]}


for f in ("data/demo.db", "data/demo.db-wal", "data/demo.db-shm"):
    Path(f).unlink(missing_ok=True)
shutil.rmtree("data/demo_workspace", ignore_errors=True)
c = Company("data/demo.db", {"ollama": DemoModel()}, log_path=None, workspace_dir="data/demo_workspace")


def task(pid, desc, dept, caps, cost=0.0, prio=2, approval=False, deps=None):
    return c.queue.add(TaskProposal(description=desc, department=dept, priority=prio,
                                    required_capabilities=caps, estimated_cost=cost,
                                    requires_approval=approval), pid, dependencies=deps)


exp = c.experiments.create(ExperimentSpec(
    name="First €10 — printable planners", objective="Generate the first EUR 10 of revenue "
    "from a printable budget planner on Etsy.", budget_eur=20, max_duration_days=30,
    max_human_hours=5, max_pivots=1, priority=1, checkpoint_days=[7, 14, 21],
    success_criteria=[{"metric": "sales", "op": ">=", "target": 1},
                      {"metric": "revenue_eur", "op": ">=", "target": 10}]))
p1 = exp.project_id
r = task(p1, "Identify 3 underserved printable planner niches on Etsy", "research", ["market_research"], prio=1)
c.queue.start(r.id, "seed"); c.queue.complete(r.id, "Niches: wedding budget, freelancer taxes, ADHD weekly planner")
task(p1, "Write the Etsy listing title, tags and description for the wedding budget planner", "content", ["write_text"], deps=[r.id])
task(p1, "Draft a design brief for an 8-page wedding budget planner", "design", ["design_brief"], deps=[r.id])
task(p1, "Buy a premium mockup template bundle", "finance", ["budget_analysis"], cost=6.5)
task(p1, "Publish the listing on Etsy", "content", ["publish_content"], prio=3)
c.experiments.record_metric(exp.id, "listing_views", 40, "etsy_stats")

p2 = c.orchestrator.create_project("Ebook: Budgeting for Freelancers", "Write and sell a short "
                                   "ebook on budgeting for freelancers.", 15, 2).id
o = task(p2, "Outline a 10-chapter ebook on freelancer budgeting", "content", ["outline_content"], prio=1)
task(p2, "Compare 3 competing ebooks on price and reviews", "research", ["competitor_analysis"])
task(p2, "Plan an organic launch on Reddit and newsletters", "marketing", ["plan_campaign"], deps=[o.id])
task(p2, "Model pricing: EUR 4.99 vs 9.99", "finance", ["budget_analysis"])

p3 = c.orchestrator.create_project("Stream clipping channel", "Test whether short clips from "
                                   "streamers can grow a channel.", 10, 3).id
a = task(p3, "Design an automated clip-selection workflow", "automation", ["design_workflow"], prio=1)
c.queue.start(a.id, "seed"); c.queue.complete(a.id, "Workflow: VOD -> chat spikes -> 60s clips")
c.orchestrator.set_project_status(p3, __import__("core.projects", fromlist=["ProjectStatus"]).ProjectStatus.PAUSED, "waiting for streamer permission")

c.memory.add("personal", "preference", "Prefer digital products that need under 2 hours of my time per week", "HUMAN")

# Real sample files made by the studio tools (template graphics only: no GPU needed).
for prov in ("sdcpp", "a1111", "openai", "pollinations"):
    getattr(c.tools.tools["image_studio"].settings, prov).enabled = False
c.run_tool("product_builder", {"product_type": "planner", "title": "Wedding Budget Planner",
                               "subtitle": "Printable budget kit for calm planning",
                               "audience": "couples planning a wedding on a budget", "pages": 4,
                               "theme": "botanical"}, p1, actor="DEMO")
c.run_tool("image_studio", {"purpose": "Pinterest pin", "format": "pin", "layout": "product",
                            "headline": "Wedding budget, finally calm", "subline": "Printable planner kit",
                            "cta": "Get it now", "theme": "botanical"}, p1, actor="DEMO")
c.run_tool("video_studio", {"title": "Wedding Budget Planner", "format": "story", "theme": "botanical",
                            "cta": "Link in bio", "scenes": [
                                {"caption": "Where did the budget go?", "narration": "Where did the wedding budget go?"},
                                {"caption": "Every payment on one page", "narration": "Track every deposit and payment on one calm page."},
                                {"caption": "Print it tonight", "narration": "Download it and start tonight."}]},
           p1, actor="DEMO")
c.run_tool("campaign_builder", {"name": "Launch week", "goal": "first 10 sales", "audience": "engaged couples",
                                "key_message": "Plan your wedding budget without stress",
                                "offer": "20% off launch week", "channels": ["instagram", "pinterest", "email"],
                                "duration_days": 7, "posts_per_channel": 2, "videos": 0,
                                "theme": "botanical"}, p1, actor="DEMO")
c.close()
print("demo data ready: data/demo.db + data/demo_workspace")
