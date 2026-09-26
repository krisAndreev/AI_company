# Orchestrator context (maintained by Claude - keep under ~150 lines)

Last updated: 2026-09-26

Mode: planning   <!-- autonomous | planning (see ROLE.md 1b) -->

## Now: plan
<!-- planning mode: goal, then numbered tasks with [x] done / [ ] open / (skipped) -->
Goal (2026-09-26): take Digital Product 1 (Family Digital Detox Planner) to a live listing.
Owner approved steps 1-7 on 2026-09-26 (do all; owner publishes step 8 themself). Shop name: KrokiCrafts.
  [x] 1 review all 26 pages  [x] 2 competitor memo (research asset_50a23962a040)  [ ] 3 v2 brief + rebuild
  [ ] 4 new mockups/listing images  [ ] 5 listing text  [ ] 6 campaign pack update
  [ ] 7 final QA + handover checklist
  Owner OK'd tool changes + v2 (2026-09-26). Tool changes done + committed; v2 build next.  (5b shop About/policies/banner: proposed, not approved) Platform = Etsy; owner created the shop 2026-09-26 (setup in progress, owner-only).

## Now: projects
<!-- one line each: id | name | status | stage | next step | waiting for -->
- proj_5846365e8996 | Digital Product 1 (Family Digital Detox Planner) | PAUSED | stage 2 | fix product + listing, then owner publishes | owner: platform, account, approvals

## Now: waiting for the owner
<!-- reviews, decisions, accounts, money, publishing -->
- (none)

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
- 2026-09-26: product_builder: one-page fit (fit_blocks), tidy_blocks, markdown/URL stripping,
  cards + certificate blocks, optional fixed page_plan, tag quote cleanup; grid mockup centres
  short row; brand_name=KrokiCrafts; max_content_pages 12->14. test_phase16/17/18 --offline pass.
- 2026-09-26: owner switched to planning mode. Reviewed DP1: 26p PDF A4+Letter, 3 mockups, listing.md, campaign pack (23 DRAFT assets). Gaps: listing tag has stray quote, no AI disclosure / what's-included, generic sections (monthly calendar under Intro header), grid mockup has empty slot, no competitor price check.
- 2026-09-26: orchestrator role set up. No project work done yet.
