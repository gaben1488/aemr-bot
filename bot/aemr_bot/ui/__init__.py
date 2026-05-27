"""UI-слой бота: клавиатуры по доменам.

Раньше всё лежало в одном файле `aemr_bot/keyboards.py` (1392 строки).
После Cluster A (Codex PR 6) — раздроблено по 5 доменам:

- `citizen_keyboards` — экраны жителя (главное меню, воронка обращения,
  goodbye, согласие, мои обращения, useful info).
- `broadcast_keyboards` — рассылки и шаблоны (житель-отписка, оператор
  wizard, история, шаблоны).
- `operator_keyboards` — операторская панель (`op_help`, open tickets,
  audience, stats, action под карточкой обращения).
- `settings_keyboards` — `⚙️ Настройки бота` (тексты, URL, списки,
  объекты, тихий режим, PR-flow, expert).
- `wizard_keyboards` — `👥 Операторы` (список, карточка, смена роли,
  добавление wizard).

`aemr_bot.keyboards` остаётся как **compatibility facade**: re-export
всего через `from aemr_bot.ui.<module> import *`. Старые импорты
`from aemr_bot import keyboards` + `keyboards.foo()` продолжают
работать. Прямой импорт из `ui/*` модулей предпочтителен для нового
кода — короче и явнее по домену.
"""
