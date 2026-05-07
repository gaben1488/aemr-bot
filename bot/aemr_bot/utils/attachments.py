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


def collect_attachments(message: Any) -> list[dict]:
    """Взять вложения из тела сообщения MAX и сериализовать для хранения."""
    out: list[dict] = []
    body = message
    if hasattr(body, "body") and getattr(body, "body", None) is not None:
        body = body.body
    raw = getattr(body, "attachments", None) or []
    for att in raw:
        out.append(_attachment_to_dict(att))
    return out


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


def is_contact_attachment(att: Any) -> bool:
    t = getattr(att, "type", None)
    if t is None and isinstance(att, dict):
        t = att.get("type")
    return str(t).lower() == "contact"


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
