from typing import Any


def collect_attachments(message: Any) -> list[dict]:
    """Take attachments from a MAX Message object and serialize for storage."""
    out: list[dict] = []
    raw = getattr(message, "attachments", None) or []
    for att in raw:
        try:
            data = att.model_dump() if hasattr(att, "model_dump") else dict(att)
        except Exception:
            data = {"raw": str(att)}
        out.append(data)
    return out


def is_contact_attachment(att: Any) -> bool:
    t = att.get("type") if isinstance(att, dict) else getattr(att, "type", None)
    return t == "contact"


def extract_phone(message: Any) -> str | None:
    raw = getattr(message, "attachments", None) or []
    for att in raw:
        data = att.model_dump() if hasattr(att, "model_dump") else att if isinstance(att, dict) else {}
        if data.get("type") != "contact":
            continue
        payload = data.get("payload") or {}
        vcf = payload.get("vcfInfo") or payload.get("vcf_info") or ""
        for line in str(vcf).splitlines():
            if line.upper().startswith("TEL"):
                _, _, number = line.partition(":")
                if number.strip():
                    return number.strip()
        for k in ("phone", "phoneNumber", "phone_number"):
            if payload.get(k):
                return str(payload[k])
    return None
