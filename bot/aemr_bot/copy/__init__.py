"""Тексты бота по доменам.

После Cluster B (Codex PR 6 part 2) монолитный `aemr_bot/texts.py`
(949 строк) разнесён по 5 доменным модулям:

- `citizen_funnel` — экраны жителя (welcome, воронка, согласие,
  goodbye, useful info, help, рассылки-подтверждения).
- `admin_texts` — операторские (карточка обращения, ответы,
  /op_help, команды-результаты).
- `broadcast_texts` — wizard рассылок и шаблоны.
- `security_texts` — антифишинг (SECURITY_INFO_TEXT),
  OP_HELP_SECURITY, RULES.
- `errors_texts` — короткие generic сообщения (отмены, unknown).

`aemr_bot.texts` остаётся как **compatibility facade**: re-export
всего через `from aemr_bot.copy.<module> import *`. Старые
импорты `from aemr_bot import texts` + `texts.WELCOME` продолжают
работать. Прямой импорт из `copy/*` модулей предпочтителен для
нового кода.
"""
