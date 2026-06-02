import re
from datetime import datetime

from zoneinfo import ZoneInfo

from aemr_bot.config import settings
from aemr_bot.db.models import Appeal, MessageDirection, User
from aemr_bot.texts import (
    ADMIN_CARD_TEMPLATE,
    ADMIN_FOLLOWUP_TEMPLATE,
    APPEAL_CARD_TEMPLATE,
    STATUS_LABELS,
)
from aemr_bot.utils.attachments import count_by_type, suspicious_attachment_names

TZ = ZoneInfo(settings.timezone)

_ATTACHMENT_LABELS = {
    "image": "фото",
    "video": "видео",
    "file": "файлов",
}

# Anti-spoof: карточка использует box-drawing глифы (`━━━ ОБРАЩЕНИЕ #N ━━━`)
# как заголовки секций. Житель, вписав такой же `━━━ … ━━━` в текст
# обращения, может нарисовать поддельный баннер («✅ Обращение закрыто
# администрацией») и ввести оператора в заблуждение. Вырезаем box-drawing
# (U+2500–U+257F) из текста жителя перед показом — он этими символами в
# обращении не пишет, а спуфер рисует ими фейковую «шапку».
_CARD_CHROME_RE = re.compile(r"[─-╿]+")


def _strip_card_chrome(text: str | None) -> str | None:
    if not text:
        return text
    return _CARD_CHROME_RE.sub("", text)


