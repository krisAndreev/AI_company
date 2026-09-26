# Orchestrator context (maintained by Claude - keep under ~150 lines)

Last updated: 2026-09-26

Mode: planning   <!-- autonomous | planning (see ROLE.md 1b) -->

## Now: plan
<!-- planning mode: goal, then numbered tasks with [x] done / [ ] open / (skipped) -->
Goal (2026-09-26): take Digital Product 1 to a live Etsy listing - DONE.
  [x] 1-7 review, competitor memo, v2 built, listing images, listing text, campaign, checklist
  [x] 8 owner published on Etsy 2026-09-26
  [x] 9 Pinterest: pin 1 live 2026-09-26; pins 2-4 scheduled Oct 1/5/9 2026 20:00
  (5b shop About/policies/banner: offered, not approved)
- No active plan. Suggested next: measure DP1 (~2026-10-03), then product 2.

## Now: projects
<!-- one line each: id | name | status | stage | next step | waiting for -->
- proj_5846365e8996 | Digital Product 1 = '7-Day Family Screen Reset Kit' | PAUSED | stage 2 |
  LIVE on Etsy https://www.etsy.com/listing/4583030705/7-day-family-screen-time-reset-kit
  (EUR 4.99, prod_19ed8c70f03e, files `workspace/projects/2026-09-25_digital-product-1_5846365e8996/
  products/7-day-family-screen-reset-kit/v1_2026-09-26/`, checklist asset_bca50267857e).
  Campaign camp_b75217d9f6fb (`campaigns/screen-reset-kit-launch/v1_2026-09-26/`): Pinterest used
  (board 'Screen Time Ideas for Families', matched pin images `*-pinterest-pin-d9f6fb-2.png`);
  IG/FB posts ready but owner has no accounts. | measure | owner: Etsy Stats

## Now: waiting for the owner
<!-- reviews, decisions, accounts, money, publishing -->
- Reject old v1 drafts (Family Digital Detox Planner product + its campaign + 6 old images) and
  approve the v2 kit drafts in the dashboard (only the owner can decide assets).
- ~2026-10-03: Etsy Stats (visits from Pinterest, favourites, sales) -> decide next steps.
- Optional: shop About/policies/banner/icon (5b); Instagram/Facebook accounts if wanted.

