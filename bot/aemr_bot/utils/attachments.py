"""Помощники для разбора вложений сообщения из событий maxapi.

Сверено с исходниками love-apples/maxapi:
* MessageBody.attachments — это list[Attachments], где каждый элемент —
  Attachment с .type (значение enum AttachmentType) и .payload.
* Для вложений типа CONTACT payload — это ContactAttachmentPayload с
  vcf_info: str и max_info: User | None.
* payload.vcf — свойство, которое разбирает vcf_info в VcfInfo с .phone.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Типы, которые мы пересылаем в админ-чат. Исключаем:
#   contact — телефон уже в карточке оператора, vCard продублирует ПДн;
#   inline_keyboard — от гражданина и так никогда не приходит;
#   share/sticker — для обращения не нужны.
RELAYABLE_TYPES = frozenset({"image", "video", "audio", "file", "location"})

# Жёсткая верхняя граница для парсера vcf_info: испорченный или
# вредоносный contact-attachment может прислать мегабайты текста и
# заставить нас сплитить всё это в поисках префикса TEL:. 10k символов —
# выше любого реального vCard.
VCF_INFO_MAX_CHARS = 10_000


def _attachment_to_dict(att: Any) -> dict:
    if hasattr(att, "model_dump"):
        try:
            return att.model_dump(by_alias=False)
        except Exception:
            pass
    if isinstance(att, dict):
        return att
    return {}


# К обращениям принимаем только текст, фото, видео и файлы. Голосовые,
# геолокация, контакты и share-карточки бот молча игнорирует — оператор
# не работает с этими типами в потоке обращений, лишний шум в админ-
# группе их только запутывает.
ALLOWED_APPEAL_TYPES = {"image", "video", "file"}


def collect_attachments(message: Any) -> list[dict]:
    """Взять вложения из тела сообщения MAX и сериализовать для хранения.

    Пропускает только разрешённые типы вложений (см. ALLOWED_APPEAL_TYPES).
    Audio/location/contact/share — отбрасываются молча, чтобы не плодить
    в админ-группе нерелевантный поток.
    """
    out: list[dict] = []
    body = message
    if hasattr(body, "body") and getattr(body, "body", None) is not None:
        body = body.body
    raw = getattr(body, "attachments", None) or []
    for att in raw:
        att_type = getattr(att, "type", None)
        if att_type is None and isinstance(att, dict):
            att_type = att.get("type")
        if str(att_type).lower() not in ALLOWED_APPEAL_TYPES:
            continue
        out.append(_attachment_to_dict(att))
    return out


def extract_location(message: Any) -> tuple[float, float] | None:
    """Достать (latitude, longitude) из вложения типа location.

    MAX присылает его, когда житель тапает кнопку RequestGeoLocationButton.
    Возвращает None, если в сообщении нет вложения типа location либо
    координаты не парсятся.

    Поля из maxapi.types.attachments.location.Location (наследует
    Attachment): `latitude`, `longitude` лежат прямо на attachment,
    не в .payload. Для совместимости с возможными альтернативными
    форматами проверяем также .payload и lat/lon, lat/lng.
    """
    body = message
    if hasattr(body, "body") and getattr(body, "body", None) is not None:
        body = body.body
    raw = getattr(body, "attachments", None) or []

    if not raw:
        return None

    # Диагностический лог: при каждом сообщении с attachments в шаге
    # AWAITING_LOCALITY мы хотим видеть какие типы пришли. Без этого
    # отлаживать geo-flow в production невозможно.
    types_seen = []
    for att in raw:
        t = getattr(att, "type", None) or (att.get("type") if isinstance(att, dict) else None)
        types_seen.append(str(t))
    log.info("extract_location: attachments seen=%s", types_seen)

    for att in raw:
        att_type = getattr(att, "type", None)
        if att_type is None and isinstance(att, dict):
            att_type = att.get("type")
        # Для maxapi: type приходит как str-Enum «location» — равенство
        # работает напрямую без str().lower() трюков.
        if att_type != "location" and str(att_type).lower() != "location":
            continue

        # Дамп attachment — увидеть точный формат что прислал MAX.
        try:
            dumped = att.model_dump(by_alias=False) if hasattr(att, "model_dump") else (
                att if isinstance(att, dict) else dict(att.__dict__) if hasattr(att, "__dict__") else "?"
            )
            log.info("extract_location: location attachment payload=%r", dumped)
        except Exception:
            log.exception("extract_location: dump failed")

        # Координаты могут лежать в нескольких местах: для Location-
        # модели maxapi — прямо на att; для dict — в att или att.payload;
        # для legacy формата — в att.payload.location.
        att_payload = (
            att.get("payload") if isinstance(att, dict)
            else getattr(att, "payload", None)
        )
        candidates = [
            ("att", att),
            ("att.payload", att_payload),
        ]
        if isinstance(att, dict):
            candidates.append(("att[location]", att.get("location")))
            if isinstance(att_payload, dict):
                candidates.append(("att[payload][location]", att_payload.get("location")))

        for label, src in candidates:
            if src is None:
                continue
            for lat_attr, lon_attr in (
                ("latitude", "longitude"),
                ("lat", "lon"),
                ("lat", "lng"),
            ):
                if isinstance(src, dict):
                    lat = src.get(lat_attr)
                    lon = src.get(lon_attr)
                else:
                    lat = getattr(src, lat_attr, None)
                    lon = getattr(src, lon_attr, None)
                if lat is not None and lon is not None:
                    try:
                        result = (float(lat), float(lon))
                        log.info(
                            "extract_location: parsed from %s using %s/%s",
                            label, lat_attr, lon_attr,
                        )
                        return result
                    except (TypeError, ValueError):
                        continue

    log.warning("extract_location: location-attachment найден, но координаты не извлечены")
    return None


def extract_phone(message: Any) -> str | None:
    """Достать номер телефона из вложения типа contact в теле сообщения.

    Работает по MessageBody, по Message и даже по Update (мы спускаемся
    вглубь). Возвращает None, если контакт не найден.
    """
    body = message
    if hasattr(body, "body") and getattr(body, "body", None) is not None:
        body = body.body
    raw = getattr(body, "attachments", None) or []

    for att in raw:
        att_type = getattr(att, "type", None)
        if att_type is None and isinstance(att, dict):
            att_type = att.get("type")
        if str(att_type).lower() != "contact":
            continue

        payload = getattr(att, "payload", None)
        if payload is None and isinstance(att, dict):
            payload = att.get("payload")
        if payload is None:
            continue

        # Форма объекта: ContactAttachmentPayload
        max_info = getattr(payload, "max_info", None)
        if max_info is not None:
            for attr in ("phone", "phone_number"):
                val = getattr(max_info, attr, None)
                if val:
                    return str(val)

        vcf_obj = getattr(payload, "vcf", None)
        if vcf_obj is not None:
            phone = getattr(vcf_obj, "phone", None)
            if phone:
                return str(phone)

        vcf_info = getattr(payload, "vcf_info", None) or getattr(payload, "vcfInfo", None)

        # Форма dict: запасной путь через model_dump
        if isinstance(payload, dict):
            mi = payload.get("max_info") or {}
            if isinstance(mi, dict):
                for k in ("phone", "phone_number"):
                    if mi.get(k):
                        return str(mi[k])
            vcf_info = vcf_info or payload.get("vcf_info") or payload.get("vcfInfo")

        if vcf_info:
            vcf_str = str(vcf_info)
            if len(vcf_str) > VCF_INFO_MAX_CHARS:
                # Вредоносный или сломанный контакт. Не сплитим мегабайты
                # текста ради поиска префикса TEL:.
                log.warning(
                    "vcf_info length %d exceeds %d; truncating before parse",
                    len(vcf_str),
                    VCF_INFO_MAX_CHARS,
                )
                vcf_str = vcf_str[:VCF_INFO_MAX_CHARS]
            for line in vcf_str.replace("\r\n", "\n").splitlines():
                upper = line.upper()
                if upper.startswith("TEL"):
                    _, _, value = line.partition(":")
                    if value.strip():
                        return value.strip()

    return None


def extract_contact_name(message: Any) -> str | None:
    """Имя из расшаренного контакта.

    MAX в payload contact-вложения отдаёт либо `max_info.first_name`
    (когда житель шарит свой собственный профиль через
    RequestContactButton), либо vCF-структуру с полем `FN:` или
    `name`. Подбираем оба варианта. Возвращаем None, если ничего
    приемлемого не нашли.

    Без этого житель проходил бы шаг «как к вам обращаться» вручную
    даже после того, как уже отдал контакт, в котором его имя есть.
    """
    body = message
    if hasattr(body, "body") and getattr(body, "body", None) is not None:
        body = body.body
    raw = getattr(body, "attachments", None) or []

    for att in raw:
        att_type = getattr(att, "type", None)
        if att_type is None and isinstance(att, dict):
            att_type = att.get("type")
        if str(att_type).lower() != "contact":
            continue

        payload = getattr(att, "payload", None)
        if payload is None and isinstance(att, dict):
            payload = att.get("payload")
        if payload is None:
            continue

        # Pydantic-форма: max_info с first_name
        max_info = getattr(payload, "max_info", None)
        if max_info is not None:
            for attr in ("first_name", "name"):
                val = getattr(max_info, attr, None)
                if val:
                    return str(val).strip() or None

        vcf_obj = getattr(payload, "vcf", None)
        if vcf_obj is not None:
            for attr in ("first_name", "name", "fn"):
                val = getattr(vcf_obj, attr, None)
                if val:
                    return str(val).strip() or None

        # Dict-fallback
        if isinstance(payload, dict):
            mi = payload.get("max_info") or {}
            if isinstance(mi, dict):
                for k in ("first_name", "name"):
                    if mi.get(k):
                        return str(mi[k]).strip() or None

        # Сырой vCF: ищем строку «FN:Имя»
        vcf_info = getattr(payload, "vcf_info", None) or getattr(payload, "vcfInfo", None)
        if vcf_info is None and isinstance(payload, dict):
            vcf_info = payload.get("vcf_info") or payload.get("vcfInfo")
        if vcf_info:
            vcf_str = str(vcf_info)
            if len(vcf_str) > VCF_INFO_MAX_CHARS:
                vcf_str = vcf_str[:VCF_INFO_MAX_CHARS]
            for line in vcf_str.replace("\r\n", "\n").splitlines():
                upper = line.upper()
                if upper.startswith("FN:") or upper.startswith("FN;"):
                    _, _, value = line.partition(":")
                    val = value.strip()
                    if val:
                        return val

    return None


def _normalize_type(att: dict) -> str:
    return str(att.get("type", "")).lower()


def count_by_type(stored: list[dict]) -> dict[str, int]:
    """Подсчитать вложения по типам. Используется для отрисовки заголовка карточки в админ-чате."""
    counts: dict[str, int] = {}
    for att in stored:
        if not isinstance(att, dict):
            continue
        kind = _normalize_type(att)
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def deserialize_for_relay(stored: list[dict]) -> list:
    """Развернуть сохранённые словари обратно в pydantic-объекты Attachment,
    чтобы send_message смог снова сбросить их в полезную нагрузку API.
    Откидывает типы с ПДн и непересылаемые типы. Возвращает пустой
    список, если maxapi недоступен, чтобы вызывающий код не падал.
    """
    if not stored:
        return []
    try:
        from pydantic import TypeAdapter

        from maxapi.types.attachments import Attachments
    except Exception:
        log.exception("maxapi attachment types unavailable; skipping relay")
        return []

    adapter: TypeAdapter = TypeAdapter(Attachments)
    out: list = []
    for raw in stored:
        if not isinstance(raw, dict):
            continue
        if _normalize_type(raw) not in RELAYABLE_TYPES:
            continue
        try:
            out.append(adapter.validate_python(raw))
        except Exception as e:
            # Расхождение схемы на стороне MAX или урезанный payload.
            # Пропускаем это вложение, чтобы не заблокировать отправку
            # всего обращения.
            log.warning("attachment %s failed to deserialize: %r", _normalize_type(raw), e)
    return out
