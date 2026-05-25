"""Extract Регламент.docx to plain text with structure preserved."""
import zipfile
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

NS = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
DOCX = Path(r'C:\Users\filat\Documents\aemr-bot\docs\Регламент.docx')
OUT_JSON = Path(r'C:\Users\filat\Documents\aemr-bot\docs\_extracted\reglament_raw.json')
OUT_TXT = Path(r'C:\Users\filat\Documents\aemr-bot\docs\_extracted\reglament_raw.txt')

OUT_JSON.parent.mkdir(parents=True, exist_ok=True)


def extract():
    with zipfile.ZipFile(DOCX) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    root = ET.fromstring(xml)
    body = root.find(f'{NS}body')

    items = []
    for el in body.iter():
        tag = el.tag
        if tag == f'{NS}p':
            # paragraph: collect runs (text)
            chunks = []
            for t in el.iter(f'{NS}t'):
                chunks.append(t.text or '')
            text = ''.join(chunks)
            # detect style for headings
            pPr = el.find(f'{NS}pPr')
            style = ''
            numId = None
            ilvl = None
            if pPr is not None:
                pStyle = pPr.find(f'{NS}pStyle')
                if pStyle is not None:
                    style = pStyle.get(f'{NS}val', '')
                numPr = pPr.find(f'{NS}numPr')
                if numPr is not None:
                    ni = numPr.find(f'{NS}numId')
                    il = numPr.find(f'{NS}ilvl')
                    if ni is not None:
                        numId = ni.get(f'{NS}val')
                    if il is not None:
                        ilvl = il.get(f'{NS}val')
            if text.strip() or style.startswith('Heading'):
                items.append({
                    'type': 'p',
                    'style': style,
                    'numId': numId,
                    'ilvl': ilvl,
                    'text': text,
                })
        elif tag == f'{NS}tbl':
            # table
            rows = []
            for tr in el.iter(f'{NS}tr'):
                cells = []
                for tc in tr.findall(f'{NS}tc'):
                    cell_texts = []
                    for p in tc.iter(f'{NS}p'):
                        chunks = [t.text or '' for t in p.iter(f'{NS}t')]
                        cell_texts.append(''.join(chunks))
                    cells.append(' / '.join(c for c in cell_texts if c.strip()))
                rows.append(cells)
            items.append({'type': 'table', 'rows': rows})

    # dedupe consecutive paragraphs that come from nested iteration (table cells)
    # filter out empty
    # Actually iter() will visit nested table-cell paragraphs too. Need different approach.
    return items


def extract_clean():
    """Walk body in order, distinguishing top-level paragraphs from tables."""
    with zipfile.ZipFile(DOCX) as z:
        xml = z.read('word/document.xml').decode('utf-8')
    root = ET.fromstring(xml)
    body = root.find(f'{NS}body')

    items = []

    def process_para(p):
        chunks = [t.text or '' for t in p.iter(f'{NS}t')]
        text = ''.join(chunks)
        pPr = p.find(f'{NS}pPr')
        style = ''
        numId = None
        ilvl = None
        if pPr is not None:
            pStyle = pPr.find(f'{NS}pStyle')
            if pStyle is not None:
                style = pStyle.get(f'{NS}val', '')
            numPr = pPr.find(f'{NS}numPr')
            if numPr is not None:
                ni = numPr.find(f'{NS}numId')
                il = numPr.find(f'{NS}ilvl')
                if ni is not None:
                    numId = ni.get(f'{NS}val')
                if il is not None:
                    ilvl = il.get(f'{NS}val')
        return {
            'type': 'p',
            'style': style,
            'numId': numId,
            'ilvl': ilvl,
            'text': text,
        }

    def process_table(tbl):
        rows = []
        for tr in tbl.findall(f'{NS}tr'):
            cells = []
            for tc in tr.findall(f'{NS}tc'):
                cell_paras = []
                for p in tc.findall(f'{NS}p'):
                    chunks = [t.text or '' for t in p.iter(f'{NS}t')]
                    cell_paras.append(''.join(chunks))
                cells.append('\n'.join(c for c in cell_paras))
            rows.append(cells)
        return {'type': 'table', 'rows': rows}

    for child in body:
        tag = child.tag
        if tag == f'{NS}p':
            items.append(process_para(child))
        elif tag == f'{NS}tbl':
            items.append(process_table(child))

    return items


if __name__ == '__main__':
    items = extract_clean()
    OUT_JSON.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')

    # plain text dump
    lines = []
    total_paras = 0
    total_tables = 0
    headings = 0
    for it in items:
        if it['type'] == 'p':
            total_paras += 1
            t = it['text'].rstrip()
            if it['style'].startswith('Heading') or it['style'].startswith('Title'):
                headings += 1
                lines.append(f"[{it['style']}] {t}")
            else:
                lines.append(t)
        elif it['type'] == 'table':
            total_tables += 1
            lines.append('[TABLE]')
            for row in it['rows']:
                lines.append('| ' + ' | '.join(c.replace('\n', ' / ') for c in row) + ' |')
            lines.append('[/TABLE]')
    OUT_TXT.write_text('\n'.join(lines), encoding='utf-8')

    print(f'items={len(items)}, paragraphs={total_paras}, headings={headings}, tables={total_tables}')
    print(f'chars in txt: {len(OUT_TXT.read_text(encoding="utf-8"))}')
