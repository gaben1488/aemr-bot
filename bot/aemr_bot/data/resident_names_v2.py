"""Frozen name policy v2: given name in either of the first two positions.

Do not change this policy or its dictionary in place: future revisions get a new
version so replaying historical migrations has deterministic semantics.
"""

import unicodedata

from aemr_bot.data.resident_names_v1 import given_names


def _word(value: str) -> bool:
    return all(
        part and all(c.isalpha() and "CYRILLIC" in unicodedata.name(c, "") for c in part)
        for part in value.split("-")
    )


def normalize_resident_name(value: object, *, max_chars: int = 120) -> str | None:
    """Allow 1–3 Cyrillic words; at least one of the first two is a given name.

    Preserve the supplied order. Other words are checked only for spelling
    shape, not against a surname/patronymic dictionary or identity records.
    """
    if not isinstance(value, str) or not value or len(value) > min(max_chars, 120):
        return None
    if any(unicodedata.category(c).startswith("C") for c in value):
        return None
    value = unicodedata.normalize("NFC", value).replace("‐", "-").replace("‑", "-")
    parts = value.split()
    if not 1 <= len(parts) <= 3 or not all(_word(p) for p in parts):
        return None
    names = given_names()
    candidates = [part.casefold().replace("ё", "е") for part in parts[:2]]
    if not any(
        candidate in names
        or ("-" in candidate and all(part in names for part in candidate.split("-")))
        for candidate in candidates
    ):
        return None
    return " ".join(p.title() for p in parts)
