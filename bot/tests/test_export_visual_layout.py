"""Regressions reproduced by rendering long synthetic histories in Writer/Calc."""
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from xml.etree import ElementTree
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook

from aemr_bot.services.appeal_word import build_appeal_docx
from aemr_bot.services.complete_stats import _Sheet, cell_chunks


def test_word_history_heading_stays_with_following_message():
    appeal = SimpleNamespace(id=1, created_at=datetime.now(UTC), status='new',
        locality='', address='', topic='', summary='Текст', messages=[], attachments=[], user=None)
    with ZipFile(BytesIO(build_appeal_docx(appeal))) as archive:
        root = ElementTree.fromstring(archive.read('word/styles.xml'))
    ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    for name in ('Title','Heading1','Heading2'):
        style=root.find(f'w:style[@w:styleId="{name}"]',ns)
        assert style.find('w:pPr/w:keepNext',ns) is not None


def test_long_history_is_split_below_visible_excel_row_height():
    original=('Строка с переносом\n'*1000)+'Ж'*70000
    chunks=cell_chunks(original)
    assert ''.join(chunks)==original
    assert all(len(chunk)<=960 and chunk.count('\n')<=16 for chunk in chunks)


def test_xlsx_headers_wrap_and_rows_have_readable_heights():
    wb=Workbook(write_only=True)
    sheet=_Sheet(wb,'История',['Рабочих часов до первого ответа','Текст'],[18,75])
    sheet.append([4,'Строка\nещё строка'])
    sheet.finish()
    data=BytesIO();wb.save(data)
    result=load_workbook(BytesIO(data.getvalue()))['История']
    assert result['A1'].alignment.wrap_text
    assert result.row_dimensions[1].height>=45
    assert result.row_dimensions[2].height>=36
