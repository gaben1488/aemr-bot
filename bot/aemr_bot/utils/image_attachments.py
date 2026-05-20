from __future__ import annotations

from typing import Any

from aemr_bot.utils.attachments import collect_attachments, deserialize_for_relay


def _type_name(raw: Any) -> str:
    value = raw.get("type") if isinstance(raw, dict) else getattr(raw, "type", raw)
    return str(value or "").lower().rsplit(".", 1)[-1]


def is_image_attachment(raw: Any) -> bool:
    return _type_name(raw) == "image"



def image_attachments_from_body(body: Any, *, limit: int = 1) -> list[dict]:
    items = [att for att in collect_attachments(body) if is_image_attachment(att)]
    return items[:limit] if limit > 0 else items


def image_attachments_from_event(event: Any, *, limit: int = 1) -> list[dict]:
    msg = getattr(event, "message", None)
    body = getattr(msg, "body", None) if msg is not None else None
    return image_attachments_from_body(body, limit=limit)


def build_outbound_image_attachments(stored: list[dict] | None) -> list:
    return deserialize_for_relay([att for att in (stored or []) if is_image_attachment(att)])


def attachment_meta(stored: list[dict] | None) -> dict[str, int]:
    return {"images": len(stored or [])}
