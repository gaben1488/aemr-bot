# -*- coding: utf-8 -*-
"""Сборка единого index.html из каркаса _shell.html + 10 фрагментов _kb2/.

Запуск (из корня репозитория или откуда угодно):
    python docs/site/_kb/_assemble.py

Источник истины вики — фрагменты `docs/site/_kb2/*.html`. Сам `index.html`
СОБИРАЕТСЯ из них этим скриптом и руками не правится. Что синхронность не
нарушится, следит тест `bot/tests/test_wiki_in_sync.py` (CI краснеет, если
`index.html` разошёлся со сборкой) и pre-commit-хук `assemble-wiki`.
"""
import pathlib
import re
import sys

# Пути берём ОТНОСИТЕЛЬНО расположения скрипта, а не хардкодом — иначе сборка
# работает только на одной машине и не проверяется в CI. _assemble.py лежит в
# docs/site/_kb/, значит docs/site — это родитель его родителя.
_HERE = pathlib.Path(__file__).resolve()
BASE = _HERE.parents[1]            # docs/site
KB2 = BASE / "_kb2"
SHELL_PATH = BASE / "_kb" / "_shell.html"
OUT_PATH = BASE / "index.html"

# (имя файла, подпись в навигации, номер)
SECTIONS = [
    ("obzor", "Обзор", "0"),
    ("zhitel", "Жителю", "1"),
    ("operator", "Оператору", "2"),
    ("admin", "Администратору", "3"),
    ("developer", "Разработчику", "4"),
    ("koncept", "Как устроено", "5"),
    ("security", "Безопасность и ПДн", "6"),
    ("pravovoe", "Правовое", "7"),
    ("spravochnik", "Справочник", "8"),
    ("adr", "Журнал решений", "9"),
]
GROUPS = [
    ("Начало", ["obzor"]),
    ("Людям", ["zhitel", "operator"]),
    ("Инженерам", ["admin", "developer"]),
    ("Понять и проверить", ["koncept", "security", "pravovoe"]),
    ("Справка", ["spravochnik", "adr"]),
]


def build() -> tuple[str, list[str], list[str]]:
    """Собрать HTML вики из каркаса и фрагментов БЕЗ записи на диск.

    Возвращает (html, missing, broken):
    - html — собранная строка index.html;
    - missing — фрагменты `_kb2/*.html`, которых не хватает;
    - broken — `data-go`/`href="#..."`, не резолвящиеся ни в один `id`.
    Чистая функция: ничего не пишет, поэтому её безопасно звать из тестов.
    """
    meta = {n: (label, num) for n, label, num in SECTIONS}
    shell = SHELL_PATH.read_text(encoding="utf-8")

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
    nav_html = "\n".join(nav)

    # ---- РАЗДЕЛЫ ----
    secs, missing = [], []
    for i, (n, label, num) in enumerate(SECTIONS):
        f = KB2 / f"{n}.html"
        if not f.exists():
            missing.append(n)
            continue
        inner = f.read_text(encoding="utf-8").strip()
        active = " is-active" if i == 0 else ""
        secs.append(
            f'<section class="section{active}" id="{n}" aria-label="{label}">\n'
            f"{inner}\n</section>"
        )
    secs_html = "\n\n".join(secs)

    out = shell.replace("<!--NAV-->", nav_html).replace("<!--SECTIONS-->", secs_html)
    # бренд-логотип ведёт на обзор
    out = re.sub(
        r'(<a class="side__brand")(?![^>]*data-go)',
        r'\1 data-go="obzor" href="#obzor"',
        out,
        count=1,
    )

    # guard: каждый data-go / href="#..." обязан резолвиться в существующий id
    ids = {n for n, _, _ in SECTIONS} | set(
        re.findall(r'id="([A-Za-z][\w-]*)"', out)
    )
    targets = re.findall(r'(?:data-go|href)="#?([A-Za-z][\w-]*)"', out)
    broken = sorted({t for t in targets if t not in ids})
    return out, missing, broken


def main() -> None:
    out, missing, broken = build()
    # newline="\n": репозиторий держит LF (pre-commit mixed-line-ending --fix=lf),
    # поэтому пишем LF сразу, а не CRLF от Windows-дефолта write_text.
    OUT_PATH.write_text(out, encoding="utf-8", newline="\n")
    if broken:
        print("!! BROKEN LINKS (нет такого id):", broken, file=sys.stderr)
    print(f"WROTE index.html: {len(out)} chars, {len(SECTIONS) - len(missing)} sections")
    if missing:
        print("!! MISSING fragments:", missing, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
