-- Reset test data: wipes everything except operators and editable settings.
-- Use this between test runs. NOT FOR PRODUCTION DATA.
--
-- What's kept:
--   * operators       — registered IT/coordinator/aemr/egp accounts
--   * settings        — welcome text, contacts, schedules, policy URL etc.
--                       (incl. cached policy_pdf_token so PRIVACY.pdf
--                       doesn't re-upload on next start)
--
-- What's wiped:
--   * users           — all citizens, their phones, names, consent timestamps
--   * appeals         — all submitted appeals
--   * messages        — citizen↔operator message history
--   * events          — idempotency dedupe log
--   * audit_log       — operator action history
--   * broadcasts      — past broadcast metadata
--   * broadcast_deliveries — per-recipient delivery records
--
-- After running this, the bot starts «as if no citizen ever interacted with
-- it» but operators stay registered and settings stay in place. The
-- bootstrap_it_from_env path won't re-fire because the IT operator row
-- survives.
--
-- Usage from project root:
--   PowerShell:
--     Get-Content scripts\reset_test_data.sql | docker compose -f infra\docker-compose.yml exec -T db psql -U aemr -d aemr
--   Git Bash:
--     cat scripts/reset_test_data.sql | docker compose -f infra/docker-compose.yml exec -T db psql -U aemr -d aemr
--
-- (The `-T` disables the pseudo-TTY so stdin is honored. Without it psql
-- reads nothing.)

BEGIN;

TRUNCATE
    broadcast_deliveries,
    broadcasts,
    messages,
    appeals,
    events,
    audit_log,
    users
RESTART IDENTITY CASCADE;

COMMIT;

-- Show what remains so the operator sees the result.
SELECT 'operators' AS table, count(*) AS rows FROM operators
UNION ALL SELECT 'settings', count(*) FROM settings
UNION ALL SELECT 'users', count(*) FROM users
UNION ALL SELECT 'appeals', count(*) FROM appeals
UNION ALL SELECT 'messages', count(*) FROM messages
UNION ALL SELECT 'broadcasts', count(*) FROM broadcasts
UNION ALL SELECT 'broadcast_deliveries', count(*) FROM broadcast_deliveries
UNION ALL SELECT 'audit_log', count(*) FROM audit_log
UNION ALL SELECT 'events', count(*) FROM events
ORDER BY 1;
