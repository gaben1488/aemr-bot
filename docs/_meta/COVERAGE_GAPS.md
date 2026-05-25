# Test Coverage Gap Analysis

Generated: 2026-05-25
Source: `bot/coverage.json` (pytest-cov 7.14.0, branch coverage on)
Total tests: 964 passed, 97 skipped (21.79s)

## Топ-уровневая сводка

| метрика                 | значение           |
| ----------------------- | ------------------ |
| **Overall coverage**    | **61.1%** (combined line+branch) |
| Line coverage           | 64.4% (5062/7865 statements) |
| Branch coverage         | 50.1% (1162/2318 branches)   |
| Missing lines           | 2803               |
| Missing branches        | 1156               |
| Partial branches        | 246                |
| Files at 100%           | 12 из 60           |
| Files в зоне ≥85%       | 25 из 60           |
| Файлы под рисками <50%  | 13 из 60           |

12 модулей уже на 100%: `aemr_bot/__init__.py`, `db/models.py`,
`handlers/_common.py`, `services/progress.py`, `services/repo_sync.py`,
`texts.py`, `utils/__init__.py`, `utils/background.py`,
`utils/image_attachments.py`, `utils/menu_tracker.py`,
`db/__init__.py`, `services/__init__.py`.

## Топ-10 модулей с самыми опасными пробелами

Сортировка: `score = miss_lines * 2 + miss_branches` (line-misses весят
больше, потому что обычно представляют целые непроверенные функции, а
не отдельные ветки уже покрытой логики).

| #  | Module                                    | Cov%   | Brc%   | Stmts | Miss | BrMiss | Score |
| -- | ----------------------------------------- | ------ | ------ | ----- | ---- | ------ | ----- |
| 1  | `handlers/admin_settings.py`              | 15.2%  | 9.6%   | 465   | 384  | 170    | 938   |
| 2  | `handlers/broadcast_templates.py`         | 30.3%  | 23.0%  | 527   | 353  | 151    | 857   |
| 3  | `handlers/admin_operators.py`             | 38.7%  | 35.4%  | 424   | 255  | 93     | 603   |
| 4  | `handlers/admin_commands.py`              | 15.1%  | 0.0%   | 229   | 183  | 76     | 442   |
| 5  | `keyboards.py`                            | 68.4%  | 46.2%  | 520   | 141  | 56     | 338   |
| 6  | `handlers/appeal_funnel.py`               | 59.0%  | 44.3%  | 258   | 93   | 49     | 235   |
| 7  | `main.py`                                 | 39.2%  | 22.2%  | 173   | 99   | 28     | 226   |
| 8  | `services/users.py`                       | 27.3%  | 10.0%  | 142   | 98   | 27     | 223   |
| 9  | `handlers/admin_panel.py`                 | 45.9%  | 37.0%  | 174   | 90   | 29     | 209   |
| 10 | `handlers/operator_reply.py`              | 66.4%  | 51.9%  | 276   | 77   | 52     | 206   |

Дальше за топ-10 (значимые, но меньше):
- `services/cron.py` 64.6% (84/28)
- `services/appeals.py` 25.7% (80/24)
- `services/broadcast_templates.py` 19.5% (71/24)
- `handlers/start.py` 62.1% (62/29)
- `services/settings_store.py` 50.5% (57/34)
- `handlers/appeal_runtime.py` 29.3% (59/23)

## Per-module breakdown

### 1. `handlers/admin_settings.py` — 465 stmts, 15.2% covered

**Что покрыто:** только импорты и shape-функции (`_clip_audit_value`,
`_intent_set`/`_intent_get`/`_intent_drop`, `_render_value`) — это
test_admin_settings_audit.py (57 строк, аудит-clip-логика).

**🔴 Bug-prone gaps (P0 для написания тестов):**

- L115-145: `run_settings_menu` — главное меню «⚙️ Настройки бота».
  Ни одного теста на роль-проверку (`ensure_role(IT)`), на отображение
  `dirty_count`, на «Не выгружено в репо: N (preview)».
- L175-289: `_route_set_action` — 16-way диспетчер `op:set:*` callback'ов.
  Каждая ветка (`cat:texts`, `cat:urls`, `text:`, `url:`, `edit:`,
  `cancel:`, `list:`, `list_add:`, `list_del:`, `obj:`, `obj_view:`,
  `obj_add:`, `obj_del:`, `author`, `pr:start`, `pr:confirm`,
  `pr:diff`) — отдельная неоттестированная ветка.
