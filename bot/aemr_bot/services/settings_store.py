import json
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.config import settings as cfg
from aemr_bot.db.models import Setting

# SEC #4: whitelist хостов для URL-настроек. Operator-facing botов
# (citizens click trusted govbot link) — должны вести только на
# официальные ресурсы. Rogue/compromised IT не сможет поставить
# phishing URL.
#
# Подвиды доменов добавляются ниже — точное совпадение или suffix
# `.elizovomr.ru` / `.kamgov.ru` / `.gosuslugi.ru`. Если нужно
# временно разрешить новый домен — добавить сюда и редеплоить
# (не правится через UI, чтобы не выстрелить себе в ногу).
_URL_HOST_WHITELIST_SUFFIXES = (
    "elizovomr.ru",
    "kamgov.ru",
    "gosuslugi.ru",
    "kamchatka.gov.ru",
)


def _is_whitelisted_url(value: str) -> bool:
    """True если URL ведёт на разрешённый host (Elizovo / Kamchatka gov)."""
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _URL_HOST_WHITELIST_SUFFIXES
    )

DEFAULTS: dict[str, Any] = {
    "welcome_text": None,
    "consent_text": None,
    # Автор коммитов от бота для services/repo_sync. Подставляется в
    # GitHub API при создании PR. Меняется через меню «👤 Автор
    # коммитов» в админ-панели — без редеплоя.
    "commit_author_name": None,
    "commit_author_email": None,
    "policy_url": (
        "https://elizovomr.ru/storage/attachments/2024/08/15/U9XfgiWRETCF0KKT.pdf"
    ),
    "electronic_reception_url": "https://kamgov.ru/questions",
    "udth_schedule_url": (
        "https://udth.elizovomr.ru/publikatsiia/raspisanie-prigorodnykh-avtobusov"
    ),
    "udth_schedule_intermunicipal_url": (
        "https://kamgov.ru/mintrans/current_activities/"
        "raspisania-dvizenia-passazirskogo-avtomobilnogo-transporta-"
        "mezmunicipalnogo-soobsenia-v-kamcatskom-krae"
    ),
    "appointment_text": (
        "Приём граждан временно исполняющим полномочия Главы Елизовского "
        "муниципального района А.С. Гончаровым осуществляется два раза в месяц "
        "(1 и 3 среда каждого месяца) по предварительной записи. "
        "Запись на приём ведётся по номеру телефона 8 (415-31) 7-25-29."
    ),
    "emergency_contacts": [],
    "transport_dispatcher_contacts": [],
    "topics": [],
    # Глобальный лимит «сколько картинок оператор может приложить к
    # одной рассылке». Раньше был в env BROADCAST_MAX_IMAGES; перенесли
    # сюда для оперативной правки IT-оператором через меню «⚙️ Настройки
    # бота» без редеплоя. 5 — баланс «афиша + 3-4 фото» vs нагрузка на
    # канал MAX (каждая картинка ×N подписчиков). Допустимый диапазон
    # 1–20 (см. SCHEMA).
    "broadcast_max_images": 5,
    "localities": [
        "Елизовское ГП",
        "Вулканное ГП",
        "Корякское СП",
        "Начикинское СП",
        "Николаевское СП",
        "Новоавачинское СП",
        "Новолесновское СП",
        "Паратунское СП",
        "Пионерское СП",
        "Раздольненское СП",
    ],
}

# Белый список ключей, которые можно править, с допустимыми Python-типами и
# дополнительными правилами. /setting <key> <value> отклоняет всё, чего нет в
# этой карте.
SCHEMA: dict[str, dict] = {
    "welcome_text": {"type": str, "min_len": 1, "max_len": 4000},
    "consent_text": {"type": str, "min_len": 1, "max_len": 4000},
    "commit_author_name": {"type": str, "min_len": 1, "max_len": 120},
    "commit_author_email": {"type": str, "min_len": 3, "max_len": 200},
    "policy_url": {"type": str, "url": True},
    "electronic_reception_url": {"type": str, "url": True},
    "udth_schedule_url": {"type": str, "url": True},
    "udth_schedule_intermunicipal_url": {"type": str, "url": True},
    "appointment_text": {"type": str, "min_len": 1, "max_len": 2000},
    "emergency_contacts": {"type": list, "min_items": 1, "item_keys": {"name", "phone"}},
    "transport_dispatcher_contacts": {
        "type": list,
        "min_items": 1,
        "item_keys": {"routes", "phone"},
    },
    "topics": {"type": list, "min_items": 1, "max_items": 30, "item_type": str},
    # Глобальный лимит картинок в рассылке. Диапазон 1–20: 1 — минимум
    # для «текст + одна афиша», 20 — практический потолок (выше MAX
    # ограничивает частоту, см. _send_one).
    "broadcast_max_images": {"type": int, "min": 1, "max": 20},
    "localities": {"type": list, "min_items": 1, "max_items": 30, "item_type": str},
}


