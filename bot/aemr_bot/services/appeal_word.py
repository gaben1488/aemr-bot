"""Create a readable Word history without publishing transport tokens."""
from datetime import UTC, datetime
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from aemr_bot.config import settings


def _xml_text(value: object) -> str:
    # XML 1.0 excludes some Unicode controls. Preserve their identity visibly
    # instead of dropping them: Word cannot represent the literal characters.
    value = str(value or "")
    return escape("".join(c if c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF
                          or 0xE000 <= ord(c) <= 0xFFFD
                          or 0x10000 <= ord(c) <= 0x10FFFF
                          else f"⟦U+{ord(c):04X}⟧" for c in value))


def _paragraph(text: object, style: str = "Normal") -> str:
    # w:br is used instead of an XML newline so Word displays line breaks.
    lines = _xml_text(text).split("\n")
    runs = '<w:br/>'.join(f'<w:t xml:space="preserve">{line}</w:t>' for line in lines)
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r>{runs}</w:r></w:p>'


def _date(value) -> str:
    if not value:
        return "не указана"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m.%Y %H:%M")


def _attachment_lines(attachments) -> list[str]:
    types = {"image": "Фото", "video": "Видео", "file": "Файл", "audio": "Аудио"}
    result = []
    for index, item in enumerate(attachments or [], 1):
        if not isinstance(item, dict):
            result.append(f"{index}. Вложение: сведения недоступны")
            continue
        kind = types.get(str(item.get("type", "")).lower(), "Вложение")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        name = item.get("filename") or payload.get("filename") or item.get("name") or ""
        result.append(f"{index}. {kind}" + (f" — {name}" if name else ""))
    return result


def build_appeal_docx(appeal) -> bytes:
    user = getattr(appeal, "user", None)
    status = {"new": "Новое", "in_progress": "В работе", "answered": "Ответ дан",
              "closed": "Закрыто"}.get(appeal.status, "Статус не указан")
    body = [_paragraph(f"Обращение № {appeal.id}", "Title"),
            _paragraph(f"Подано: {_date(appeal.created_at)} · {status}", "Subtitle")]
    fields = (("Житель", getattr(user, "first_name", None)),
              ("Телефон", getattr(user, "phone", None)),
              ("Населённый пункт", appeal.locality), ("Адрес", appeal.address),
              ("Тема", appeal.topic))
    body.extend(_paragraph(f"{label}: {value or 'не указано'}") for label, value in fields)
    body.extend([_paragraph("Обращение", "Heading1"), _paragraph(appeal.summary or "Текст не указан")])
    for line in _attachment_lines(getattr(appeal, "attachments", [])):
        body.append(_paragraph(line))
    body.append(_paragraph("История", "Heading1"))
    messages = sorted(getattr(appeal, "messages", []) or [],
                      key=lambda m: (getattr(m, "created_at", None) or datetime.min.replace(tzinfo=UTC),
                                     getattr(m, "id", 0) or 0))
    if not messages:
        body.append(_paragraph("Дополнений и ответов пока нет."))
    for message in messages:
        label = {"from_user": "Дополнение жителя", "from_operator": "Ответ специалиста",
                 "system": "Служебная запись"}.get(message.direction, "Запись")
        body.append(_paragraph(f"{label} · {_date(message.created_at)}", "Heading2"))
        body.append(_paragraph(message.text or "Без текста"))
        body.extend(_paragraph(line) for line in _attachment_lines(message.attachments))
    body.append(_paragraph("Согласие на обработку данных", "Heading1"))
    records = getattr(user, "consent_records", None) or []
    if not records:
        body.append(_paragraph("Текст ранее данного согласия не сохранён; восстановить его по текущим настройкам нельзя."))
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("text"), str):
            continue
        try:
            accepted = _date(datetime.fromisoformat(record["accepted_at"]))
        except (KeyError, TypeError, ValueError):
            accepted = "время неизвестно"
        body.extend([_paragraph(f"Подтверждено: {accepted}", "Heading2"), _paragraph(record["text"])])
    body.append(_paragraph("Вложения перечислены для каждой записи. Сам документ не содержит файлы; "
                           "открывайте их в карточке обращения. Доступность зависит от MAX."))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body>' + ''.join(body) + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1134" w:right="850" w:bottom="1134" w:left="1701"/>'
                '</w:sectPr></w:body></w:document>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" '
              'w:hAnsi="Times New Roman" w:cs="Times New Roman"/><w:sz w:val="24"/></w:rPr>'
              '</w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="100" w:line="320" '
              'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
              + ''.join(f'<w:style w:type="paragraph" w:styleId="{name}"><w:name w:val="{name}"/>'
                        f'<w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr>'
                        '</w:style>' for name, size in (("Title", 34), ("Subtitle", 22),
                                                       ("Heading1", 28), ("Heading2", 24)))
              + '</w:styles>')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                     '</Types>')
    relationships = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                     '</Relationships>')
    document_relationships = ('<?xml version="1.0" encoding="UTF-8"?>'
                              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                              '</Relationships>')
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", document_relationships)
    return output.getvalue()
