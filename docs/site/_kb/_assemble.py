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


# Запечённый mermaid-SVG уже несёт текст-альтернативу (role="img" + aria-label) —
# это закрывает WCAG 1.1.1 для скринридера. Но видимой подписи у диаграмм нет,
# хотя класс `.diagram__cap` для неё определён в _shell.html. Достраиваем её здесь,
# на этапе сборки, чтобы покрыть все диаграммы разом (и будущие тоже), не правя
# 11 фрагментов руками. Подпись берём из того же aria-label (без хвоста
# «(диаграмма)») и помечаем aria-hidden="true": SVG уже озвучивает этот текст,
# второй раз скринридеру он не нужен — подпись чисто визуальная.
_DIAGRAM_BLOCK = re.compile(
    r'(<div class="diagram">\s*<svg\b[^>]*\baria-label="([^"]*)"[\s\S]*?</pre>\s*)(</div>)'
)


def _inject_diagram_captions(html: str) -> str:
    """Дописать видимую `.diagram__cap` к каждому блоку `.diagram` из его aria-label."""
    import html as _htmlmod

    def repl(m: "re.Match[str]") -> str:
        label = m.group(2).strip()
        # хвост «(диаграмма)» нужен скринридеру в aria-label, но в видимой подписи он лишний
        cap = re.sub(r"\s*\(диаграмма\)\s*$", "", label).strip()
        if not cap:
            return m.group(0)
        # label берём из готового атрибута (уже HTML-экранирован при запекании);
        # раскодируем сущности и заэкранируем заново, чтобы текст подписи был корректен
        cap = _htmlmod.escape(_htmlmod.unescape(cap))
        return (
            m.group(1)
            + f'<span class="diagram__cap" aria-hidden="true">{cap}</span>\n'
            + m.group(3)
        )

    return _DIAGRAM_BLOCK.sub(repl, html)


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
        # снять дубль id: верхний <h2 id="{n}"> повторяет id <section> — оставляем id только у секции,
        # иначе getElementById(n) возвращает <section> и подсветка/якоря метят не тот узел.
        inner = re.sub(r'(<h2\b[^>]*?)\s+id="' + re.escape(n) + r'"', r"\1", inner, count=1)
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

    # видимые подписи диаграмм (.diagram__cap) из их aria-label — WCAG 1.1.1
    out = _inject_diagram_captions(out)

    # guard: каждый data-go / href="#..." обязан резолвиться в существующий id
    ids = {n for n, _, _ in SECTIONS} | set(
        re.findall(r'id="([A-Za-z][\w-]*)"', out)
    )
    targets = re.findall(r'(?:data-go|href)="#?([A-Za-z][\w-]*)"', out)
    broken = sorted({t for t in targets if t not in ids})
    # guard: id каждой секции обязан быть уникален (дубль section/h2 ломает подсветку и якоря)
    dup_secs = sorted(n for n, _, _ in SECTIONS if out.count('id="' + n + '"') > 1)
    broken = sorted(set(broken) | {"dup-id:" + s for s in dup_secs})
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