def validate(key: str, value: Any) -> tuple[bool, str]:
    """Возвращает (ok, message). В сообщении — причина при ошибке или 'ok' при успехе."""
    if key not in SCHEMA:
        return False, f"Unknown key '{key}'. Allowed: {sorted(SCHEMA)}"
    rule = SCHEMA[key]
    expected = rule["type"]
    if not isinstance(value, expected):
        return False, f"Expected type {expected.__name__}, got {type(value).__name__}"
    if expected is str:
        if "min_len" in rule and len(value) < rule["min_len"]:
            return False, f"String too short, min_len={rule['min_len']}"
        if "max_len" in rule and len(value) > rule["max_len"]:
            return False, f"String too long, max_len={rule['max_len']}"
        if rule.get("url"):
            if not (value.startswith("https://") or value.startswith("http://")):
                return False, "URL must start with http:// or https://"
            if not _is_whitelisted_url(value):
                return False, (
                    f"URL host не в whitelist. Разрешены только официальные "
                    f"ресурсы: {', '.join(_URL_HOST_WHITELIST_SUFFIXES)}. "
                    f"Для нового домена обратитесь к разработчику."
                )
    if expected is list:
        if "min_items" in rule and len(value) < rule["min_items"]:
            return False, f"List too short, min_items={rule['min_items']}"
        if "max_items" in rule and len(value) > rule["max_items"]:
            return False, f"List too long, max_items={rule['max_items']}"
        if "item_type" in rule and not all(isinstance(it, rule["item_type"]) for it in value):
            return False, f"All items must be {rule['item_type'].__name__}"
        if "item_keys" in rule:
            for it in value:
                if not isinstance(it, dict) or not rule["item_keys"].issubset(it):
                    return False, f"Each item must be an object with keys: {rule['item_keys']}"
    if expected is int:
        # bool — подкласс int в Python, явно фильтруем: True/False не
        # должны проходить как int (validate("broadcast_max_images", True)
        # = no-go).
        if isinstance(value, bool):
            return False, "Expected int, got bool"
        if "min" in rule and value < rule["min"]:
            return False, f"Integer too small, min={rule['min']}"
        if "max" in rule and value > rule["max"]:
            return False, f"Integer too large, max={rule['max']}"
    return True, "ok"


def format_obj_list(items: list[dict]) -> str:
    """Чистая функция рендера тела карточки списка объектов
    (emergency_contacts, transport_dispatcher_contacts).

    Если у item'ов есть «section» (актуально для emergency_contacts —
    Экстренные службы / Электроэнергия / Отопление / Холодная вода) —
    группируем визуально. Item'ы без section падают в «Прочее».
    Порядок секций — по первому появлению, чтобы совпадал с порядком
    в seed/contacts.json и не прыгал между рендерами. Глобальная
    нумерация (1..N) сохраняется — она совпадает с idx в obj_item
    card, чтобы клик на «5» открывал ровно пятый контакт.

    Если секция всего одна (особенно «Прочее») — заголовок не
    добавляем, остаётся плоский список как раньше (для transport-
    диспетчеров, у которых section не используется).

    Лежит в services/, а не в handlers/, чтобы юнит-тест мог импортить
    функцию без подтягивания maxapi через handlers/__init__.py.
    """
    if not items:
        return "(список пуст)"

    groups: dict[str, list[tuple[int, dict]]] = {}
    order: list[str] = []
    for i, item in enumerate(items):
        section = (item.get("section") or "").strip() or "Прочее"
        if section not in groups:
            groups[section] = []
            order.append(section)
        groups[section].append((i, item))

    lines: list[str] = []
    show_headers = len(order) > 1
    for section in order:
        if show_headers:
            lines.append(f"\n▸ {section}")
        for i, item in groups[section]:
            name = item.get("name") or item.get("routes") or "?"
            phone = item.get("phone") or ""
            lines.append(f"{i+1}. {name} — {phone}")
    return "\n".join(lines).lstrip("\n")


async def get(session: AsyncSession, key: str) -> Any:
    row = await session.scalar(select(Setting).where(Setting.key == key))
    if row is not None:
        return row.value
    return DEFAULTS.get(key)


async def set_value(session: AsyncSession, key: str, value: Any) -> None:
    stmt = (
        pg_insert(Setting)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    )
    await session.execute(stmt)


