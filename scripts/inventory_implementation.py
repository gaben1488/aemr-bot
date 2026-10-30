"""Build a reviewable static implementation inventory; never execute bot handlers."""
from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/audits/IMPLEMENTATION_INVENTORY_2026-10.md"
SOURCE = ROOT / "bot/aemr_bot"


def location(path: Path, line: int = 1) -> str:
    name = path.relative_to(ROOT).as_posix()
    return f"`{name}:{line}`"


def main() -> None:
    commands = []
    registrations = []
    callbacks = []
    texts = []
    modules = []
    settings = []
    aliases = []
    for path in sorted(SOURCE.rglob("*.py")):
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content)
        modules.append((path, hashlib.sha256(content.encode()).hexdigest()))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = ast.unparse(node.func)
                if function == "Command":
                    commands.append((ast.unparse(node), location(path, node.lineno)))
                elif function == "CallbackRoute":
                    callbacks.append((ast.unparse(node), location(path, node.lineno)))
                elif function.startswith("dp.") or function.endswith(".register"):
                    registrations.append((function, location(path, node.lineno)))
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.isupper() and path.parent.name == "copy":
                        texts.append((target.id, location(path, node.lineno)))
                    if isinstance(target, ast.Name) and target.id == "SCHEMA" and isinstance(node.value, ast.Dict):
                        settings = [key.value for key in node.value.keys if isinstance(key, ast.Constant)]
        if path.name == "config.py":
            aliases = sorted(set(re.findall(r'alias="([A-Z0-9_]+)"', content)))
    example = ROOT / "infra/.env.example"
    example_keys = sorted(set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", example.read_text(), re.MULTILINE)))
    lines = ["# Инвентаризация реализации — 30.10.2026", "",
             ("Статический снимок исходников текущей ветки. Генератор читает все Python-модули приложения, "
             "регистрации, таблицу маршрутов, экспортируемые тексты, схему настроек, seed и пример окружения. "
             "Чтение/хеш файла означает охват инвентаризации, а не доказательство семантической корректности каждой строки."), "",
             ("Воспроизведение: `python scripts/inventory_implementation.py`. После изменения исходников обновить этот файл. "
             "Сгенерированные `docs/site/index.html` и `aemr-bot-index.md` не являются независимыми источниками истины."), "",
             "## Проверенные связи возможностей", "",
             "| Возможность | Реализация |", "| --- | --- |",
             "| Черновик обращения/дополнения, 20000/20, явная отправка | `handlers/appeal_funnel.py`, `services/appeals.py`, `ui/citizen_keyboards.py` |",
             "| Ответ 3000, предпросмотр, роль, подтверждение/unknown | `handlers/operator_draft.py`, `handlers/operator_reply.py`, `services/operator_drafts.py`, `services/operator_reply_attempts.py` |",
             "| Общая очередь, отдельные подробности, IT-восстановление | `services/queue_panel.py`, `handlers/admin_panel.py`, `handlers/admin_callback_dispatch.py` |",
             "| Расписание и снимки периодов SLA | `services/work_schedule.py`, `services/sla.py`, `services/appeals.py`, `handlers/admin_settings.py` |",
             "| Полная история, Word, XLSX | `handlers/admin_callback_dispatch.py`, `services/appeal_word.py`, `services/complete_stats.py`, `services/stats.py` |",
             "| Раздельные согласия, доказательства показанного текста | `services/consent_history.py`, `services/users.py`, `handlers/start.py` |",
             "| Удаление и восстановление, неизвестные внешние копии | `services/data_erasure.py`, `services/db_backup.py`, `scripts/replay_erasure_tombstones.py` |",
             "| Тихий режим, критические события, инциденты баз угроз | `services/admin_bus.py`, `services/quiet_hours.py`, `services/alert_state.py`, `services/threat_intel.py` |", "",
             "## Границы доказательства", "",
             ("AST не раскрывает все вычисляемые payload, getattr, monkey-patch, косвенную регистрацию или реальную последовательность событий. "
             "Команды ниже — литеральные вызовы фильтра; фактические доступы зависят от чата, активной роли, состояния и повторных проверок. "
             "Тесты контрактов маршрутов и сквозные PostgreSQL-тесты дополняют этот снимок. "
             "Получение/доставка сообщений реальным MAX, клиентские ограничения, права группы, ручная обработка внешних копий, journald, "
             "неизвестные старые копии, восстановление резервной копии и закрытый рабочий сервер требуют ручной проверки. "
             "Нет заявления о 100% охвате или exactly-once внешних действий."), "",
             "## Команды и регистрация", ""]
    for value, source in sorted(set(commands)):
        lines.append(f"- `{value}` — {source}")
    lines.extend(["", "Регистрационные вызовы (включая косвенные):", ""])
    for value, source in sorted(set(registrations)):
        lines.append(f"- `{value}` — {source}")
    lines.extend(["", "## Маршруты callback", ""])
    for value, source in callbacks:
        lines.append(f"- `{value}` — {source}")
    lines.extend(["", "## Настройки и окружение", "", f"Полный SCHEMA ({len(settings)}): " + ", ".join(f"`{v}`" for v in sorted(settings)) + ".", "",
                  "Алиасы config.py: " + ", ".join(f"`{v}`" for v in aliases) + ".", "",
                  "Пример окружения (включая закомментированные): " + ", ".join(f"`{v}`" for v in example_keys) + ".", "",
                  "Алиасы, которых нет в примере (это не утверждение обязательности): " + ", ".join(f"`{v}`" for v in sorted(set(aliases) - set(example_keys))) + ".", "",
                  "## Имена текстов бота", ""])
    for name, source in texts:
        lines.append(f"- `{name}` — {source}")
    lines.extend(["", "## Seed и статические данные", "", "Имена и SHA256; бинарные материалы не анализируются как текст.", ""])
    for path in sorted((ROOT / "seed").rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            lines.append(f"- {location(path)} — {len(data)} байт, SHA256 `{hashlib.sha256(data).hexdigest()}`")
    lines.extend(["", "## Прочитанные исходники приложения", "", f"Всего {len(modules)} Python-файлов; SHA256 каждого исходника:", ""])
    for path, digest in modules:
        lines.append(f"- {location(path)} — `{digest}`")
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
