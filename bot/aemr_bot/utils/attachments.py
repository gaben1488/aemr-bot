"""Helpers for parsing message attachments from maxapi events.

Verified against love-apples/maxapi sources:
* MessageBody.attachments is list[Attachments] where each item is an
  Attachment with .type (AttachmentType enum value) and .payload.
* For CONTACT attachments, payload is ContactAttachmentPayload with
  vcf_info: str and max_info: User | None.
* payload.vcf is a property that parses vcf_info into VcfInfo with .phone.
"""

from typing import Any


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
    """Take attachments from a MAX message body and serialize for storage."""
    out: list[dict] = []
    body = message
    if hasattr(body, "body") and getattr(body, "body", None) is not None:
        body = body.body
    raw = getattr(body, "attachments", None) or []
    for att in raw:
        out.append(_attachment_to_dict(att))
    return out


def extract_phone(message: Any) -> str | None:
    """Extract phone number from a contact-type attachment in the message body.

    Works on a MessageBody, a Message, or even an Update (we drill down).
    Returns None if no contact found.
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

        # Object form: ContactAttachmentPayload
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

        # Dict form: model_dump fallback
        if isinstance(payload, dict):
            mi = payload.get("max_info") or {}
            if isinstance(mi, dict):
                for k in ("phone", "phone_number"):
                    if mi.get(k):
                        return str(mi[k])
            vcf_info = vcf_info or payload.get("vcf_info") or payload.get("vcfInfo")

        if vcf_info:
            for line in str(vcf_info).replace("\r\n", "\n").splitlines():
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