## Now: blocked / capability gaps
<!-- what cannot be done well yet, evidence, proposed fix, owner decision if any -->
- Local 4B models write generic product text and can crash in repeat loops (Ollama 'token
  repeat limit', 2026-09-26) -> retry+fallback added; sellable text is written by Claude.
- No publishing integration: posting = owner's Chrome via claude-in-chrome + owner OK.
- Etsy prices/stats not readable by tools (robots, no ETSY_API_KEY) -> owner checks by eye.
- campaign_builder picks post visuals by rotation, not by meaning -> check every image against
  its headline; a per-post visual choice would fix it (not built yet).
- test_phase19 'refused action' test fails on the Windows console (UnicodeEncodeError on the
  warning-sign char); pre-existing, not yet fixed.

## Owner decisions and preferences
- 2026-09-26: Claude (this role) is the orchestrator, run from a Claude Code session in the
  project folder. Hard rules: nothing illegal, never change budgets, owner confirms vital
  steps (publishing, accounts, money, contacting people, installs).
- 2026-09-26: sales platform = Etsy, shop **KrokiCrafts** (brand_name set; crocodile mascot
  "Kroki"; owner is a developer). Owner creates/verifies accounts and payments themselves.
- 2026-09-26: Claude writes sellable content itself (product pages, listing, posts) and passes
  it as finished content (`written_pages`/`listing`, `strategy`/`written_posts`); local models
  for background work only.
- 2026-09-26: marketing on Pinterest first; the owner likes batched confirmations (fill all,
  one OK) and clicked Schedule themselves.
- Fast checks only: `--offline` tests + one targeted live call.

## Lessons learned
<!-- proven only; "when X, do Y, because Z (evidence)" -->
- Etsy printables (family niche) sell as KITS: challenge days, family contract, reward
  coupons, activity cards, certificate, tracker. A parent journal with text walls is weak (DP1 v1).
- Before registering real drafts, trial-render in `Company(":memory:")` and look at contact
  sheets of ALL pages/images; register only the good version (kept DP1 drafts clean).
- Look at every page, not only previews: DP1 v1 problems (overflow pages padded with Notes,
  12 planned -> 26 pages; raw markdown; invented URLs; minutes in tick boxes) were on pages
  7-26, which the 6 previews never showed.
- Check each marketing image against its headline: rotation put the agreement page on the
  "Activity Cards" pin; caught only before scheduling. Script fonts (Pacifico) draw below
  their line box -> leave clear space above buttons.
- Honest AI disclosure everywhere: Etsy description line + Pinterest 'Mark as AI-Modified'.
  The listing says "every page was reviewed by hand" -> the owner must read the PDF first.
- Pinterest via Chrome: `/pin-creation-tool/`, `file_upload` on the file input, drafts keep
  the schedule (date typed MM/DD/YYYY, then click the day; time via dropdown menuitem),
  topic suggestions are useless (skip), extension promo popup after publish (Esc).
  Scheduled pins: profile > Created > "Scheduled Pins" (only visible to the owner).
- Etsy form (2026): title 140 chars, 13 tags <= 20 chars, type Digital files (upload PDFs,
  not the zip), renewal Manual; strip tracking params from listing URLs before sharing.

## Mistakes made (and the rule that prevents them)
- web_research refused: sent 4 queries, max is 3 -> read a tool's Params schema before the
  first call (ROLE.md 4 already says so).
- Scratch scripts outside the repo failed with `No module named 'core'` -> run them with
  `PYTHONPATH=.` from the project root.
- `import test_phase16` to debug started the whole suite incl. the LIVE part -> never import
  test files; call the code directly.
- Wrote a test with 2 tags where the schema needs >= 5 -> check field limits before writing
  test data.
- A heredoc script read `sys.argv[1]` and got the scratch path as the theme -> use env vars
  or explicit flags for script options.
- Spent ~25 min on a local-model product build that crashed -> for sellable text, write it
  directly (owner decision) and keep the 4B models for background jobs.
- First handover had an unfixed image/headline mismatch (mentioned, not fixed) -> fix
  cheap quality issues before handing over instead of listing them as caveats.

## Changes made to the system
<!-- YYYY-MM-DD: what changed, why, test that passed -->
- 2026-09-26: `orchestrator/ROLE.md` + this file; planning mode (ROLE 1b); git + GitHub push.
- 2026-09-26: product_builder: one-page fit (fit_blocks), tidy_blocks, markdown/URL stripping,
  cards + certificate blocks, fixed `page_plan`, retry-then-fallback on model errors,
  `CallParams = WrittenProduct` (finished `written_pages` + `listing`); max_content_pages 14.
- 2026-09-26: campaign_builder `CallParams = WrittenCampaign` (finished strategy/posts/emails/
  blog, still checked against channel limits); social images: clear space above CTA, posts
  rotate product visuals (`ImageParams.visual`); tools.call validates `CallParams` if present.
- 2026-09-26: workspace layout for the owner: `projects/<date>_<name>_<id>/` + INDEX.md,
  products/campaigns in `<title>/v<N>_<date>/` (same title = next version). Folder rules:
  ROLE.md section 4 "Files and folders". Tests 16-19 --offline pass.

## Log (last ~2 weeks; older -> HISTORY.md)
- 2026-09-26: DP1 finished: v2 kit written by Claude (16p), 7 listing images, listing text,
  checklist; owner published on Etsy; Pinterest pin 1 live, pins 2-4 scheduled Oct 1/5/9.
- 2026-09-26: planning mode; reviewed DP1 v1 (26p generic planner) and competitors.
- 2026-09-26: orchestrator role set up.
