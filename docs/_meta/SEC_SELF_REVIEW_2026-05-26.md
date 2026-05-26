# Security Self-Review: PR #79–#82
**Date:** 2026-05-26
**Scope:** Code authored by claude-code without independent review.
**Method:** Manual review of new/changed code only. No tests, no fixes.

---

### F1: [ReDoS] [low] CVSS 2.7
**Where:** `bot/aemr_bot/services/settings_store.py:40` — `_PHONE_PATTERN = re.compile(r"^[\d\s\+\-\(\)\.]{2,40}$")`
**Scenario:** Single character-class quantifier `{2,40}` over `[\d\s\+\-\(\)\.]`, anchored at both ends (`^…$`).
**Code:** `_PHONE_PATTERN.match(value.strip())` — input is operator-supplied via settings UI, max ~200 chars after strip.
**Status:** false positive — single class, no nested quantifier, bounded length. No catastrophic backtracking possible.
**Fix direction:** none.

### F2: [ReDoS] [low] CVSS 2.7
**Where:** `settings_store.py:88-91` `_URL_IN_TEXT_PATTERN = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)` and `_MD_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")`
**Scenario:** Both use `[^…]+` (negated greedy) without nested quantifier. Input is broadcast text bounded by `cfg.broadcast_max_chars` (typically 4000).
**Status:** false positive — linear regex, single greedy class.
**Fix direction:** none. Bounding by `max_chars` adds belt-and-braces.

### F3: [ReDoS] [med] CVSS 4.3
**Where:** `settings_store.py:124-125` — `re.compile(r"<\s*script[^>]*>.*?</\s*script\s*>", re.IGNORECASE | re.DOTALL)`
**Scenario:** Pattern is anchored start-end on `<script>` tag pair, uses lazy `.*?` + greedy `[^>]*`. With pathological input like `<script ` + (no `>`) + 100KB of garbage, `[^>]*` walks the whole tail. Welcome_text is bounded `max_len=4000`, so payload is structurally limited. Same applies to iframe variant.
**Status:** theoretical — exploitable only by IT-role who already controls text; `max_len=4000` caps work to ~16M ops worst-case (sub-second).
**Fix direction:** add timeout via `re2` or pre-trim suspicious markup; not urgent given the auth boundary.

### F4: [Race condition] [med] CVSS 5.4
**Where:** `bot/aemr_bot/handlers/broadcast.py:417-440` — `_pending_broadcasts[broadcast_id] = cooldown_task` then `_run_with_cooldown` calls `_pending_broadcasts.pop(broadcast_id, None)` after sleep.
**Scenario:** Operator clicks confirm → cooldown task starts → at exact moment `asyncio.sleep` returns, operator clicks "Отменить". Cancel handler does `_pending_broadcasts.pop()` → gets None (task already popped itself) → returns "уже стартовала" message. But the actual `_run_broadcast` is now executing — broadcast goes out anyway. **The race window is small but non-zero**, and the user-facing message is misleading ("уже стартовала или была отменена ранее" while it really did just start in this tick).
**Code:**
```python
# _run_with_cooldown:
await asyncio.sleep(cooldown_sec)  # ← returns
_pending_broadcasts.pop(broadcast_id, None)  # ← race window opens HERE
await _run_broadcast(...)
# _handle_cancel_cooldown (called concurrently in another task):
task = _pending_broadcasts.pop(broadcast_id, None)
if task is None: return "уже стартовала"  # ← misleading: actually starting NOW
```
**Status:** confirmed (small window, low frequency, but observable).
**Fix direction:** flip to `_active_broadcasts` flag set BEFORE pop, with explicit state machine (DRAFT→COOLDOWN→SENDING→DONE) checked atomically in DB transaction.

### F5: [Race condition / lost cancel] [low] CVSS 3.7
**Where:** `broadcast.py:443-491` — `_handle_cancel_cooldown` calls `task.cancel()` then later `await broadcasts_service.mark_cancelled(...)`.
**Scenario:** Cancel between `_pending_broadcasts.pop` and `mark_cancelled`. Cancel succeeded (`task.cancel()` works), but if `mark_cancelled` connection fails (db hiccup), broadcast row stays in DRAFT — never transitions. Reaper `reap_orphaned_sending` only handles SENDING, not DRAFT. **Stale DRAFT row remains forever**; `/broadcast list` shows it confusingly.
**Status:** confirmed.
**Fix direction:** add `reap_orphaned_draft` job, or mark DRAFT with TTL.

### F6: [Sanitization bypass — HTML entities] [med] CVSS 4.3
**Where:** `settings_store.py:121-129` `_DANGEROUS_HTML_PATTERNS` — only matches literal `<script>`, not HTML-entity-encoded `&lt;script&gt;` or `&#60;script&#62;`.
**Scenario:** IT writes welcome_text containing `&lt;script&gt;alert(1)&lt;/script&gt;`. Sanitizer does not match it (regex sees literal `&lt;`). When MAX client renders this in markdown context, **whether it decodes entities depends on MAX's renderer**. If MAX decodes HTML entities (most chat platforms do for safety/usability), payload survives sanitization.
**Status:** theoretical — exploitability hinges on MAX client behavior; needs platform testing to confirm. Plain text rendering in MAX appears not to decode entities, so likely benign here, but defense-in-depth lacking.
**Fix direction:** call `html.unescape()` before regex pass, then sanitize.

