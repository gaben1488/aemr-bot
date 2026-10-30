"""Split text conservatively for MAX screens without losing characters."""

from __future__ import annotations


def fits_max_message(text: str, limit: int = 3999) -> bool:
    """App UTF-16 budget; pinned maxapi rejects Python len >= 4000.

    The official API documents up to 4000 characters without specifying
    UTF-16 here. Counting non-BMP characters twice is our conservative rule.
    """
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text) <= limit


def split_text_units(text: str, limit: int = 2600) -> list[str]:
    if limit < 2:
        raise ValueError("limit must allow a non-BMP character")
    if not text:
        return [""]
    chunks: list[str] = []
    start = 0
    units = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            chunks.append(text[start:index])
            start, units = index, 0
        units += width
    chunks.append(text[start:])
    return chunks
