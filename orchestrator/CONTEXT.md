# Orchestrator context (maintained by Claude - keep under ~150 lines)

Last updated: 2026-09-26

Mode: planning   <!-- autonomous | planning (see ROLE.md 1b) -->

## Now: plan
<!-- planning mode: goal, then numbered tasks with [x] done / [ ] open / (skipped) -->
Goal (2026-09-26): take Digital Product 1 to a live Etsy listing (shop KrokiCrafts).
  [x] 1 review  [x] 2 competitor memo  [x] 3 v2 built (Claude-written, prod_19ed8c70f03e)
  [x] 4 listing images (5 new + cover/tablet mockups)  [x] 5 listing text (in listing.md, EUR 4.99)
  [x] 6 campaign camp_b75217d9f6fb (12 posts pin/ig/fb, Claude-written)  [x] 7 checklist asset_bca50267857e
  [x] 8 OWNER published on Etsy 2026-09-26: https://www.etsy.com/listing/4583030705/7-day-family-screen-time-reset-kit
  [ ] 9 campaign camp_b75217d9f6fb: Pinterest only (owner account KrokiCrafts, board 'Screen Time
      Ideas for Families'). Pin 1 published 2026-09-26 (owner OK). Pins 2-4 proposed scheduled
      Oct 1/5/9 via 'Publish at a later date', each owner-confirmed. IG/FB: no accounts yet. (5b shop About/policies/banner: offered, not approved)

## Now: projects
<!-- one line each: id | name | status | stage | next step | waiting for -->
- proj_5846365e8996 | Digital Product 1 -> v2 '7-Day Family Screen Reset Kit' | PAUSED | stage 2 | owner reviews + publishes | owner

## Now: waiting for the owner
<!-- reviews, decisions, accounts, money, publishing -->
- 2026-09-26: OK to schedule pins 2-4 (Oct 1/5/9)?
- 2026-09-26: review v2 drafts (7-Day kit) + reject v1 drafts (Family Digital Detox Planner +
  its campaign, 6 old campaign images); read PDF, then publish on Etsy via PUBLISH-CHECKLIST.md;
  send listing URL. Campaign dates are suggestions (shift to go-live).

## Now: blocked / capability gaps
<!-- what cannot be done well yet, evidence, proposed fix, owner decision if any -->
- qwen3/gemma page writing hit Ollama 'token repeat limit' (2026-09-26) -> retry+fallback added.
- test_phase19 'refused action' test fails on Windows console (UnicodeEncodeError on the warning
  sign char); pre-existing, not yet fixed.

## Owner decisions and preferences
- 2026-09-26: Claude (this role) is the orchestrator, run from a Claude Code session in the
  project folder. Hard rules: nothing illegal, never change budgets, owner confirms vital
  steps (publishing, accounts, money, contacting people, installs). Otherwise act
  autonomously: try first, review own work, improve tools, then hand over for review.
- 2026-09-26: sales platform = Etsy. Owner created the shop and sets up the account/payments/tax themselves.
- 2026-09-26: owner: Claude may write product content itself (more reliable than the 4B model);
  pass it as `written_pages` + `listing` to product_builder.
- Save tokens: local Ollama models do bulk generation; Claude plans and reviews.
- Fast checks only: `--offline` tests + one targeted live call.

## Lessons learned
- 2026-09-26: Pinterest via Chrome: pin-creation-tool, file_upload on the file input; topic
  suggestions are poor (skip); toggle 'Mark as AI-Modified' on; extension promo popup after publish (Esc).
<!-- proven only; "when X, do Y, because Z (evidence)" -->
- 2026-09-26: product_builder v1 flaws (DP1 review): pages overflow -> spill page padded with Notes
  (12 planned -> 24 pages); markdown (*x*, [u](u)) printed raw; model invents URLs; tracker used
  for minutes (tick boxes only); page title repeated as heading; brand empty -> "(c) the seller".
- 2026-09-26: competitors (Etsy, family screen time) sell KITS: 7-day challenge, family tech
  contract, reward chart/coupons, activity cards, certificate, weekly tracker (~12 pages).
  Our v1 = parent journal with text walls. Etsy prices not readable via tool (robots).

## Changes made to the system
<!-- YYYY-MM-DD: what changed, why, test that passed -->
- 2026-09-26: created `orchestrator/ROLE.md` and this file.
- 2026-09-26: added planning mode (ROLE.md 1b): plan first, then one task per owner approval.
- 2026-09-26: project put in git and pushed to GitHub (private, `main`); ROLE.md 4 has the
  commit/push rule.

## Log (last ~2 weeks; older -> HISTORY.md)
- 2026-09-26: DP1 v2 built from Claude-written pages (16p) + 5 listing images + campaign; campaign
  tool got written-content option; social images: CTA overlap fixed, visuals rotate. Tests 16/17/18 pass.
- 2026-09-26: product_builder: one-page fit (fit_blocks), tidy_blocks, markdown/URL stripping,
  cards + certificate blocks, optional fixed page_plan, tag quote cleanup; grid mockup centres
  short row; brand_name=KrokiCrafts; max_content_pages 12->14. test_phase16/17/18 --offline pass.
- 2026-09-26: owner switched to planning mode. Reviewed DP1: 26p PDF A4+Letter, 3 mockups, listing.md, campaign pack (23 DRAFT assets). Gaps: listing tag has stray quote, no AI disclosure / what's-included, generic sections (monthly calendar under Intro header), grid mockup has empty slot, no competitor price check.
- 2026-09-26: orchestrator role set up. No project work done yet.
