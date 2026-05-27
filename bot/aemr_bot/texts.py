"""Compatibility facade для текстов бота.

После Cluster B (Codex PR 6 part 2, 2026-05-28) монолитный
`texts.py` (949 строк, 90+ констант) раздроблен по 5 доменным
модулям в `aemr_bot/copy/`. Здесь — re-export всех публичных имён
для обратной совместимости с существующими импортами вида:

    from aemr_bot import texts
    texts.WELCOME
    texts.ADMIN_CARD_TEMPLATE

Прямой импорт из copy/* модулей предпочтителен для нового кода:

    from aemr_bot.copy.citizen_funnel import WELCOME
    from aemr_bot.copy.admin_texts import ADMIN_CARD_TEMPLATE
    from aemr_bot.copy.security_texts import SECURITY_INFO_TEXT

См. `aemr_bot/copy/__init__.py` для карты доменов.
"""
# noqa: F401,F403 на каждом импорте — это намеренные re-export'ы для
# обратной совместимости старых сайтов `from aemr_bot import texts`.
from aemr_bot.copy.admin_texts import *  # noqa: F401, F403
from aemr_bot.copy.broadcast_texts import *  # noqa: F401, F403
from aemr_bot.copy.citizen_funnel import *  # noqa: F401, F403
from aemr_bot.copy.errors_texts import *  # noqa: F401, F403
from aemr_bot.copy.security_texts import *  # noqa: F401, F403
