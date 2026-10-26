"""Original frozen name policy v1 from 0.2.2; v2 allows surname-first input.

Do not change this policy or its dictionary in place: future revisions get a new
version so replaying historical migrations has deterministic semantics.
"""

import unicodedata
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def given_names() -> frozenset[str]:
    data = files("aemr_bot.data").joinpath("given_names_v1.txt").read_text(encoding="utf-8")
    return frozenset(line.casefold().replace("ё", "е") for line in data.splitlines() if line)


def _word(value: str) -> bool:
    return all(
        part and all(c.isalpha() and "CYRILLIC" in unicodedata.name(c, "") for c in part)
        for part in value.split("-")
    )


def normalize_resident_name(value: object, *, max_chars: int = 120) -> str | None:
    """Given name + optional 1–2 Cyrillic surname/patronymic tokens.

    Only the first token is dictionary checked, as requested. This validates
    spelling shape, not identity or whether a surname/patronymic is real.
    """
    if not isinstance(value, str) or not value or len(value) > min(max_chars, 120):
        return None
    if any(unicodedata.category(c).startswith("C") for c in value):
        return None
    value = unicodedata.normalize("NFC", value).replace("‐", "-").replace("‑", "-")
    parts = value.split()
    if not 1 <= len(parts) <= 3 or not all(_word(p) for p in parts):
        return None
    first = parts[0].casefold().replace("ё", "е")
    names = given_names()
    if first not in names and not ("-" in first and all(p in names for p in first.split("-"))):
        return None
    return " ".join(p.title() for p in parts)
