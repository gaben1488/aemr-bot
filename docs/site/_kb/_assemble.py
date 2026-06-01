# -*- coding: utf-8 -*-
"""Сборка единого index.html из каркаса _shell.html + 10 фрагментов _kb2/.
Запуск: python docs/site/_kb/_assemble.py  (после сборки фрагментов воркфлоу).
"""
import re, pathlib, sys

base = pathlib.Path(r'C:\Users\filat\Documents\aemr-bot\docs\site')
kb2 = base / '_kb2'
shell_path = base / '_kb' / '_shell.html'

# (имя файла, подпись в навигации, номер)
SECTIONS = [
    ('obzor', 'Обзор', '0'),
    ('zhitel', 'Жителю', '1'),
    ('operator', 'Оператору', '2'),
    ('admin', 'Администратору', '3'),
    ('developer', 'Разработчику', '4'),
    ('koncept', 'Как устроено', '5'),
    ('security', 'Безопасность и ПДн', '6'),
    ('pravovoe', 'Правовое', '7'),
    ('spravochnik', 'Справочник', '8'),
    ('adr', 'Журнал решений', '9'),
]
GROUPS = [
    ('Начало', ['obzor']),
    ('Людям', ['zhitel', 'operator']),
    ('Инженерам', ['admin', 'developer']),
    ('Понять и проверить', ['koncept', 'security', 'pravovoe']),
    ('Справка', ['spravochnik', 'adr']),
]
meta = {n: (label, num) for n, label, num in SECTIONS}

shell = shell_path.read_text(encoding='utf-8')

# ---- НАВИГАЦИЯ (двухуровневая, data-go = id раздела) ----
nav = []
for gname, members in GROUPS:
    nav.append(f'      <li class="nav__group">{gname}</li>')
    for n in members:
        label, num = meta[n]
        nav.append(
            f'      <li><a href="#{n}" data-go="{n}">'
            f'<span class="nav__num">{num}</span> {label}</a></li>'
        )
nav_html = '\n'.join(nav)

# ---- РАЗДЕЛЫ ----
secs, missing = [], []
for i, (n, label, num) in enumerate(SECTIONS):
    f = kb2 / f'{n}.html'
    if not f.exists():
        missing.append(n)
        continue
    inner = f.read_text(encoding='utf-8').strip()
    active = ' is-active' if i == 0 else ''
    secs.append(
        f'<section class="section{active}" id="{n}" aria-label="{label}">\n{inner}\n</section>'
    )
secs_html = '\n\n'.join(secs)

out = shell.replace('<!--NAV-->', nav_html).replace('<!--SECTIONS-->', secs_html)
# бренд-логотип ведёт на обзор
out = re.sub(r'(<a class="side__brand")(?![^>]*data-go)',
             r'\1 data-go="obzor" href="#obzor"', out, count=1)

(base / 'index.html').write_text(out, encoding='utf-8')

# guard: каждый data-go / href="#..." обязан резолвиться в существующий id
_ids = {n for n, _, _ in SECTIONS} | set(re.findall(r'id="([A-Za-z][\w-]*)"', out))
_targets = re.findall(r'(?:data-go|href)="#?([A-Za-z][\w-]*)"', out)
_broken = sorted({t for t in _targets if t not in _ids})
if _broken:
    print('!! BROKEN LINKS (нет такого id):', _broken, file=sys.stderr)

print(f'WROTE index.html: {len(out)} chars, {len(secs)} sections')
if missing:
    print('!! MISSING fragments:', missing, file=sys.stderr)
    sys.exit(2)
