"""Compatibility facade для клавиатур.

После Cluster A (Codex PR 6, 2026-05-27) монолитный keyboards.py
(1392 строки, 70+ функций) разнесён по 5 доменным модулям в
`aemr_bot/ui/`. Здесь — re-export всех публичных имён для обратной
совместимости с существующими импортами вида:

    from aemr_bot import keyboards
    keyboards.main_menu(...)
    keyboards.appeal_admin_actions(...)

Прямой импорт из ui/* модулей предпочтителен для нового кода:

    from aemr_bot.ui.citizen_keyboards import main_menu
    from aemr_bot.ui.operator_keyboards import appeal_admin_actions

См. `aemr_bot/ui/__init__.py` для карты доменов.
"""

# обратной совместимости старых сайтов `from aemr_bot import keyboards`.
from aemr_bot.ui.broadcast_keyboards import *
from aemr_bot.ui.citizen_keyboards import *
from aemr_bot.ui.operator_keyboards import *
from aemr_bot.ui.settings_keyboards import *
from aemr_bot.ui.wizard_keyboards import *
