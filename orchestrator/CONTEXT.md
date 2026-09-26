# Orchestrator context (maintained by Claude - keep under ~150 lines)

Last updated: 2026-09-26

Mode: autonomous   <!-- autonomous | planning (see ROLE.md 1b) -->

## Now: plan
<!-- planning mode: goal, then numbered tasks with [x] done / [ ] open / (skipped) -->
Goal (2026-09-26): tennis digital product 2 - DONE (researched, built, live, marketed).
  [x] 1-5 research (3 pockets /25), court-diagram renderer, product, 7 listing images, checklist
  [x] 6 owner read the PDF and published on Etsy: https://www.etsy.com/listing/4583127470
  [x] 7 Pinterest board 'Tennis Doubles Tips': pin 1 live, pin 4 scheduled Oct 7 20:00 (Claude);
      pins 2 (Sep 29) + 3 (Oct 3) finished by the owner (owner said "all done") - verify in
      profile > Created > Scheduled Pins at the next session.
Goal (2026-09-26, owner): find a niche for teaching people to create AI e-books / digital products.
  [x] 1 project proj_0e1849bb6696 'Digital Product 3 - AI creator guide' (EUR 5, PAUSED = Claude runs it)
  [x] 2 research: winner 'Family story book with AI' 21/25 (teachers 17, coaches 15; generic
      AI-ebook/PLR dropped) -> `notes/2026-09-26_research-3-pockets.md` (asset_72d5cd396d12)
  [ ] 3 owner confirms winner + format; then build kit (Claude writes pages), images, checklist
- Also due: measure DP1 ~2026-10-03 and DP2 ~2026-10-10 (Etsy Stats).

## Now: projects
<!-- one line each: id | name | status | stage | next step | waiting for -->
- proj_5846365e8996 | Digital Product 1 = '7-Day Family Screen Reset Kit' | PAUSED | stage 2 |
  LIVE on Etsy https://www.etsy.com/listing/4583030705/7-day-family-screen-time-reset-kit
  (EUR 4.99, prod_19ed8c70f03e, files `workspace/projects/2026-09-25_digital-product-1_5846365e8996/
  products/7-day-family-screen-reset-kit/v1_2026-09-26/`, checklist asset_bca50267857e).
  Campaign camp_b75217d9f6fb (`campaigns/screen-reset-kit-launch/v1_2026-09-26/`): Pinterest used
  (board 'Screen Time Ideas for Families', matched pin images `*-pinterest-pin-d9f6fb-2.png`);
  IG/FB posts ready but owner has no accounts. | measure | owner: Etsy Stats
- proj_c002d70421e8 | Digital Product 2 - Tennis = 'Tennis Doubles Playbook' | PAUSED (Claude runs it) | LIVE on Etsy https://www.etsy.com/listing/4583127470 (EUR 6.99) |
  v1 built prod_fe6023bde4d2, files `workspace/projects/2026-09-26_digital-product-2-tennis_c002d70421e8/
  products/tennis-doubles-playbook/v1_2026-09-26/` (A4+Letter PDFs, zip, listing.md, 3 mockups);
  research `notes/2026-09-26_research-3-pockets.md`; 7 listing images + checklist; pins in
  `campaigns/doubles-playbook-pinterest/v1_2026-09-26/` (PINS.md) | measure ~2026-10-10 | owner: Etsy Stats

## Now: waiting for the owner
<!-- reviews, decisions, accounts, money, publishing -->
- DP3: confirm niche 'Family story book with AI' (interview kit + AI prompts + book template).
- ~2026-10-10: DP2 Etsy Stats (visits from Pinterest, favourites, sales). Reject old DP2 hero
  `listing-01-hero.jpg` (replaced by `listing-01-hero-square.jpg`) in the dashboard.
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
- Listing images are made by one-off scratch scripts (DP1 and DP2, not in git) -> a reusable
  `listing_images` step in product_builder (headline + pages + close-up) would save a session.
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
- 2026-09-26: ALWAYS research with the 3-profitable-pockets method (/25, one winner) - enforced in
  code (`core/research.py:RESEARCH_METHOD`) + ROLE.md 2b. Product 2 niche was sports/tennis.