def _local(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def attachments_summary_line(attachments: list[dict]) -> str:
    """Однострочная сводка по вложениям гражданина для карточки в
    админ-чате. Возвращает пустую строку, если показать нечего.
    """
    counts = count_by_type(attachments or [])
    if not counts:
        return ""
    parts: list[str] = []
    for kind, label in _ATTACHMENT_LABELS.items():
        n = counts.get(kind, 0)
        if n:
            parts.append(f"{label} {n}")
    if not parts:
        return ""
    line = "Вложения: " + ", ".join(parts)
    # Anti-spoof: двойное расширение в имени файла (`.exe.pdf`) — попытка
    # выдать исполняемое за документ. Предупреждаем оператора до открытия.
    suspicious = suspicious_attachment_names(attachments or [])
    if suspicious:
        line += "\n⚠️ подозрительное имя (двойное расширение): " + ", ".join(suspicious)
    return line


def _clip(text: str, limit: int = 900) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _loaded_messages(appeal: Appeal) -> list:
    """Берём только уже загруженные messages.

    У SQLAlchemy lazy-load в async-коде может упасть MissingGreenlet, если
    случайно обратиться к незагруженной связи. Поэтому смотрим в __dict__:
    selectinload положит туда список, а незагруженную связь не трогаем.
    """
    messages = getattr(appeal, "__dict__", {}).get("messages")
    return list(messages or [])


def _local_short(dt: datetime) -> str:
    """Короткая локальная дата для timeline — без года, экономия места."""
    return dt.astimezone(TZ).strftime("%d.%m %H:%M")


# Лимит сообщений в timeline — карточка не должна разрастаться.
_TIMELINE_MAX_MESSAGES = 10

# SECURITY (P2-3, perf + scope): cap длины конкатенации текста перед
# тем, как прогнать её через extract_urls / threat-intel в
# _maybe_url_warning. Без cap'а карточка обращения с тысячами длинных
# followup'ов заставляла бы синхронный regex-скан мегабайтов текста на
# КАЖДОМ рендере (DoS-вектор: житель шлёт тонну текста, оператор
# открывает карточку → подвисание event-loop). 50 КБ с запасом
# покрывает видимый срез (10 сообщений × text_limit) + summary;
# обрезка идёт по символам, не по URL — частичный URL на границе
# среза в худшем случае просто не сматчится, что fail-closed для
# нашей задачи (warning — best-effort сигнал, не блокировка).
_URL_SCAN_MAX_CHARS = 50_000


def _visible_timeline_messages(msgs: list) -> list:
    """Видимый срез ленты — последние `_TIMELINE_MAX_MESSAGES` сообщений
    в хронологическом порядке.

    Единый источник истины для (а) того, что реально рендерится в
    карточке (`_render_timeline`), и (б) того, по какому тексту мы ищем
    URL для warning'а (`admin_card`). Раньше warning сканировал ВСЮ
    историю (`appeal.messages`), а показывались только последние 10 —
    рассинхрон: ⛔ мог сработать по ссылке, которой оператор в карточке
    даже не видит, плюс O(N) regex-скан на каждом рендере. Теперь оба
    пути берут один и тот же tail.
    """
    if not msgs:
        return []
    ordered = sorted(
        msgs,
        key=lambda m: getattr(m, "created_at", None) or datetime.min.replace(tzinfo=TZ),
    )
    return ordered[-_TIMELINE_MAX_MESSAGES:]


def _render_timeline(
    msgs: list,
    *,
    operator_marker: str,
    user_marker: str,
    text_limit: int,
) -> str:
    """Единый рендер хронологической ленты переписки.

    Используется и admin (📨 Ответ оператора / 📩 Дополнение жителя), и
    user (📨 Ответ Администрации / 📩 Ваше дополнение) вариантами —
    маркеры передаются параметрами, а структура блока (заголовок,
    hidden-count, дата · текст · вложения) единая.

    text_limit — пер-сообщение, для admin меньше (компактная сводка
    оператору), для user больше (житель хочет видеть полный ответ).
    """
    if not msgs:
        return ""
    hidden_count = max(0, len(msgs) - _TIMELINE_MAX_MESSAGES)
    visible = _visible_timeline_messages(msgs)
    lines = ["· · · · · · · ·", "История переписки:"]
    if hidden_count:
        lines.append(f"Ранее ещё {hidden_count} сообщений (скрыты).")
    for msg in visible:
        direction = getattr(msg, "direction", "")
        if direction == MessageDirection.FROM_OPERATOR.value:
            marker = operator_marker
        elif direction == MessageDirection.FROM_USER.value:
            marker = user_marker
        else:
            marker = "•"
        created_at = getattr(msg, "created_at", None)
        time_str = _local_short(created_at) if created_at else ""
        header = f"{marker} ({time_str})" if time_str else marker
        text = (getattr(msg, "text", None) or "").strip()
        # SECURITY (URL defang): сообщения жителей в timeline могут
        # содержать кликабельные URL — оператор должен видеть текст,
        # но не должен мочь случайно тапнуть. Defang только для
        # «от жителя» — ответы оператора уже прошли whitelist на
        # outgoing, тем им defang не нужен.
        if direction == MessageDirection.FROM_USER.value and text:
            from aemr_bot.utils.url_defang import defang_url_in_text

            # _strip_card_chrome: тот же anti-spoof, что для summary —
            # followup жителя не должен рисовать поддельную «шапку».
            text = defang_url_in_text(_strip_card_chrome(text) or "")
        body = _clip(text, limit=text_limit) if text else "Без текста."
        attach_line = attachments_summary_line(getattr(msg, "attachments", None) or [])
        if attach_line:
            body = f"{body}\n{attach_line}"
        lines.append(f"{header}:")
        lines.append(body)
    return "\n".join(lines)


def appeal_timeline_block(appeal: Appeal) -> str:
    """Хронологическая лента переписки для admin-карточки.

    A1 (2026-05-27): унифицировано. Раньше при отсутствии ответа
    оператора возвращался компактный `admin_followups_block`
    («Дополнения к обращению»), а после первого ответа формат
    переключался на полноценный timeline («История переписки»). Два
    рендерера давали несогласованный UX: оператор видел разный
    layout до и после собственного ответа. Теперь всегда timeline —
    единый формат с момента появления хотя бы одного сообщения.
    """
    msgs = _loaded_messages(appeal)
    if not msgs:
        return ""
    return _render_timeline(
        msgs,
        operator_marker="📨 Ответ оператора",
        user_marker="📩 Дополнение жителя",
        text_limit=400,
    )


def _citizen_status_line(user: User) -> str:
    """Компактная строка статуса жителя для admin-карточки — оператор
    видит «нормальный житель» vs «отозвал согласие, обращение в работе
    для финального ответа» vs «заблокирован» одним взглядом.

    Формат: `Статус: <маркер подписки> · <маркер согласия>[ · 🚫 заблокирован]`.

    Маркеры:
    - 🔔 / 🔕 — подписан / не подписан на рассылку.
    - ✅ / 🔁 — согласие активно / отозвано (revoked имеет приоритет).
    - 🚫 — заблокирован (добавляется только если applicable).
    """
    parts: list[str] = []
    if getattr(user, "subscribed_broadcast", False):
        parts.append("🔔 подписан")
    else:
        parts.append("🔕 без подписки")
    if getattr(user, "consent_revoked_at", None) is not None:
        parts.append("🔁 согласие отозвано")
    elif getattr(user, "consent_pdn_at", None) is not None:
        parts.append("✅ согласие активно")
    if getattr(user, "is_blocked", False):
        parts.append("🚫 заблокирован")
    return "Статус: " + " · ".join(parts)


def admin_card(appeal: Appeal, user: User) -> str:
    """Карточка обращения в служебной группе — единый стиль независимо от
    того, есть вложения или нет.

    Раньше было «то 2, то 3 разделителя в зависимости от наличия фото».
    Теперь блок «Вложения» всегда добавляется внутри тела через ту же
    линию, что и остальные секции, без второй декоративной полосы.

    Под именем/телефоном — строка маркеров состояния жителя (PR F):
    подписка / согласие / блокировка. Оператор видит контекст «обычный»
    vs «отозвал согласие — финальный ответ» vs «заблокирован» сразу,
    без прыжков в админ-меню.
    """
    # SECURITY: текст обращения (summary) и адрес — единственные
    # поля карточки, куда житель может вписать произвольный URL.
    # Defang делает любые http(s)-ссылки некликабельными в admin-MAX
    # без визуального изменения. Имя/телефон/локация не показывают
    # URL по дизайну (валидация на входе), defang не нужен.
    from aemr_bot.utils.url_defang import defang_for_admin

    body = ADMIN_CARD_TEMPLATE.format(
        number=appeal.id,
        name=user.first_name or "—",
        phone=user.phone or "—",
        status_line=_citizen_status_line(user),
        locality=appeal.locality or "—",
        address=defang_for_admin(_strip_card_chrome(appeal.address)) or "—",
        topic=appeal.topic or "—",
        summary=defang_for_admin(_strip_card_chrome(appeal.summary)) or "—",
        answer_limit=settings.answer_max_chars,
    )
    summary_line = attachments_summary_line(appeal.attachments or [])
    if summary_line:
        body = f"{body}\n{summary_line}"
    # Timeline: полная история переписки (followup'ы жителя + ответы
    # оператора в хронологии). A1 (2026-05-27): единый формат
    # «История переписки» с момента первого сообщения — без
    # переключения layout при первом ответе оператора.
    timeline = appeal_timeline_block(appeal)
    if timeline:
        body = f"{body}\n\n{timeline}"
    # SECURITY_REVIEW M5 + P2-3: текст обращения (summary) и followup'ы
    # приходят от жителя и могут содержать фишинговые ссылки. Один
    # общий warning внизу карточки, если в ВИДИМОЙ части есть URL.
    #
    # Scope (P2-3 fix 2026-06-02): сканируем только тот же видимый срез
    # ленты (`_visible_timeline_messages` — последние
    # `_TIMELINE_MAX_MESSAGES`), что реально показан оператору, а НЕ всю
    # историю `appeal.messages`. Раньше карточка обращения с тысячами
    # followup'ов прогоняла extract_urls + threat-intel по всему объёму
    # СИНХРОННО на каждом рендере (DoS-вектор + рассинхрон: ⛔ мог
    # сработать по ссылке вне видимой части). Плюс длина конкатенации
    # capped на `_URL_SCAN_MAX_CHARS` перед скан-функцией.
    #
    # try/except защищает от MissingGreenlet (ORM ленивая загрузка
    # `appeal.messages` после закрытия сессии). Если detached — просто
    # не показываем URL warning по timeline, ограничиваемся summary.
    try:
        loaded_messages = list(appeal.messages or [])
    except Exception:
        loaded_messages = []
    warn_src = _bounded_scan_source(
        appeal.summary or "",
        _visible_timeline_messages(loaded_messages),
    )
    # _maybe_url_warning возвращает "" если URL'ов нет — отдельный
    # double-scan (старый `has_url`) больше не нужен, экономим проход.
    body = body + _maybe_url_warning(warn_src)
    return body


def _bounded_scan_source(summary: str, visible_msgs: list) -> str:
    """Собрать текст для URL-скана из summary + видимых сообщений,
    обрезав суммарную длину до `_URL_SCAN_MAX_CHARS`.

    Defense-in-depth (P2-3): даже если видимый срез ограничен 10
    сообщениями, отдельное сообщение жителя теоретически может быть
    очень длинным (валидация на входе ограничивает, но карточка не
    должна полагаться на это). Cap гарантирует, что синхронный
    regex-скан в `_maybe_url_warning` работает по ограниченному
    объёму — линейно и предсказуемо, без подвисания event-loop.

    Обрезаем по символам: частичный URL на границе в худшем случае не
    сматчится (warning — best-effort сигнал оператору, не блокировка),
    что приемлемо fail-closed-поведение.
    """
    parts = [summary] + [(getattr(m, "text", "") or "") for m in visible_msgs]
    out = "\n".join(parts)
    if len(out) > _URL_SCAN_MAX_CHARS:
        out = out[:_URL_SCAN_MAX_CHARS]
    return out


def _maybe_url_warning(text: str) -> str:
    """SECURITY_REVIEW M5 + threat-intel: предупреждение оператору
    если в тексте жителя есть URL.

    Два уровня:
    1. Любой http(s) URL → стандартный warning «не открывайте напрямую».
    2. URL в threat-intel базе (URLhaus / ThreatFox / PhishTank) →
       усиленный warning «⛔ это известный фишинг/malware», с
       перечислением скомпрометированных host'ов.

    P3-4 (2026-06-02): голые домены без схемы (`login-gosuslugi.top`)
    раньше проходили мимо `extract_urls` (тот ловит только `http(s)://`
    + unicode-омоглиф-quasi), поэтому defang их экранировал, но ⛔
    threat-intel warning не срабатывал — оператор не получал сигнала,
    что bare-host жителя есть в базе известного фишинга. Теперь хосты
    извлекаются и через `url_defang._BARE_DOMAIN_PATTERN` (тот же
    regex, что уже используется для defang'а) и прогоняются через
    threat-intel. Базовый ⚠️ «содержит ссылку» по-прежнему гейтится на
    `extract_urls` (http/quasi) — поведение для benign bare-domain не
    меняется, эскалируем только при реальном malware-хосте.

    Threat-intel — best-effort: если бот только что стартовал и cron
    не успел подтянуть feed'ы (set пуст) — обычный warning без
    усиления. Stale-set'ом (старше 6ч) пользуемся, не отказываемся.

    Не блокируем сообщение жителя — у него может быть legitimate
    кейс «мне это прислали мошенники, разберитесь».
    """
    from aemr_bot.services.settings_store import extract_urls

    urls = extract_urls(text)

    # P3-4: bare-domain кандидаты (без схемы) — `login-gosuslugi.top`,
    # `vk-id.ru` и т.п. Берём ровно тот же `_BARE_DOMAIN_PATTERN`, что
    # и defang, чтобы scope warning'а совпадал со scope экранирования.
    # group(1) = имя 2-го уровня, group(2) = TLD → `host` = `имя.tld`.
    bare_hosts: list[str] = []
    if text:
        from aemr_bot.utils.url_defang import _BARE_DOMAIN_PATTERN

        for m in _BARE_DOMAIN_PATTERN.finditer(text):
            bare_hosts.append(f"{m.group(1)}.{m.group(2)}")

    if not urls and not bare_hosts:
        return ""

    # Threat-intel check для каждого http(s)-URL И каждого bare-host.
    # Не падаем если модуль сломан — fall back на обычный warning.
    # is_malicious сам нормализует host (lowercase, strip www), bare-host
    # без схемы тоже корректно парсится (`_normalize_host` дописывает
    # `http://` искусственно).
    malicious: list[str] = []
    try:
        from aemr_bot.services.threat_intel import get_store

        store = get_store()
        for candidate in [*urls, *bare_hosts]:
            is_bad, _source = store.is_malicious(candidate)
            if is_bad and candidate not in malicious:
                malicious.append(candidate)
    except Exception:
        pass

    if malicious:
        # Показать до 3 ссылок, остальные за многоточием
        sample = ", ".join(malicious[:3])
        more = "" if len(malicious) <= 3 else f" и ещё {len(malicious) - 3}"
        return (
            "\n\n⛔ Подозрительные ссылки (известные фишинг/malware "
            f"по threat-intel базе): {sample}{more}. "
            "Категорически не открывайте, не пересылайте. Если житель "
            "просит разобраться — отметьте в audit, направьте жителя "
            "в МВД (отделение полиции по месту жительства, единый "
            "экстренный номер 112)."
        )
    # Базовый ⚠️ показываем только при наличии http(s)/quasi-URL
    # (`urls`). Голый домен без схемы сам по себе ⚠️ НЕ триггерит —
    # поведение сохранено как до P3-4: defang его уже экранировал, а
    # эскалация до ⛔ выше срабатывает лишь когда host реально в
    # threat-intel базе. Иначе (только benign bare-host) — пусто.
    if not urls:
        return ""
    return (
        "\n\n⚠️ Текст содержит ссылку. Не открывайте напрямую из "
        "карточки — сверьте адрес визуально и при необходимости "
        "введите в браузер вручную."
    )


def admin_followup(appeal: Appeal, user: User, text: str) -> str:
    """Карточка дополнения от жителя для admin-чата.

    URL'ы в тексте жителя проходят defang — оператор видит ссылку,
    но не может тапнуть случайно (защита от accidental phishing-click).
    Подробности: utils/url_defang.py.
    """
    from aemr_bot.utils.url_defang import defang_for_admin

    rendered = ADMIN_FOLLOWUP_TEMPLATE.format(
        number=appeal.id,
        name=user.first_name or "—",
        text=defang_for_admin(text),
    )
    # warning остаётся — оператор должен явно знать что в тексте была
    # ссылка, даже если она defang'нута; защита 2-в-1 (visual cue +
    # technical un-click).
    return rendered + _maybe_url_warning(text)


def citizen_reply(appeal: Appeal, reply_text: str) -> str:
    """Обернуть текстовый ответ оператора в формальную рамку письма,
    чтобы гражданин видел, кто ответил и по какому обращению, а не
    голое сообщение в личке с ботом."""
    from aemr_bot.texts import CITIZEN_REPLY_TEMPLATE

    return CITIZEN_REPLY_TEMPLATE.format(
        number=appeal.id,
        created_at=_local(appeal.created_at),
        topic=appeal.topic or "—",
        locality=appeal.locality or "—",
        address=appeal.address or "—",
        reply_text=reply_text,
    )


def user_card(appeal: Appeal) -> str:
    emoji, label = STATUS_LABELS.get(appeal.status, ("•", appeal.status))
    body = APPEAL_CARD_TEMPLATE.format(
        number=appeal.id,
        created_at=_local(appeal.created_at),
        status_emoji=emoji,
        status_label=label,
        locality=appeal.locality or "—",
        address=appeal.address or "—",
        topic=appeal.topic or "—",
        summary=appeal.summary or "—",
    )
    # Лента для жителя: те же блоки что у admin_card, но житель видит
    # «Ваше дополнение» и «Ответ Администрации» вместо служебных
    # маркеров (через user_appeal_timeline_block).
    timeline = user_appeal_timeline_block(appeal)
    if timeline:
        body = f"{body}\n\n{timeline}"
    return body


def user_appeal_timeline_block(appeal: Appeal) -> str:
    """Хронологическая лента переписки для карточки жителя.

    От admin-варианта отличается только маркерами (от 2-го лица,
    «Администрации» формальнее «оператора») и лимитом текста
    (700 vs 400 — житель хочет видеть полный ответ).
    """
    return _render_timeline(
        _loaded_messages(appeal),
        operator_marker="📨 Ответ Администрации",
        user_marker="📩 Ваше дополнение",
        text_limit=700,
    )


def appeal_list_label(appeal: Appeal) -> str:
    """Метка обращения для списка «📂 Мои обращения» жителя.

    Для new/in_progress показываем дату создания (когда подал). Для
    answered — дату ответа (когда ответили). Для closed — дату закрытия.
    Так житель сразу видит «вот когда мне ответили», а не «вот когда
    я писал» (создание уже не информативно после ответа).
    """
    emoji, label = STATUS_LABELS.get(appeal.status, ("•", appeal.status))
    summary_preview = (appeal.summary or "").replace("\n", " ")[:32]
    # Выбор отображаемой даты — по фазе жизненного цикла обращения.
    if appeal.status == "answered":
        relevant_date = getattr(appeal, "answered_at", None) or appeal.created_at
    elif appeal.status == "closed":
        relevant_date = getattr(appeal, "closed_at", None) or appeal.created_at
    else:
        relevant_date = appeal.created_at
    return f"{emoji} #{appeal.id} · {label} · {_local(relevant_date)} · {summary_preview}"
