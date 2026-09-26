# AI Company — project context

A local AI orchestration system on Windows (GTX 1650 Super, 4 GB VRAM) that runs small
online-business experiments. Python 3.12 + Ollama (`gemma3:4b` fast, `qwen3:4b` reasoning).
It researches the web, builds digital products (print-ready PDFs), marketing images,
AI photos (local Stable Diffusion), short videos with voiceover and campaign packs.
Owner runbook: `PRODUCTION.md`.

## Core principle (never break)
"AI decides what should happen. Code decides what is allowed. Code executes."
- Models only PROPOSE, as JSON validated by Pydantic (`core/schemas.py`, `core/structured.py`).
- Code assigns IDs and enforces budgets, permissions, statuses and limits.
- The AI never runs shell commands, installs software, reads arbitrary files, calls
  arbitrary URLs, spends money or publishes. New abilities = code-controlled tools
  (`Tool` subclasses registered in `core/tools.py:tool_classes()`) + `config/tools.json`
  (allowlisted hosts, API keys from env vars only, validated `settings`).
- The AI never edits code, config or security rules itself. Its ideas become
  improvement proposals that a human accepts.

## Claude as orchestrator
When the owner asks you to act as the orchestrator (run the company, not just code it),
read `orchestrator/ROLE.md` and `orchestrator/CONTEXT.md` first and follow them. That role
may change code and run tools itself; the rules above still bind the local models inside
the system, and ROLE.md's hard rules (legal, budgets, owner-confirmed steps) bind you.

## Layout
- `clients/` — `ModelClient` interface, `OllamaClient` (HTTP API, stdlib only)
- `core/company.py` — `Company(...)` wires everything; start here. `run_tool()` = human-started tool run
- `core/chat.py` — the orchestrator chat: model decides actions (`ProposedAction`), code
  validates; autopilot runs safe ones at once (`AUTO`), `ALWAYS_CONFIRM` + big budgets wait
  for the owner. `core/autopilot.py` — advances projects stage by stage from the work loop
  (plan / review / finish / pause+ask) and tells the owner about waiting tasks.
  `company.control` = work-loop start/pause/status (set by the dashboard)
- `core/power.py` — `KeepAwake` (SetThreadExecutionState): the runner thread keeps Windows
  awake only while READY/RUNNING tasks exist (+ grace), studio jobs while they run
- `core/` — orchestrator, project_manager, departments, workers, runner, task_queue,
  permissions (approval tiers, expenses), experiments, memory/learning, research,
  tools, resources (multi-node GPU slots, `gpu_session`), chat, router
- Studio (tools): `web.py` (search + SSRF-safe page reading + cited report),
  `products.py` (fpdf2 PDFs, previews, mockups, listing, zip), `imagegen.py` (graphics +
  AI photo providers sdcpp/a1111/openai/pollinations), `video.py` (ffmpeg), `voice.py`
  (Piper/ElevenLabs), `campaigns.py` (channel-validated posts, emails, blog, pack)
- Support: `net.py` (allowlisted API requests, `fetch_public`), `workspace.py` (files +
  `assets` table, DRAFT/APPROVED/REJECTED, human-only approval), `graphics.py`,
  `design.py` (themes, formats, OFL fonts), `engines.py` (`run_fixed` for local exes),
  `studio.py`, `backup.py`