- L297-330: `_show_text_card` — рендер карточки текста/URL.
  Не тестируется `is_url` ветка с подсказкой про http://, ни лимит
  `max_len`, ни обрезание длинного `_render_value`.
- L333-358: `_start_edit_intent` — guard «ключа нет в SCHEMA», ветка
  `rule.get("url")` vs `rule.get("type") is str`.
- L368-431: list-CRUD (`_show_list_card`, `_list_delete`,
  обработка `list_add` через intent) — пустой список, удаление
  индексом, защита от out-of-range.
- L441-587: object-CRUD для emergency_contacts /
  transport_dispatcher_contacts — ни одна ветка не покрыта.
- L582-587: `_show_author_card` — рендер commit_author настроек.
- L609-906: PR-flow (`_show_pr_confirm`, `_show_pr_diff`,
  `_create_pr`) — критичный путь для IT, ноль тестов; включает
  ошибки git/gh, отсутствие dirty keys, проверку cooldown.
- L879-984: `handle_settings_text_input` — перехват входящего
  текстового сообщения как нового значения настройки. Включает:
  - guard «нет активного intent» (возврат `False`);
  - валидация типа (`int`, `str`, `dict` через json.loads);
  - URL-валидация (http/https + опциональный whitelist из SEC #4);
  - audit-log запись с `_clip_audit_value`;
  - GC просроченных intent'ов.

**🟡 Integration-only:** нет — это handler-модуль, integration
покрытие через `make_event` _именно есть_ в `_helpers.py` и должно
быть применено. Все «integration-only» гэпы здесь = ленивые гэпы.

**🟢 Trivial:** нет.

**Suggested tests (priority order):**
1. `test_run_settings_menu_role_check` — non-IT → ensure_role
   reject, ни одного `send_or_edit_screen` вызова.
2. `test_run_settings_menu_dirty_keys_preview` — `dirty=[]` vs
   `dirty=[k1..k7]` (preview обрезается на 5+ «и ещё N»).
3. `test_route_set_action_each_branch` — параметризованный 17-way
   тест: каждый payload-префикс вызывает свой helper.
4. `test_show_text_card_url_vs_text` — `is_url=True` добавляет
   подсказку, `max_len` присутствует/отсутствует.
5. `test_start_edit_intent_unknown_key` — key not in SCHEMA →
   красная карточка, intent НЕ ставится.
6. `test_handle_settings_text_input_*` (8 тестов):
   - no intent → return False;
   - expired intent → return False + intent dropped;
   - valid str с max_len превышен → reject;
   - valid url прошёл whitelist (если включён);
   - invalid url → reject «должно начинаться с https://»;
   - valid list_add → append + intent dropped;
   - valid obj_add (json) → парсинг + сохранение;
   - audit_log: details содержит clipped before/after.
7. `test_create_pr_*` — mock `services.repo_sync.create_pr`,
   проверить cooldown, dirty=[], ошибка git.

Расчёт: ~30 unit-тестов, ожидаемый прирост покрытия модуля 15% → 80%+.

### 2. `handlers/broadcast_templates.py` — 527 stmts, 30.3% covered

**Что покрыто (test_broadcast_templates_handlers.py, 444 строки):**
часть `_list`, `_open` happy-path, `_apply` dedupe-флоу. Wizard
ввод имени/текста — мало.

**🔴 Bug-prone gaps:**

- L143-178: `_list` ветки `not items` (empty), `items` rendering.
  Покрыты, но не все frame'ы кнопок.
- L215-304: `_apply` — детали render preview (citation footer),
  guard `actor_id is None`, ветка active-rate-limit-warning.
- L314-321: `_drop_expired` GC.
- L332-381: `_rename` flow — все ветки wizard'а на rename.
- L401-506: `_edit` flow — replace text + сохранить images.
- L518-548: `_delete` flow — confirm + apply.
- L579-651: wizard ввод (name → text → images → save).
  Каждая ветка проверки длины, отмены, шага-переключения — отдельный
  тест.
- L893-1230 (~340 строк): admin-side handlers для template-attachments
  (загрузка картинок, переотправка превью, отмена). Целиком без
  тестов.

**🟡 Integration-only:** L518->520, L821->833 (callback dispatching
веток в `register(dp)`) — покрываются интеграционно через
test_callback_router.

**🟢 Trivial:** нет.

**Suggested tests:**
1. `test_apply_double_tap_dedupe` — два apply в 3-сек окно: второй
   ack без record_usage (проверить `_is_recent_apply`).
2. `test_wizard_state_lifecycle` — `_TmplWizardState.expired()`
   через monotonic-mock.
3. `test_rename_*` (3): no-op (то же имя), conflict (другой
   шаблон с тем же именем), success.
4. `test_edit_text_*` (4): cancel, превышение длины, valid,
   images preservation.
5. `test_delete_confirm_*` (3): cancel, success, race (шаблон
   уже удалён другим оператором).
6. `test_wizard_image_collect_*` (5): single image, max images
   reached, document вместо image, отмена, save без изображений.
7. `test_admin_image_dedupe` — обработка дублей в обработчике
   incoming-message.

Расчёт: ~25 unit-тестов, прирост 30% → 75%+.

### 3. `handlers/admin_operators.py` — 424 stmts, 38.7% covered

**Что покрыто (test_admin_operators.py, 431 строка):** базовый wizard
add operator, role change, deactivate. Не покрыты: from-group picker,
карточка с защитой «единственный IT», hydrate-from-DB ветки.

**🔴 Bug-prone gaps:**

- L92-99: `_safe_get_chat_members` — exception → log + пустой
  список. Без теста — silent breakage если MAX API изменит формат.
- L105-109: `_full_name_from_member` — все 4 ветки (first+last,
  только first, только last, пусто → "User N").
- L147, 152, 157, 162, 167, 170, 175: `run_operators_action` —
  каждый `payload.startswith(...)` без теста на «эта ветка
  действительно зовёт правильный helper».
- L189-199: `op:opadd:pick:` — `ValueError` на нечисловой
  picked_user_id (ловится, но без теста).
- L218-222: ветки name_keep / name_edit / edit_role.
- L249: header «активных N, деактивированных M».
- L272-306: `_show_operator_card`:
  - target_id ValueError → silent ack;
  - оператор не найден → красная карточка;
  - `can_deactivate=False` для единственного активного IT
    (SEC-критично, защищает от self-lockout);
  - `is_self` маркер «себя через меню изменить нельзя».
- L364-428: `_show_role_change` — picker и applied-role.
- L445-480: `_apply_role_change` — happy + race (роль уже та же)
  + операция от non-IT.
- L496-585: `_show_deactivate_confirm`, `_apply_deactivate` —
  включая SEC #6 race-protection (operator deactivation race).
- L599-695: from-group picker (`_show_from_group`,
  `_start_add_with_picked`).
- L764, 794-845: wizard ввода имени для добавляемого оператора —
  каждый шаг.
- L861, 883-906: hydrate ветки при старте.

**🟡 Integration-only:** L979->991 (register_handlers).

**🟢 Trivial:** L991 (final return).

**Suggested tests:**
1. `test_safe_get_chat_members_*` (3): no `members` attr, exception,
   happy.
2. `test_full_name_from_member_*` (4): все комбинации first/last.
3. `test_run_operators_action_dispatch` — параметризован по 9
   префиксам.
4. `test_show_operator_card_last_it_blocks_deactivate` — критично
   для SEC #6.
5. `test_show_operator_card_self_marker` — `is_self=True` показывает
   warning «Это вы».
6. `test_apply_deactivate_race` — между _show_confirm и _apply
   IT уже единственный → reject.
7. `test_show_from_group_filters_existing_operators` — picker не
   показывает уже-операторов.
8. `test_wizard_name_edit_*` (3): cancel, valid, too-long.

Расчёт: ~22 unit-теста, прирост 38% → 78%+.

### 4. `handlers/admin_commands.py` — 229 stmts, 15.1% covered (branch 0%)

**Что покрыто:** только импорты модуля.

**🔴 Bug-prone gaps:**

Каждая slash-команда — отдельная функция в `register(dp)`:
`/open_tickets`, `/stats`, `/reply`, `/reopen`, `/close`, `/erase`,
`/forget`, `/whoami`, `/diag`, `/diag_extended`, `/diag_csv`.

- L143-145: `cmd_open_tickets` — ensure_operator reject.
- L149-160: `cmd_stats` — argument parsing, VALID_PERIODS check.
- L164-195: `cmd_reply` — двухслойная защита (SEC #9), parse-arg,
  ValueError на нечисловом id, пустой текст.
- L199-224: `cmd_reopen` — argument parse, audit-log запись.
- L228-247: `cmd_close` — analogous.
- L254-312: `cmd_erase` — phone vs id, не-IT reject (только IT
  может erase), confirmation step.
- L316-361: `cmd_forget` — отзыв согласия + auto-close открытых
  обращений (P1 в #21).
- L365-379: `cmd_whoami` — простой.
- L388-453: `cmd_diag` / `cmd_diag_extended` / `cmd_diag_csv` —
  частично покрыт через test_diag_extended.py, но не через
  message-handler register.

**🟡 Integration-only:** все handler-функции через `register(dp)` —
требуют make_event + decorated handler shape.

**🟢 Trivial:** нет.

**Suggested tests:**
1. `test_cmd_reply_*` (5): not-admin-chat, not-operator,
   missing args, invalid id, empty text.
2. `test_cmd_reopen_*` (4): not-operator, ValueError,
   already_open, blocked_by_revoke.
3. `test_cmd_close_*` (4): аналогично reopen.
4. `test_cmd_erase_*` (6): not-IT, phone/id parsing, confirmation
   required, not-found.
5. `test_cmd_forget_*` (3): not-self, success, already-revoked.
6. `test_cmd_stats_*` (4): invalid period, default today, all,
   no-data case.
7. `test_cmd_diag*_register_path` — через make_event с
   /diag команды.

Расчёт: ~26 unit-тестов, прирост 15% → 85%+.

### 5. `keyboards.py` — 520 stmts, 68.4% covered

**🟡 Integration-only:** в основном — это keyboard-builders, которые
покрываются интеграционно при рендере карточек. Большая часть
missing lines — это специализированные клавиатуры, которые не
вызываются из тестируемых путей.

**🔴 Bug-prone gaps:**

- L168, 175, 204-207: `_attach_back_row` / навигационные guards.
- L496-501, 557-583: keyboards для admin_settings меню (вызывается
  только из untested handlers).
- L799-849: broadcast_templates keyboards — тоже из untested.
- L854-1061: целая серия специализированных клавиатур (op_operators_*,
  op_settings_*, broadcast_templates_*).

Здесь повышение покрытия = автоматическое следствие написания
тестов для пунктов 1-4 выше.

**Suggested tests:** 4 микро-юнит-теста для нетривиальных билдеров:
1. `test_op_operators_list_keyboard_inactive_marker`.
2. `test_broadcast_templates_card_keyboard_used_flag`.
3. `test_op_settings_menu_dirty_count`.
4. `test_op_audience_paginator_edge` (last page, single page).

### 6. `handlers/appeal_funnel.py` — 258 stmts, 59.0% covered

**🔴 Bug-prone gaps:**

- L93-146: `_handle_repeat_intent` — повторное обращение по
  ANSWERED/CLOSED, маркировка темы (P1 critical: цепочка
  «обратная связь по отвеченному вопросу»).
- L186-220: ветка «новый житель без consent» — onboarding-fork.
- L397-403: validation для phone format в funnel.
- L417-448: топик-suggestions при пустом вводе.
- L493-510: address-step с пустым ответом.
- L623-651: followup detached-safety (P2 #24).
- L691-721: edge: пользователь отписался во время funnel.

**Suggested tests:** ~12 тестов.

### 7. `main.py` — 173 stmts, 39.2% covered

**🟢 Mostly trivial / integration-only:** L62-64 (sys.argv detect),
L82-83 (logger config), L113 (env-var fallback), L134-170 (CLI args
parsing для preflight), L205-206 (bot info logging), L247-372 (async
main entry — оркестратор).

Это — `async def main()` оркестратор. Распилить на тестируемые блоки:
- `_preflight_check_token` (уже есть mock в тестах).
- `_register_bot_commands`, `_seed_settings`, bootstrap_it_from_env,
  reap_orphaned_sending, hydrate.

**Suggested tests:** ~6 тестов на extracted helpers, остальное —
законно untested orchestrator.

### 8. `services/users.py` — 142 stmts, 27.3% covered

**🔴 Bug-prone gaps (P0 — это data layer для residents):**

- L42-46: `get_or_create` — путь когда user is None (создание).
- L59-60: `has_consent` — bool conversion edge.
- L74: `set_consent` — SEC #1 защита: НЕ сбрасывать is_blocked.
  Без теста — следующий рефакторинг может сломать.
- L106-109: `set_state` с `data` patch (data is None vs dict).
- L133-149: `update_dialog_data` — advisory_xact_lock + race
  protection (postgres path; SQLite fallback покрыт неявно).
- L141-149: SQLite fallback ветка (advisory недоступен).
- L164-176: `find_stuck_in_summary` — limit default cfg
  fallback.
- L228, 247-270: `find_stuck_in_funnel` (аналогично).
- L282-378: `set_blocked`, `auto_close_open_appeals` (P1 #21),
  `revoke_consent` — каждый со своими guards.
- L401-485: `erase_*` функции (anonymise) — критично для 152-ФЗ:
  каждое поле должно стираться, audit-log писаться.
- L530-630: `find_by_phone`, `format_resident_card`,
  `count_consent_*`.

**🟡 Integration-only:** advisory_xact_lock paths требуют PG.

**🟢 Trivial:** нет.

**Suggested tests:** ~18 unit-тестов на чистом SQLite:
1. `test_get_or_create_*` (2).
2. `test_set_consent_does_not_reset_blocked` — SEC #1 защита.
3. `test_update_dialog_data_*` (3): user is None, patch merge,
   advisory unavailable fallback.
4. `test_find_stuck_in_*` (4).
5. `test_set_blocked_unblocked_*` (2): blocks new appeals,
   unblock allows.
6. `test_revoke_consent_auto_close` (P1 #21).
7. `test_erase_clears_all_pii` — phone, first_name,
   phone_normalized.
8. `test_find_by_phone_normalization` (3 формата ввода).

Расчёт: прирост 27% → 88%+.

### 9. `handlers/admin_panel.py` — 174 stmts, 45.9% covered

**🔴 Bug-prone gaps:**

- L29: ensure_role role-check.
- L147-207: render главного меню админ-панели — каждая роль (IT,
  COORDINATOR, OPERATOR) видит разные кнопки.
- L225-449: цикл callback-handler'ов для main menu (большой
  switch).
- L500-506: cleanup intent ветки.

**Suggested tests:** ~10 тестов, основной — три параметризованных
test_admin_panel_role_keyboard (IT vs COORDINATOR vs OPERATOR).

### 10. `handlers/operator_reply.py` — 276 stmts, 66.4% covered

**🔴 Bug-prone gaps:**

- L143-147: ack_callback early exit.
- L176, 183-185, 189-191: reply intent dedupe (race rapid
  double-tap, P2 #22).
- L226: marker spoof guard (SEC #3 🆔 №N).
- L374: edge case в parse marker.
- L409-421: handle_command_reply branch.
- L515-528: intermediate reply close-warning (P2 #23).
- L645-757: swipe-reply handler — большой блок с обработкой
  forwarded message detection.
- L800-804: пользователь больше не оператор (deactivated mid-flow).

**Suggested tests:** ~12 тестов, P0 = SEC #3 (marker spoof) и
P2 #22 (rapid double-tap).

## Service-layer summary (за топ-10)

| Module                       | Cov%  | P0 для тестирования                                |
| ---------------------------- | ----- | -------------------------------------------------- |
| `services/cron.py`           | 64.6% | Failure-injection: scheduler не падает на исключении одного джоба |
| `services/appeals.py`        | 25.7% | `create_appeal`, `add_user_message`, `add_operator_message` (intermediate vs final, CLOSED не «оживается»), `mark_in_progress`, `reopen`, `close`, `count_recent_for_user`, `list_unanswered_with_messages` |
| `services/broadcast_templates.py` | 19.5% | CRUD service: list_active, get_by_id, create, rename (conflict), record_usage, soft_delete |
| `services/settings_store.py` | 50.5% | get/set с schema validation, dirty-tracking, list-keys, audit-events на изменении |
| `services/operators.py`      | 22.6% | get_any, upsert (insert vs reactivate), deactivate (active vs missing), change_role, write_audit, count_active_by_role, bootstrap_it_from_env |
| `services/broadcasts.py`     | 37.5% | reap_orphaned_sending, lifecycle FSM (PENDING → SENDING → SENT/FAILED), progress-update transactions |
| `services/stats.py`          | 47.1% | period-bounds (today / week / month / quarter / half_year / year / all), xlsx export shape |
| `services/admin_relay.py`    | 55.9% | retry-on-throttle, fallback handlers when admin_card not deliverable |

## Категоризация — глобально

| Категория            | Доля missing | Действие                              |
| -------------------- | ------------ | ------------------------------------- |
| 🔴 **Bug-prone**     | ~55%         | Покрывать unit-тестами через make_event |
| 🟡 **Integration**   | ~25%         | Покрывать integration-тестами с in-memory SQLite + mock bot |
| 🟢 **Trivial / unreachable** | ~10% | Оставить (TYPE_CHECKING, `if __name__`, defensive guards) |
| ⚪ **Hot path uncovered** | ~10%   | SEC #1/#3/#6, P1 #19/#21, P2 #22/#23, SACRED #5 — обязательно покрыть |

## Реалистичная цель

100% unit-coverage невозможен из-за:
- `main.py:main()` — async orchestrator, требует моков bot+scheduler+
  dispatcher+health одновременно.
- `services/db_backup.py` — pg_dump/gpg/rclone subprocesses (75% уже
  достигнуто за счёт мокирования subprocess.run).
- `services/cron.py` — APScheduler integration (64.6%).
- `handlers/start.py` — MAX-bot rich content events требуют интеграции
  с реальной полировкой sender.
- `services/users.py:update_dialog_data` advisory_xact_lock ветка
  работает только на Postgres.
- `services/idempotency.py` jsonb_set хирургия — Postgres-only.
- `services/admin_relay.py` retry-on-throttle с реальным backoff.

**Реалистичная цель:** **88-92% line, 78-83% branch.** Это
интеграционно достижимо за 4-6 PR.

## План работы (PR-by-PR)

Эстимейты — в часах на одного сениор-разработчика, знающего
test_admin_handlers_small.py-style паттерн make_event.

| PR | Файл                              | Тестов | Effort | Прирост модуля   | Прирост total |
| -- | --------------------------------- | ------ | ------ | ---------------- | ------------- |
| A  | `handlers/admin_settings.py`      | +30    | 24h    | 15% → 80%        | +4.0%         |
| B  | `services/users.py` + `services/appeals.py` | +28 | 16h | 27%/26% → 88%/85% | +3.2%        |
| C  | `handlers/admin_commands.py`      | +26    | 16h    | 15% → 85%        | +2.3%         |
| D  | `handlers/admin_operators.py`     | +22    | 14h    | 38% → 78%        | +2.0%         |
| E  | `handlers/broadcast_templates.py` + `services/broadcast_templates.py` | +30 | 20h | 30%/19% → 75%/82% | +3.4%        |
| F  | `handlers/operator_reply.py` (SEC #3 + P2 #22/#23) + `handlers/appeal_funnel.py` (P1 + P2 #24) | +20 | 14h | 66%/59% → 88%/85% | +2.1%       |
| G  | `services/operators.py` + `services/settings_store.py` + `services/broadcasts.py` + `services/stats.py` | +24 | 12h | соответственно → 85%+ | +2.5%      |
| H  | `handlers/admin_panel.py` + `handlers/appeal_runtime.py` + `keyboards.py` (только нетривиальные builders) | +18 | 10h | → 80%+ | +1.5%       |

**Итого:** 8 PR, ~198 новых тестов, ~126h работы → ожидаемый
total coverage **61.1% → ~82-83%** (line ~88%, branch ~80%).

**Оставшиеся 8-10% gap** требуют:
- настройку Postgres-fixture (testcontainers / pgvector-mini) для
  advisory_xact_lock, jsonb_set, idempotency, db_backup;
- моки aiohttp-сессии для MAX-API integration в start.py и
  admin_relay.py;
- e2e сценарии для cron.py scheduler-flow (4-6h).

Это +2 PR (I, J) ещё на ~30h → доводит до **90%+ line / 83%+ branch**.

## Source data

- `bot/coverage.json` — структурированный output (695 KB).
- `bot/_cov_analyze.py` — helper для ranking и per-file inspection
  (создан в ходе анализа; полезен для повторных прогонов).
