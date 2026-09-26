# Running the AI Company in production

This is the owner's runbook: install, configure, run every day, and fix problems.
Everything runs on this PC (Windows, GTX 1650 Super 4 GB). Nothing is published or
paid for without you.

## 1. Install (once)

```powershell
pip install -r requirements.txt
ollama pull gemma3:4b
ollama pull qwen3:4b
python setup_assets.py          # fonts, VC++ runtime (app-local), sd.cpp, Piper voice, image model (~2.8 GB)
python run_dashboard.py --set-password
python doctor.py                # must end with "0 failures"
```

`setup_assets.py` pins every download to a SHA-256 hash and installs only inside this
folder (`assets/`, `tools/`, `models/`) - no system-wide installs, no admin rights.

## 2. Configure

| What | Where | Notes |
|---|---|---|
| Brand name on products/graphics | `config/company.json` -> `brand_name` | e.g. "Maple & Ink Studio" |
| Budgets, limits | `config/company.json`, `config/policy.json` | total budget, approval tiers |
| Tool settings | `config/tools.json` | search provider, image providers, voice, limits per day |

All config files are editable in the dashboard (**Configuration**) with validation,
backups and diffs.

**Optional API keys** (environment variables - never put keys in files):

```powershell
setx TAVILY_API_KEY "tvly-..."        # recommended: reliable web search, 1,000 free searches/month
setx OPENAI_API_KEY "sk-..."          # optional: cloud photos (enable "openai" in tools.json)
setx POLLINATIONS_API_KEY "sk_..."    # optional: cheap cloud photos
setx ELEVENLABS_API_KEY "..."         # optional: premium voiceover (voice.engine = "elevenlabs")
setx ETSY_API_KEY "..."               # optional: live Etsy market data
```

Restart the dashboard after `setx`. Without keys everything still works: web search
uses DuckDuckGo (best effort), photos come from local Stable Diffusion, voice from Piper.

## 3. Run

```powershell
python run_dashboard.py                                   # http://localhost:8765 (and your LAN)
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1   # optional: start at logon
```

The work loop starts **paused**. Press **Start** when you want the company to work.

## 4. Daily workflow

1. **Talk to Orchestrator** - tell it what you want, in plain words: "Make and launch a
   printable weekly planner for busy parents, budget 15 EUR", "also make 3 Instagram
   images for it", "pause everything", "I published the listing: <link>", "I made 3 sales".
   With **autopilot** on (`config/company.json`, default) it acts immediately: creates and
   plans the project, adds tasks, and starts the work loop. It asks you when it needs
   information and posts updates in the chat ("stage 2 planned", "I need you: publish the
   listing", "project finished").
   Always waiting for your **Do it** click: spending approvals, finishing a project,
   personal guidelines, and projects with a budget above `autopilot_max_project_budget_eur`.
2. While the loop runs, the autopilot moves each project forward by itself: when a stage
   is done it reviews the project (continue / pause / finish) and plans the next stage, up
   to `max_stages_per_project`. Tasks use the real tools: `web_research`,
   `create_digital_product`, `create_graphics`, `create_video`, `create_campaign`.
3. Manual steps (publishing, purchases) wait for you; the orchestrator tells you in the
   chat. Tell it when you did them and it marks them done.
4. **Studio & Files** - every produced file appears as **DRAFT**: product PDFs (A4 + US
   Letter), download bundle, mockups, listing text, images, videos with subtitles,
   research reports, campaign packs. Preview, then **Approve** or **Reject**.
   You can also create things directly in **Studio -> Create**.
5. **Publish yourself**: upload the product bundle + mockups + listing to Etsy/Gumroad,
   schedule the campaign pack's posts (calendar.csv has dates, captions, hashtags and
   media file names). Then tell the orchestrator ("published, link ...").
6. Tell it your results (views, sales) - they are recorded on the experiment and used at
   its checkpoints.

Typical times on this PC: AI photo 50-70 s, 8-page product 3-6 min, 15-30 s video
20-60 s (+ photos), campaign pack 5-15 min, web research 2-5 min.

## 4b. Sleep and electricity

- While the work loop has tasks to do (and for 3 minutes after, so the orchestrator can
  plan the next stage), the app asks Windows not to sleep. The topbar shows
  "PC kept awake". Studio jobs do the same while they run.
- When everything is done, paused, or only waiting for you, it lets Windows sleep normally.
  Nothing in Windows settings is changed; you can always sleep the PC by hand (work then
  pauses and continues after wake; an interrupted step is retried automatically).
- Recommended Windows settings (Settings -> System -> Power): screen off after 5 min,
  sleep after 15-30 min when plugged in.
- Config: `keep_awake_while_working`, `keep_awake_grace_minutes` in `config/company.json`.

## 5. Safety model (what the AI can and cannot do)

- Models only **propose** JSON (search queries, product briefs, storyboards, captions).
  Code validates it, picks files and sizes, runs the fixed tools, and enforces budgets.
- The AI never runs commands, never chooses a URL to open (only pages returned by the
  search provider, public addresses only, robots.txt respected), never publishes, never
  spends money above the approval tiers. Paid tool calls are refused if their worst case
  exceeds the project's remaining budget.
- Web pages are treated as untrusted data; numbers are only kept if they literally
  appear on the page; findings without a real source are dropped.
- Photo prompts are cleaned and filtered; images never contain AI-rendered text (text is
  drawn by code). Fonts are open-licence (OFL), so products can be sold.

## 6. Backups and restore

- The running dashboard backs up the database daily to `data/backups/` (keeps
  `backup_keep` copies, default 14). **Studio -> Create -> Back up database now** makes one
  immediately.
- Files live in `workspace/`; back that folder up with your normal backup tool.
- Restore: stop the dashboard, copy a backup over `data/company.db`, delete
  `data/company.db-wal` and `-shm`, start again.

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| `doctor.py` FAIL | follow the fix text printed next to it |
| "duckduckgo is rate-limiting this PC" | wait an hour, or set `TAVILY_API_KEY` |
| AI photo failed: "memory" | another program is using the GPU (a game, `ollama run` in a terminal); close it - the company unloads its own models automatically |
| No voiceover | `python setup_assets.py --only runtime,voice` |
| A task keeps failing | open it in **Tasks**; the error and worker log are shown; retry or cancel |
| Health check | `GET http://localhost:8765/healthz` (no login, no data) |
| Logs | `logs/company.log` (rotated at 5 MB), `logs/dashboard.out` when autostarted |