### F7: [Sanitization bypass — nested tags] [med] CVSS 4.0
**Where:** `settings_store.py:126` `re.compile(r"<\s*(script|iframe|object|embed|applet)[^>]*/?>", re.IGNORECASE)` — only self-closing/single-tag form. Combined with line 124 which is `<script…>…</script>`.
**Scenario:** Payload `<<script>script>alert(1)<</script>/script>` — outer regex matches `<script>script>...</script>/script>` but `.*?` is lazy and stops at FIRST `</script>`. After substitution, leftover text contains `<script>` (the inner one). Test: `<scr<script></script>ipt>` → first regex removes `<script></script>`, leaves `<script>` exposed.
**Code:** Three patterns run sequentially; none re-scans after substitution.
**Status:** confirmed for the nested-cloak class. Real-world exploitability depends on MAX render (see F6); for plain-text it's moot, for any markdown→html path it's a hole.
**Fix direction:** loop sanitization until fixed-point, or use HTML parser (`bleach`/`nh3`).

### F8: [Sanitization bypass — multiline markdown link] [low] CVSS 3.7
**Where:** `settings_store.py:133` `_MD_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")`
**Scenario:** `[label](\nhttps://attacker.com)` — `[^\s)]+` requires non-whitespace, so newline breaks it; pattern fails to match. The URL however still appears in plain text and would be caught by `_URL_IN_TEXT_PATTERN` only if found via `extract_urls`, but `sanitize_settings_text` does NOT call `find_non_whitelisted_urls` — it only handles md-links and dangerous schemes. **Plain `https://attacker.com` survives `sanitize_settings_text` untouched.** This is by design (settings text shouldn't have stray URLs, but if it does they're not actively rewritten). However the docstring claims "режет markdown-ссылки на не-whitelist домены" — misleading.
**Status:** confirmed gap vs. documented intent. Defense exists at outbound layer (`find_non_whitelisted_urls` in broadcast/reply), but settings → welcome shown to citizens does NOT filter plain URLs.
**Fix direction:** either rewrite plain URLs in sanitizer too, or correct the docstring + ensure consent_text/welcome_text use `find_non_whitelisted_urls` on render.

### F9: [URL whitelist bypass — Unicode homoglyph] [med] CVSS 5.3
**Where:** `settings_store.py:88` — `_URL_IN_TEXT_PATTERN = re.compile(r"https?://…")` ASCII-only.
**Scenario:** Operator (or compromised account) writes `һttps://elizovomr.evil.com` (Cyrillic `һ` U+04BB looks like `h`). Regex does NOT match — `find_non_whitelisted_urls` returns `[]` → broadcast/reply goes through. Citizen's MAX client may auto-linkify the Cyrillic-prefixed URL via IDN handling. This is a documented bypass class (used in phishing for years).
**Status:** confirmed bypass of the whitelist mechanism.
**Fix direction:** NFKC-normalize the text before regex match, then re-scan for confusables; or use IDN-aware URL extractor.

### F10: [URL whitelist bypass — newline split] [med] CVSS 4.7
**Where:** Same regex as F9. `[^\s<>\"'`]+` excludes whitespace including `\n`.
**Scenario:** `Hello https://elizovomr.ru\nhttps://attacker.com is great`. `findall` returns BOTH URLs. `find_non_whitelisted_urls` correctly identifies `attacker.com`. **So this is fine.** But: `Hello https://elizovomr.ru/page?next=https://attacker.com` — second URL is part of querystring of first. `findall` returns ONE match `https://elizovomr.ru/page?next=https://attacker.com` (no whitespace), `_is_whitelisted_url` checks `urlparse(...).hostname` = `elizovomr.ru` → ALLOWED. But MAX rendering may auto-linkify the `https://attacker.com` substring inside the URL.
**Status:** confirmed — open-redirect-style bypass via embedded URL in querystring.
**Fix direction:** also check for embedded `https?://` substring within each matched URL, or block URLs whose querystring contains another URL.

### F11: [Cron stale-operators partial list] [med] CVSS 5.7
**Where:** `bot/aemr_bot/services/cron.py:288-303` — `members = await _safe_get_chat_members(bot)`; if `not members: return` (empty=safe). But **no check for partial / paginated results**.
**Scenario:** MAX `get_chat_members` API returns first N members (typical pagination ~100). Group has 150 operators. Bot receives 100. `current_member_ids` = 100. Remaining 50 LEGITIMATE operators have `is_active=true` but NOT in `current_member_ids` → **deactivated**. Audit log records "left_admin_chat" — false. Next morning IT sees 50 missing, panics, reactivates manually.
**Code:** `result = await bot.get_chat_members(chat_id=cfg.admin_group_id)` → no `marker`/`limit` parameter, no pagination loop. `members = result.members or []`.
**Status:** confirmed if MAX API paginates. Need to verify MAX API spec — if it returns all members in one call (gov groups are small, <50 typical), risk is low. **Untested assumption.**
**Fix direction:** either confirm MAX API returns all members in one call (then add comment), or implement pagination loop, or add sanity check `if len(members) < count_active_operators(): skip`.

