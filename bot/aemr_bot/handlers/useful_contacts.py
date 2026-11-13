"""Render public contact lists as bounded, navigable MAX screens."""
from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from aemr_bot.handlers import callback_payloads as cp
from aemr_bot.utils.text_pages import split_text_units


def render(kind: str, items: list[dict]) -> str:
    if not items:
        return ("Список контактов скоро появится." if kind == "emergency"
                else "Список диспетчерских скоро появится.")
    if kind == "dispatchers":
        return "📞 Диспетчерские автотранспорта:\n\n" + "\n\n".join(
            f"• {item.get('routes', '—')}\n  {item.get('phone', '—')}" for item in items
        )
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item.get("section") or "Прочее", []).append(item)
    blocks = ["☎️ Телефоны экстренных и аварийных служб"]
    for section, contacts in grouped.items():
        blocks.append(f"\n{section}:")
        blocks.extend(f"• {item.get('name', '—')} — {item.get('phone', '—')}" for item in contacts)
    return "\n".join(blocks)


def _page_button(kind: str, page: int, text: str):
    if kind == "emergency":
        return CallbackButton(text=text, payload=cp.info_emergency_page(page))
    return CallbackButton(text=text, payload=cp.info_dispatchers_page(page))


def screen(kind: str, text: str, page: int):
    # Prefer line boundaries; an exceptionally long line is split losslessly.
    pages: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        for fragment in split_text_units(line, 3200):
            if current and sum(2 if ord(c) > 0xFFFF else 1 for c in current + fragment) > 3200:
                pages.append(current)
                current = ""
            current += fragment
    if current or not pages:
        pages.append(current)
    page = max(0, min(page, len(pages) - 1))
    kb = InlineKeyboardBuilder()
    nav = []
    if page:
        nav.append(_page_button(kind, page - 1, "◀️ Назад"))
    if page + 1 < len(pages):
        nav.append(_page_button(kind, page + 1, "Далее ▶️"))
    if nav:
        kb.row(*nav)
    kb.row(CallbackButton(text="↩️ К полезной информации", payload="menu:useful_info"))
    kb.row(CallbackButton(text="↩️ В меню", payload="menu:main"))
    suffix = f"\n\nСтраница {page + 1}/{len(pages)}" if len(pages) > 1 else ""
    return pages[page] + suffix, kb.as_markup()
