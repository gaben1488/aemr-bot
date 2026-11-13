"""Bound exported text by visible rows, not only Excel's storage-cell limit."""
from datetime import datetime
from math import ceil
from unicodedata import east_asian_width


def _units(value: str) -> int:
    return sum(2 if east_asian_width(c) in {'W', 'F'} or ord(c) > 0xFFFF else 1 for c in value)


def readable_chunks(value: str, width: int = 75) -> list[str]:
    """Preserve every character while limiting a cell to about sixteen lines."""
    budget = max(2, int(width * .8))
    chunks: list[str] = []
    start = units = 0
    lines = 1
    for index, char in enumerate(value):
        if char == '\n':
            if lines == 16:
                chunks.append(value[start:index + 1])
                start, units, lines = index + 1, 0, 1
            else:
                units, lines = 0, lines + 1
            continue
        weight = _units(char)
        if units + weight > budget:
            if lines == 16:
                chunks.append(value[start:index])
                start, lines = index, 1
            else:
                lines += 1
            units = 0
        units += weight
    if start < len(value) or not chunks:
        chunks.append(value[start:])
    return chunks


def row_height(values: list, widths: list[int]) -> float:
    lines = 1
    for value, width in zip(values, widths, strict=True):
        text = value.strftime('%d.%m.%Y %H:%M') if isinstance(value, datetime) else str(value or '')
        budget = max(2, int(width * .8))
        lines = max(lines, sum(max(1, ceil(_units(line) / budget)) for line in text.split('\n')))
    return min(400, lines * 16 + 8)
