# Reliability release 0.2.0 Implementation Plan

> **For agentic workers:** Use focused implementation tasks under dispatching-parallel-agents; integrate and review the complete branch before release.

**Goal:** Close F01–F13 from the execution-path audit and publish a tested consolidated release.
**Architecture:** Persist inbound events before acknowledging transport progress; recover only work known not to have started and explicitly surface uncertain outcomes. Preserve existing operator/citizen workflows; tighten state ordering, cancellation and deployment recovery.
**Tech Stack:** Python 3.12, maxapi 1.2.3, SQLAlchemy/PostgreSQL, APScheduler, Docker Compose.
**Spec:** docs/audits/FLOW_AUDIT_2026-10-26.md plus user's instruction to release a fully corrected version.

## Global Constraints

- Preserve existing functionality and current maxapi 1.2.3 integration.
- Backups remain optional and quiet when disabled. Threat feeds signal once per outage.
- Do not blindly repeat an external send after an uncertain outcome.
- No real messages or production database mutations during testing.
- Ship only after regression suite, migration/schema checks, security/lint/type checks and branch review.
- User authorizes release; no further plan approval needed. Actual VPS access is not available.

## Review Focus

- Process death between durable acceptance and handler dispatch must leave recoverable work.
- Process death during external sends must not cause silent replay or a false success claim.
- Concurrent transitions must not resurrect cleared drafts or split locks.
- Cancellation and privacy erasure must apply to pending work as well as current in-memory state.
- A failed deployment must be retried even when Git HEAD already equals remote.

## Task 1: inbound transport and supported webhook (F01/F08)

Files: new services/inbox.py and transport module, db/models.py, migration 0026, main.py, handlers/__init__.py, idempotency.py; tests/test_inbox*.py and webhook tests.
Interface: accept raw batch durably before advancing marker; pending worker dispatches supported SDK models. Done payload is cleared; uncertain/failed execution is recorded and signalled once. Webhook uses canonical secret header and durable acceptance, returns non-2xx on DB failure.
- [ ] Write failing tests for durable acceptance, marker preservation, recovery and webhook auth/failure.
- [ ] Implement persistence and bounded worker, integrate startup/shutdown and cleanup.
- [ ] Run focused tests and commit.

## Task 2: appeal locks and initial-card recovery (F02/F03)

Files: handlers/appeal_runtime.py, services/admin_card.py, new focused tests. Root integrates recovery scheduling.
Interface: get_user_lock/drop_user_lock retain identity while holders/waiters reference it; recover_unpublished_appeals(bot) finds saved open appeals lacking first card, loads relationships and republishes safely.
- [ ] Convert lock failure into regression test, add first-card recovery tests.
- [ ] Implement bounded recovery and concurrency protection.
- [ ] Run appeal/card tests and commit only owned files.

## Task 3: broadcast delivery accounting and cancellation (F04/F07)

Files: handlers/broadcast.py, services/broadcasts.py, broadcast tests.
Interface: cancellation checked before each new send; acknowledgement distinguishes requested/completed. Delivery accounting failures never yield normal DONE and never silently resume uncertain sends. Persist intent before sending if necessary using existing delivery ledger.
- [ ] Reproduce failed journal and pre-existing cancellation with desired expectations.
- [ ] Implement durable accounting/failure semantics and truthful progress.
- [ ] Run broadcast tests and commit owned files.

## Task 4: persistence ordering and settings export race (F06/F11)

Files: services/wizard_registry.py, services/wizard_persist.py if needed, handlers/admin_settings_pr.py, services/settings_store.py, focused tests.
Interface: ordered writes per wizard key; mark_synced only unchanged exported snapshots. Snapshot runtime settings by value before awaiting network.
- [ ] Reproduce delayed SAVE after DELETE and change during PR creation.
- [ ] Implement order and compare-and-set sync marking.
- [ ] Run targeted tests and commit owned files.

## Task 5: deployment and backup process budgets (F09/F10)

Files: scripts/auto-deploy.sh, services/backup_verify.py, targeted tests.
Interface: complete decrypt/read/wait bounded and child reaped on timeout/cancel; failed build retries same revision or restores source HEAD without pretending deploy succeeded.
- [ ] Adapt audit reproductions to desired behavior.
- [ ] Implement and run shellcheck/backup/deploy tests; commit owned files.

## Task 6: cron, data scope, documentation and release (F05/F12/F13)

Files: services/cron.py, copy/citizen_funnel.py, lifecycle docs, roadmap/contracts; pyproject/lock/version/release notes.
- [ ] Test retention prewarning runs when no records overdue; run independently of cleanup failures.
- [ ] Integrate card recovery and inbox housekeeping.
- [ ] Accurately describe DB erasure and copies in MAX; no unsolicited destructive history cleanup.
- [ ] Update stale documentation, mark audit probes historical, version 0.2.0 and changelog.
- [ ] Full regression/security/schema checks, independent review, green CI.
- [ ] Merge consolidated release, supersede old PRs, tag/publish if tools permit; report exact shipped state.

## Execution record

Initial branch base: c283d4e (includes #297/#298). origin/main remains aac8a79. No implementation changes in main yet.

### Review and verification record

- Tasks 1–5 implemented; task 6 integration and local verification complete, CI/publication pending.
- Independent review of c283d4e..9937432: three Important findings (cancellation after global pacing; restoration of old deploy controller; erasure/active receipt race). All reproduced before fix and covered by test_release_review_regressions.py, test_auto_deploy.py, test_inbox_recovery.py.
- Review Minor (outdated admin_card helper doc) corrected. No findings parked.
- Previous integrated baseline: 2508 passed, 147 PostgreSQL-dependent skipped; mypy132 clean; lock offline check clean. Final rerun after review follows.
- Decision: unknown external effects are quarantined and surfaced, never blindly replayed; MAX provides no application idempotency key.
- Decision: erasure describes and enforces bot DB scope; prior external MAX messages are retained under their separate lifecycle.