### F12: [Audit log JSON injection] [low] CVSS 2.0
**Where:** `operators.py:198-208` — `details={"reason": "left_admin_chat", "role": op.role, "full_name": op.full_name}`.
**Scenario:** `op.full_name` is user-supplied (via IT add wizard). asyncpg writes to JSONB via parameterized query. No string concatenation in SQL.
**Status:** false positive — asyncpg properly serializes Python dict to JSONB; no injection vector. JSON itself can't carry SQL. Worst case is bloated JSONB entries.
**Fix direction:** none. Truncate `full_name` to e.g. 200 chars for storage efficiency.

### F13: [PR-body injection — HTML comment / RTL override] [med] CVSS 5.4
**Where:** `repo_sync.py:121-145` — `_sanitize_for_pr_body` collapses `\r\n` and escapes backticks only.
**Scenario:** `full_name = "Bob <!-- approve: true --> Smith"`. After sanitize: backtick replaced, newlines collapsed, but `<!--` and `-->` PASS THROUGH. PR body becomes:
```
**Инициатор:** Bob <!-- approve: true --> Smith (max_user_id=…)
```
GitHub renders the comment as hidden text. Worse: RTL-override `‮` reverses the rest of the line — `"Bob ‮ hacker"` displays as `"Bob rekcah"` but copy-paste returns the original. Zero-width joiner `‍` invisible.
**Status:** confirmed — markdown-comment + unicode-control injection both survive.
**Fix direction:** strip `<!--`, `-->`, and any chars from Unicode category Cf (format/control); restrict to printable ASCII + Cyrillic letters/digits/whitespace.

### F14: [get_text_with_fallback overly broad except] [low] CVSS 2.5
**Where:** `settings_store.py:428-458` and `403-425` (`get_consent_request_text`) — `try: raw = await get(session, key) except Exception: raw = None` (or return fallback).
**Scenario:** `Exception` swallows `NameError`, `AttributeError`, `TypeError` — programming bugs are masked as "DB error" and fallback is silently used. Citizen sees the hardcoded text, no log line at the bug site. **Diagnosis becomes a nightmare:** "why is welcome always falling back?".
**Status:** confirmed code smell; not a vuln per se but degrades operability.
**Fix direction:** narrow to `(SQLAlchemyError, asyncio.TimeoutError)`; log at WARNING with `exc_info=True` on the rare path.

### F15: [Healthwatch BOT_TOKEN regex too permissive / too restrictive] [low] CVSS 3.1
**Where:** `scripts/healthwatch.sh:86` — `if ! [[ "$MAX_AUTH" =~ ^[A-Za-z0-9+/=._-]+$ ]]; then …`
**Scenario:** MAX bot-token format is not formally documented in this repo. The regex permits `=`/`+`/`/` (base64 chars), `.`/`_`/`-` (JWT-friendly). It does NOT permit `:`, `~`, `*`, or whitespace. **If MAX rotates token format (e.g. adds `:` separator like Telegram `1234:ABCDEF`), the alert becomes silently broken** — `exit 2` from a cron job with no notification path.
**Status:** confirmed brittleness, no immediate security harm (fails closed: alert simply doesn't fire). The regex itself is sound against injection.
**Fix direction:** log via `logger -t` AND attempt a fallback raw POST so an admin notices the format drift instead of silent failure. Also add a startup-time validation in bot itself to catch token format on deploy.

### F16: [init-letsencrypt DOMAIN/EMAIL validation] [low] CVSS 2.0
**Where:** `infra/init-letsencrypt.sh:15-22`.
**Scenario:** Regex permits valid domain/email; the script is run **manually** by sysadmin (not invoked from network). Risk surface is operator-error only. Validation is appropriate for the threat model.
**Status:** ✅ N/A — verified validation is correct; no bypass found.
**Fix direction:** none.

---

## Summary
- **Confirmed:** F4, F5, F7, F8, F9, F10, F13 (sanitization/race issues — patch in order of CVSS).
- **Theoretical (needs platform test):** F3, F6, F11.
- **False positive / N/A:** F1, F2, F12, F16.
- **Operability degradation, not vuln:** F14, F15.

**Highest priority:** F9 (Unicode homoglyph in URL whitelist) and F13 (PR-body injection) — both bypass intended security boundaries with low attacker skill.

**Categories cleared:** ReDoS (A) — single dangerous case F3, low impact; SQL injection via JSON (F) — clean.
