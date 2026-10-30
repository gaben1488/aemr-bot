"""Собирает docs/Политика.pdf из docs/Политика.md через reportlab.

Рендерит русский текст шрифтом Times New Roman при его наличии, иначе
DejaVu Serif. Движок Paragraph из reportlab
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
import shutil
import sys
from pathlib import Path

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parent.parent
# Единый мастер-текст политики (тот же, из которого build_policy.mjs собирает
# DOCX). Прежде генератор читал отдельный docs/Политика.md — файл разошёлся с
# мастером и пропал, PDF устаревал. Теперь один источник на DOCX и PDF.
SRC = ROOT / "docs" / "Политика_конфиденциальности_ИС_Чат-бот_MAX.md"
# На диске PDF лежит под латинским именем — Docker buildkit не справляется
# с unicode в инструкции COPY. Имя, которое видит житель в чате MAX,
# подменяется на русское при загрузке файла, см.
# bot/aemr_bot/services/policy.py::POLICY_PDF_DISPLAY_NAME.
OUT = ROOT / "docs" / "PRIVACY.pdf"
NAMED_OUT = ROOT / "docs" / "Политика_конфиденциальности_ИС_Чат-бот_MAX.pdf"

FONT_PAIRS = (
    (Path("C:/Windows/Fonts/times.ttf"), Path("C:/Windows/Fonts/timesbd.ttf")),
    (Path("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman.ttf"),
     Path("/usr/share/fonts/truetype/msttcorefonts/Times_New_Roman_Bold.ttf")),
    (Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),
     Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf")),
)


def preprocess(md: str) -> str:
    """Мастер-текст политики → чистый markdown для гражданского PDF.

    Мастер несёт служебную разметку build_policy.mjs: ведущий HTML-комментарий,
    угловой реквизит приложения, блок наименования и чек-лист юриста ниже
    маркера ПРИЛОЖЕНИЕ-А. В PDF для жителя идёт только тело политики.
    """
    cut = md.find("<!-- ПРИЛОЖЕНИЕ-А")
    if cut != -1:
        md = md[:cut]
    md = re.sub(r"<!--.*?-->", "", md, count=1, flags=re.S)
    md = re.sub(r"\[УГЛОВОЙ-РЕКВИЗИТ\].*?\[/УГЛОВОЙ-РЕКВИЗИТ\]", "", md, flags=re.S)

    def _title(m: re.Match) -> str:
        title = " ".join(s.strip() for s in m.group(1).splitlines() if s.strip())
        return "# " + title

    md = re.sub(r"\[НАИМЕНОВАНИЕ\](.*?)\[/НАИМЕНОВАНИЕ\]", _title, md, flags=re.S)
    return md


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
    fonts = next(((regular, bold) for regular, bold in FONT_PAIRS
                  if regular.exists() and bold.exists()), None)
    if fonts is None:
        print("A Cyrillic serif font pair is required.", file=sys.stderr)
        return 2

    pdfmetrics.registerFont(TTFont("PolicySerif", str(fonts[0])))
    pdfmetrics.registerFont(TTFont("PolicySerif-Bold", str(fonts[1])))
    pdfmetrics.registerFontFamily("PolicySerif", normal="PolicySerif", bold="PolicySerif-Bold")

    styles = getSampleStyleSheet()
    base = dict(fontName="PolicySerif", alignment=TA_JUSTIFY, leading=18)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=14, firstLineIndent=12.5 * mm, spaceAfter=2, allowWidows=0, allowOrphans=0, **base)
    h1 = ParagraphStyle("h1", parent=body, fontName="PolicySerif-Bold", alignment=TA_CENTER, firstLineIndent=0, fontSize=14, leading=18, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=body, fontName="PolicySerif-Bold", alignment=TA_CENTER, firstLineIndent=0, fontSize=14, leading=18, spaceBefore=8, spaceAfter=4)
    h3 = ParagraphStyle("h3", parent=body, fontName="PolicySerif-Bold", alignment=TA_CENTER, firstLineIndent=0, fontSize=14, leading=18, spaceBefore=6, spaceAfter=2)
    li = ParagraphStyle("li", parent=body, leftIndent=14, bulletIndent=4, spaceAfter=2)
    bq = ParagraphStyle("bq", parent=body, leftIndent=10, textColor="#555", fontSize=9.5)

    md = preprocess(SRC.read_text(encoding="utf-8"))
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
        leftMargin=30 * mm,
        rightMargin=10 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title="Политика конфиденциальности — aemr_feedback_bot",
    )
    doc.build(flow)
    shutil.copyfile(OUT, NAMED_OUT)
    print(f"PDF written: {OUT}  ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
