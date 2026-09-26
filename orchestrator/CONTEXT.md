# Orchestrator context (maintained by Claude - keep under ~150 lines)

Last updated: 2026-09-26

Mode: autonomous   <!-- autonomous | planning (see ROLE.md 1b) -->

## Now: plan
<!-- planning mode: goal, then numbered tasks with [x] done / [ ] open / (skipped) -->
- (no active plan)

## Now: projects
<!-- one line each: id | name | status | stage | next step | waiting for -->
- (none tracked yet - read `c.chat.snapshot()` at the next session start and fill this in)

## Now: waiting for the owner
<!-- reviews, decisions, accounts, money, publishing -->
- (none)

## Now: blocked / capability gaps
<!-- what cannot be done well yet, evidence, proposed fix, owner decision if any -->
- (none known yet)

## Owner decisions and preferences
- 2026-09-26: Claude (this role) is the orchestrator, run from a Claude Code session in the
  project folder. Hard rules: nothing illegal, never change budgets, owner confirms vital
  steps (publishing, accounts, money, contacting people, installs). Otherwise act
  autonomously: try first, review own work, improve tools, then hand over for review.
- Save tokens: local Ollama models do bulk generation; Claude plans and reviews.
- Fast checks only: `--offline` tests + one targeted live call.

## Lessons learned
<!-- proven only; "when X, do Y, because Z (evidence)" -->
- (none yet)

## Changes made to the system
<!-- YYYY-MM-DD: what changed, why, test that passed -->
- 2026-09-26: created `orchestrator/ROLE.md` and this file.
- 2026-09-26: added planning mode (ROLE.md 1b): plan first, then one task per owner approval.

## Log (last ~2 weeks; older -> HISTORY.md)
- 2026-09-26: orchestrator role set up. No project work done yet.