- `config/*.json` — company limits (+ brand_name, workspace_dir, backup_keep), policy,
  models + task profiles, departments, tools (+ settings), nodes. Validated on load and
  cross-checked (`check_config`, incl. each tool's `Settings` schema).
- `dashboard/` — FastAPI server (`server.py`), background runner/jobs + separate studio
  job queue (`services.py`), auth, config editor; `static/` is vanilla JS (no build
  step), `ship.js` = spaceship view, Studio view = create forms + file gallery
- `data/company.db` — SQLite (WAL), daily backups in `data/backups/`;
  `logs/company.log` — readable event log (rotated at 5 MB); `workspace/` — produced files:
  `projects/<created>_<name>_<id>/` with `INDEX.md` (all files + status), `products|campaigns/
  <title>/v<N>_<date>/` (same title = next version, `Workspace.version_dir`), `research/<date>_…`,
  `images|videos/<date>_<name>/` (`dated_dir`); old layout -> `scripts/reorganize_workspace.py`
- Local engines (not in git, installed by `setup_assets.py`, hash-pinned): `assets/fonts`,
  `tools/runtime` (app-local signed MSVC DLLs), `tools/sdcpp`, `models/voices`, `models/image`

## Research method (owner rule - always)
All market / niche / product research uses `core/research.py:RESEARCH_METHOD`: 3 profitable
pockets (asked repeatedly + already paid to solve), exact buyer, quoted words, current price,
score /5 on competition, longevity, effort, sells-while-asleep, repeat = total /25, one winner.
It is injected into the chat rules, research workers and `web_research` (code totals the
scores and picks the winner). Details: `orchestrator/ROLE.md` 2b.

## Conventions
- Simple Python, few dependencies (`requirements.txt`). No frameworks without need.
- Log every important action via `EventLog.record(actor, action, project_id, task_id, **details)`
  (don't pass `action=` as a detail kwarg). Studio tools log as `STUDIO` / `RESEARCH`.
- Status changes go through the allowed-transition tables (tasks, projects, assets).
- Memory scopes are separate: personal (human-only), project, operational (needs evidence).
- The frontend runs under a strict CSP: no inline `<script>` and no `style=` attributes.
  Use classes, or `data-pct`/`data-bg` + `dyn()`.
- Model output shown in the UI is untrusted: always `esc()` it. Web page text given to a
  model is marked untrusted; model text reaches local programs only via files.
- Only one model fits in VRAM: model calls go through `NodePool` GPU slots; other GPU
  programs (sd-cli) use `gpu_session()` on the same slot, which unloads Ollama first.
- Use `127.0.0.1`, never `localhost`, for local services (Windows adds ~2 s per request
  trying IPv6 first). Dashboard endpoints must not hold `api_lock` during network calls
  (use the `cached()` helper in server.py).
- Tool costs: `Tool.max_cost()` is checked against the project budget BEFORE running;
  `ToolOutput.cost_eur` is the real cost. Multi-step tools call `ToolContext.generate()`.
- Tests: `Company(":memory:", ...)` writes files to a temp dir; use a schema-title fake
  model (`SchemaFake` in test_phase16-18) for tool tests.

## Run / test
- Setup: `pip install -r requirements.txt`, `python setup_assets.py`, `python doctor.py`
- Dashboard: `python run_dashboard.py --set-password`, then `python run_dashboard.py`
  (port 8765, LAN; demos: `python seed_demo.py` then `--local-preview-no-auth --host
  127.0.0.1 --db data/demo.db --workspace data/demo_workspace`)
- Autostart: `scripts/install_autostart.ps1` (per-user Scheduled Task)
- Tests: `python test_phase2.py` … `test_phase19.py` (plus `test_phases8_11.py`,
  `test_phases12_13.py`). Each has offline parts (fakes) and a live part needing Ollama
  (16: internet, 17: GPU image gen, 18: end-to-end, ~40 min). The owner prefers FAST
  checks: run `python test_phaseN.py --offline` (skips live parts; all suites ~1 min) and
  test the live model with one targeted call instead of the full live suites. Never run
  two live suites at once (they thrash the 4 GB GPU).

## Status
Phases 1–19 done. 16: web research + digital products; 17: images, AI photos, voice,
video; 18: campaigns, Studio UI, backups, log rotation, doctor, autostart;
19: autonomous orchestrator (autopilot chat actions + stage-by-stage autopilot loop),
dashboard freeze fix (localhost -> 127.0.0.1, no network calls under the API lock).
Candidate next steps: publishing integrations behind the `publishing` permission
(Etsy/Gumroad upload, Buffer scheduling) as human-approved tools, sales-metric import,
Z-Image Turbo as a higher-quality photo model, benchmarking Hermes as orchestrator.
