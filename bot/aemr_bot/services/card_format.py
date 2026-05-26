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
from aemr_bot.utils.attachments import count_by_type

TZ = ZoneInfo(settings.timezone)

_ATTACHMENT_LABELS = {
    "image": "фото",
    "video": "видео",
    "file": "файлов",
}


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
    return "Вложения: " + ", ".join(parts)


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


def admin_followups_block(appeal: Appeal) -> str:
    followups = [
        msg for msg in _loaded_messages(appeal)
        if getattr(msg, "direction", None) == MessageDirection.FROM_USER.value
    ]
    if not followups:
        return ""

    hidden_count = max(0, len(followups) - 5)
    visible = followups[-5:]
    title = "Дополнение к обращению:" if len(visible) == 1 else "Дополнения к обращению:"
    lines = ["────────────────", title]
    if hidden_count:
        lines.append(f"Ранее было ещё {hidden_count} дополнений.")
    for idx, msg in enumerate(visible, start=1):
        text = (getattr(msg, "text", None) or "").strip()
        attachments = getattr(msg, "attachments", None) or []
        attach_line = attachments_summary_line(attachments)
        body = _clip(text) if text else "Без текста."
        if attach_line:
            body = f"{body}\n{attach_line}"
        if len(visible) == 1:
            lines.append(body)
        else:
            lines.append(f"{idx}. {body}")
    return "\n".join(lines)


def _local_short(dt: datetime) -> str:
    """Короткая локальная дата для timeline — без года, экономия места."""
    return dt.astimezone(TZ).strftime("%d.%m %H:%M")


# Лимит сообщений в timeline — карточка не должна разрастаться.
_TIMELINE_MAX_MESSAGES = 10


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
    ordered = sorted(
        msgs,
        key=lambda m: getattr(m, "created_at", None)
        or datetime.min.replace(tzinfo=TZ),
    )
    hidden_count = max(0, len(ordered) - _TIMELINE_MAX_MESSAGES)
    visible = ordered[-_TIMELINE_MAX_MESSAGES:]
    lines = ["────────────────", "История переписки:"]
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
            text = defang_url_in_text(text)
        body = _clip(text, limit=text_limit) if text else "Без текста."
        attach_line = attachments_summary_line(
            getattr(msg, "attachments", None) or []
        )
        if attach_line:
            body = f"{body}\n{attach_line}"
        lines.append(f"{header}:")
        lines.append(body)
    return "\n".join(lines)


def appeal_timeline_block(appeal: Appeal) -> str:
    """Хронологическая лента переписки для admin-карточки.

    Если ответов оператора ещё не было — fallback на старый
    `admin_followups_block` (компактные «Дополнения к обращению»),
    чтобы не вводить лишний заголовок «История переписки» для
    обращений в состоянии NEW/IN_PROGRESS без диалога.
    """
    msgs = _loaded_messages(appeal)
    if not msgs:
        return ""
    has_operator_msg = any(
        getattr(m, "direction", None) == MessageDirection.FROM_OPERATOR.value
        for m in msgs
    )
    if not has_operator_msg:
        return admin_followups_block(appeal)
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
        address=defang_for_admin(appeal.address) or "—",
        topic=appeal.topic or "—",
        summary=defang_for_admin(appeal.summary) or "—",
        answer_limit=settings.answer_max_chars,
    )
    summary_line = attachments_summary_line(appeal.attachments or [])
    if summary_line:
        body = f"{body}\n{summary_line}"
    # Timeline: полная история переписки (followup'ы жителя + ответы
    # оператора в хронологии). Когда ответов нет — fallback на старый
    # «Дополнения к обращению» (см. appeal_timeline_block). Это
    # выполняет запрос владельца про «явную прозрачную полностью
    # информативную историю и конверсию ответов на обращения».
    timeline = appeal_timeline_block(appeal)
    if timeline:
        body = f"{body}\n\n{timeline}"
    # SECURITY_REVIEW M5: текст обращения (summary) и followup'ы
    # приходят от жителя и могут содержать фишинговые ссылки. Если
    # хоть где-то в карточке (summary + любой followup) есть URL —
    # один общий warning внизу карточки для оператора. Не блокируем
    # отображение, только просим не кликать наугад.
    # try/except защищает от MissingGreenlet (ORM ленивая загрузка
    # `appeal.messages` после закрытия сессии). Если detached — просто
    # не показываем URL warning по timeline, ограничиваемся summary.
    try:
        loaded_messages = list(appeal.messages or [])
    except Exception:
        loaded_messages = []
    has_url = bool(
        _url_in(appeal.summary or "")
        or any(
            _url_in(getattr(m, "text", "") or "")
            for m in loaded_messages
        )
    )
    if has_url:
        body = body + _maybe_url_warning(appeal.summary or "X http://X")
    return body


def _url_in(text: str) -> bool:
    """Тонкая обёртка над extract_urls — bool вместо list, локальная
    кешируемость (для admin_card обоих текстов). Использует тот же
    regex, что и outgoing URL-whitelist (settings_store)."""
    from aemr_bot.services.settings_store import extract_urls
    return bool(extract_urls(text))


def _maybe_url_warning(text: str) -> str:
    """SECURITY_REVIEW M5 + threat-intel: предупреждение оператору
    если в тексте жителя есть URL.

    Два уровня:
    1. Любой http(s) URL → стандартный warning «не открывайте напрямую».
    2. URL в threat-intel базе (URLhaus / ThreatFox / PhishTank) →
       усиленный warning «⛔ это известный фишинг/malware», с
       перечислением скомпрометированных host'ов.

    Threat-intel — best-effort: если бот только что стартовал и cron
    не успел подтянуть feed'ы (set пуст) — обычный warning без
    усиления. Stale-set'ом (старше 6ч) пользуемся, не отказываемся.

    Не блокируем сообщение жителя — у него может быть legitimate
    кейс «мне это прислали мошенники, разберитесь».
    """
    from aemr_bot.services.settings_store import extract_urls
    urls = extract_urls(text)
    if not urls:
        return ""

    # Threat-intel check для каждого URL. Не падаем если модуль
    # сломан — fall back на обычный warning.
    malicious: list[str] = []
    try:
        from aemr_bot.services.threat_intel import get_store
        store = get_store()
        for url in urls:
            is_bad, _source = store.is_malicious(url)
            if is_bad:
                malicious.append(url)
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
            "в МВД (8-800-250-30-72)."
        )
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