## Lessons learned
<!-- proven only; "when X, do Y, because Z (evidence)" -->
- Etsy search cards no longer show review counts; in-page fetch('/search?q=') + DOMParser gives cards/prices/Bestseller badges fast; Amazon fetch('/s?k=..&i=stripbooks') gives rating counts; review text is not in fetched HTML.
- Research reading: Reddit is blocked for WebFetch AND the Chrome extension; Bing shows a bot check
  (never bypass). Working sources: Etsy + Amazon search pages in Chrome (JS extract of titles/prices/
  rating counts), Talk Tennis threads via WebFetch (`https://tt.tennis-warehouse.com/index.php?threads/<slug>.<id>/`).
- Pinterest pins built from the product's own renderer (headline + matched court diagram + 3 real
  tips + product footer) beat campaign_builder's rotated visuals: no image/headline mismatch.
- Etsy categories: digital sports guides fit 'Racquet Sports' (physical or digital) + Sport type.
- Etsy supply is not demand: the only adult wall-practice listing had 0 sales; Amazon rating counts
  (e.g. rec doubles books 276/295 ratings) are better proof of "already paid to solve".
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
- Etsy thumbnails are square crops of photo 1: keep the hero's key content in the centre square
  (DP2 hero v1 lost its title). Tags: type slowly (wait ~0.5 s before/after Enter) or letters get lost.
  Etsy asks 'How is this digital content created?' -> 'With an AI generator' when text is AI-drafted.
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
- DP2 listing said "diagrams on every card" and the hero said "15 cards with diagrams" (only 9
  pages have diagrams) -> check every number/claim in listings and images against the file.
- DP2 hero was designed 4:3 only; Etsy's square thumbnail cut the title -> keep hero key content
  in the centre square (x 300-2100 of 2400).
- Typed 13 Etsy tags without pauses -> 9 came out garbled -> wait ~0.5 s around each Enter and
  verify the tag list (aria-label 'Delete tag ...') before moving on.
- Clicked by screenshot coordinates after the page layout shifted (item type stayed Physical,
  quantity stayed empty) -> use element refs (find) for form fields, re-check values after.
- Pressed Escape after Pinterest 'Schedule' -> it closed the confirm dialog, so nothing was
  scheduled -> after Schedule, look for the confirm dialog and click its Schedule button.
- Bulk JS clicks on re-rendering lists (tag delete) only removed one -> click one per step, re-query.
- First handover had an unfixed image/headline mismatch (mentioned, not fixed) -> fix
  cheap quality issues before handing over instead of listing them as caveats.

## Changes made to the system
<!-- YYYY-MM-DD: what changed, why, test that passed -->
- 2026-09-26: owner's research method forced everywhere: `RESEARCH_METHOD` in chat RULES, research
  workers (department research or research capabilities; also tool-param prompts) and web_research
  (`ResearchReport.pockets`, code totals /25 + picks winner, uncited pockets dropped, table in the
  report). ROLE.md 2b + CLAUDE.md. All offline suites pass (16: +pocket asserts, 19: +rule check).
- 2026-09-26: product_builder `court` block (tennis diagrams: us/them/ball/shot/move/zone/note,
  1-3 panels, validated by `parse_court`, shrinkable by fit_blocks); fixed guide contents page drawn
  over the cover + blank page 2 (`fresh_page`); tools.json max_content_pages 14 -> 16.
  test_phase16 (+ court/contents test) and test_phase18 --offline pass.
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
- 2026-09-26: owner switched to autonomous mode; DP3 project + niche research (family story book with AI 21/25).
- 2026-09-26: DP2 LIVE on Etsy (EUR 6.99, title shortened to 84 chars, AI generator disclosed);
  Pinterest board 'Tennis Doubles Tips' with 4 pins (1 live, 3 scheduled Sep 29/Oct 3/Oct 7).
- 2026-09-26: DP2 tennis: research (doubles 22/25, nerves 19, solo/wall 18), Tennis Doubles Playbook
  v1 built (15 content pages, court diagrams), reviewed + fixed (TOC over cover, small diagrams,
  empty half pages, lobs page overflow). 7 listing images + publish checklist made and reviewed.
- 2026-09-26: DP1 finished: v2 kit written by Claude (16p), 7 listing images, listing text,
  checklist; owner published on Etsy; Pinterest pin 1 live, pins 2-4 scheduled Oct 1/5/9.
- 2026-09-26: planning mode; reviewed DP1 v1 (26p generic planner) and competitors.
- 2026-09-26: orchestrator role set up.
