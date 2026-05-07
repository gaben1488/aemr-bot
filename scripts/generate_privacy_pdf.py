"""Собирает docs/Политика.pdf из docs/Политика.md через reportlab.

Рендерит русский текст системным Arial. Движок Paragraph из reportlab
аккуратно обрабатывает длинные ссылки благодаря переносу слов с явными
точками разрыва.

Запуск:
    python scripts/generate_privacy_pdf.py

Имя файла должно совпадать с константой POLICY_PDF_REL в
`bot/aemr_bot/services/policy.py` и с путём COPY в `infra/Dockerfile`.
Если переименовываете — правьте все три места одновременно.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "Политика.md"
# На диске PDF лежит под латинским именем — Docker buildkit не справляется
# с unicode в инструкции COPY. Имя, которое видит житель в чате MAX,
# подменяется на русское при загрузке файла, см.
# bot/aemr_bot/services/policy.py::POLICY_PDF_DISPLAY_NAME.
OUT = ROOT / "docs" / "PRIVACY.pdf"

WIN_FONTS = Path("C:/Windows/Fonts")
FONT_REGULAR = WIN_FONTS / "arial.ttf"
FONT_BOLD = WIN_FONTS / "arialbd.ttf"


def parse_blocks(md: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    lines = md.splitlines()
    buf: list[str] = []

    def flush_paragraph():
        if buf:
            text = " ".join(s.strip() for s in buf if s.strip())
            if text:
                blocks.append(("p", text))
            buf.clear()

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_paragraph()
            continue
        if line.startswith("# "):
            flush_paragraph()
            blocks.append(("h1", line[2:].strip()))
            continue
        if line.startswith("## "):
            flush_paragraph()
            blocks.append(("h2", line[3:].strip()))
            continue
        if line.startswith("### "):
            flush_paragraph()
            blocks.append(("h3", line[4:].strip()))
            continue
        if line.startswith("> "):
            flush_paragraph()
            blocks.append(("blockquote", line[2:].strip()))
            continue
        if line.lstrip().startswith(("- ", "* ")):
            flush_paragraph()
            blocks.append(("li", line.lstrip()[2:].strip()))
            continue
        buf.append(line)

    flush_paragraph()
    return blocks


def render_inline(text: str) -> str:
    """Перевести инлайн-разметку markdown в мини-HTML reportlab."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    return text


def main() -> int:
    if not SRC.exists():
        print(f"source not found: {SRC}", file=sys.stderr)
        return 1
    if not FONT_REGULAR.exists() or not FONT_BOLD.exists():
        print("Arial fonts not found in C:/Windows/Fonts.", file=sys.stderr)
        return 2

    pdfmetrics.registerFont(TTFont("Arial", str(FONT_REGULAR)))
    pdfmetrics.registerFont(TTFont("Arial-Bold", str(FONT_BOLD)))

    styles = getSampleStyleSheet()
    base = dict(fontName="Arial", alignment=TA_LEFT, leading=14)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10.5, spaceAfter=4, **base)
    h1 = ParagraphStyle("h1", parent=body, fontName="Arial-Bold", fontSize=15, leading=18, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=body, fontName="Arial-Bold", fontSize=12.5, leading=16, spaceBefore=8, spaceAfter=4)
    h3 = ParagraphStyle("h3", parent=body, fontName="Arial-Bold", fontSize=11.5, leading=14, spaceBefore=6, spaceAfter=2)
    li = ParagraphStyle("li", parent=body, leftIndent=14, bulletIndent=4, spaceAfter=2)
    bq = ParagraphStyle("bq", parent=body, leftIndent=10, textColor="#555", fontSize=9.5)

    md = SRC.read_text(encoding="utf-8")
    flow = []
    for kind, raw in parse_blocks(md):
        text = render_inline(raw)
        if kind == "h1":
            flow.append(Paragraph(text, h1))
        elif kind == "h2":
            flow.append(Paragraph(text, h2))
        elif kind == "h3":
            flow.append(Paragraph(text, h3))
        elif kind == "li":
            flow.append(Paragraph(text, li, bulletText="•"))
        elif kind == "blockquote":
            flow.append(Paragraph(text, bq))
        else:
            flow.append(Paragraph(text, body))

    doc = SimpleDocTemplate(
        str(OUT),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="Политика конфиденциальности — aemr_feedback_bot",
    )
    doc.build(flow)
    print(f"PDF written: {OUT}  ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