async def list_keys(session: AsyncSession) -> list[str]:
    rows = await session.scalars(select(Setting.key))
    in_db = set(rows)
    return sorted(in_db.union(DEFAULTS.keys()))


# Ключи, которые попадают в seed/runtime_config.json при синхронизации с
# репозиторием. Намеренно НЕ включаем commit_author_* — это серверная
# метаинформация, в репо не место. welcome_text/consent_text идут не
# сюда, а в seed/welcome.md и seed/consent.md (формат markdown).
SYNCED_KEYS: tuple[str, ...] = (
    "policy_url",
    "electronic_reception_url",
    "udth_schedule_url",
    "udth_schedule_intermunicipal_url",
    "appointment_text",
    "emergency_contacts",
    "transport_dispatcher_contacts",
    "topics",
    "localities",
)


async def get_dirty_keys(session: AsyncSession) -> list[str]:
    """Список ключей из SYNCED_KEYS, изменённых после последней
    синхронизации с репо. Используется в меню для индикатора «N
    несинхронизированных изменений»."""
    rows = await session.execute(
        select(Setting.key, Setting.updated_at, Setting.synced_at).where(
            Setting.key.in_(SYNCED_KEYS)
        )
    )
    dirty: list[str] = []
    for key, updated_at, synced_at in rows.all():
        if synced_at is None or (updated_at is not None and updated_at > synced_at):
            dirty.append(key)
    return sorted(dirty)


async def export_synced(session: AsyncSession) -> dict[str, Any]:
    """Собирает значения SYNCED_KEYS из БД с fallback на DEFAULTS.
    Возвращает dict с детерминированным порядком ключей для чистых
    diff'ов в git."""
    out: dict[str, Any] = {}
    for key in SYNCED_KEYS:
        out[key] = await get(session, key)
    return out


async def mark_synced(
    session: AsyncSession, keys: list[str] | None = None
) -> int:
    """Проставить synced_at = now() для ключей из списка (или для всех
    SYNCED_KEYS, если keys=None). Вызывается после успешного создания
    PR. Возвращает количество обновлённых строк."""
    from datetime import datetime, timezone
    from sqlalchemy import update as sa_update

    target_keys = list(keys) if keys is not None else list(SYNCED_KEYS)
    now = datetime.now(timezone.utc)
    result = await session.execute(
        sa_update(Setting)
        .where(Setting.key.in_(target_keys))
        .values(synced_at=now)
    )
    return result.rowcount or 0


def _read_seed_json(name: str) -> Any:
    path = cfg.seed_dir / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_seed_text(name: str) -> str | None:
    path = cfg.seed_dir / name
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


async def seed_if_empty(session: AsyncSession) -> None:
    """Заполнить настройки из /seed только для отсутствующих ключей.

    После вставки сразу помечает свежие SYNCED_KEYS как уже
    синхронизированные с репо (synced_at = now()). Логика: seed-файлы
    (`seed/contacts.json`, `seed/topics.json`, `seed/transport_dispatchers.json`)
    физически лежат в репозитории и уже являются baseline'ом, поэтому
    сразу после первого старта бота этим ключам не место в списке
    «несинхронизированных изменений». Иначе индикатор «3 грязных ключа»
    горит вечно у каждого свежеустановленного бота — это раздражает
    оператора и сбивает с толку: настоящих изменений нет, а UI кричит.
    """
    existing = set(await session.scalars(select(Setting.key)))

    seed_pairs: dict[str, Any] = {}
    if (topics := _read_seed_json("topics.json")) is not None:
        seed_pairs["topics"] = topics
    if (contacts := _read_seed_json("contacts.json")) is not None:
        seed_pairs["emergency_contacts"] = contacts
    if (dispatchers := _read_seed_json("transport_dispatchers.json")) is not None:
        seed_pairs["transport_dispatcher_contacts"] = dispatchers
    if (welcome := _read_seed_text("welcome.md")) is not None:
        seed_pairs["welcome_text"] = welcome
    if (consent := _read_seed_text("consent.md")) is not None:
        seed_pairs["consent_text"] = consent

    newly_seeded: list[str] = []
    for k, v in seed_pairs.items():
        if k not in existing:
            await set_value(session, k, v)
            newly_seeded.append(k)

    # Только те свежие ключи, которые входят в SYNCED_KEYS (репо-синк).
    # welcome_text / consent_text идут не сюда — их baseline хранится в
    # seed/welcome.md и seed/consent.md в формате markdown, репо-синк
    # их не трогает.
    baseline_synced = [k for k in newly_seeded if k in SYNCED_KEYS]
    if baseline_synced:
        await mark_synced(session, baseline_synced)
