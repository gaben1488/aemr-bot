"""Тексты wizard'а рассылок и CRUD шаблонов.

Все `OP_BROADCAST_*` и `OP_TMPL_*`. Шаги создания и редактирования
рассылки, превью, прогресс, завершение, отмена, история, список
неуспешных доставок. CRUD шаблонов: list, card, wizard создания,
переименования, редактирования, клонирования, поиска, удаления.
"""

# Broadcast / рассылка ---------------------------------------------------------

OP_BROADCAST_PROMPT = (
    "Введите текст рассылки одним сообщением. Лимит {limit} символов.\n"
    "К тексту можно приложить до {max_images} картинок в том же "
    "сообщении — они придут каждому подписчику.\n"
    "\n"
    "🛡️ Памятка по антифишингу:\n"
    "• ссылки только на гос-домены (elizovomr.ru, kamgov.ru, "
    "gosuslugi.ru, kamchatka.gov.ru) — иначе бот откажет;\n"
    "• для срочной ЧС-рассылки начните текст с маркера «[ЧС]» — "
    "cooldown сократится с 5 минут до 30 секунд;\n"
    "• после «Подтверждаю отправку» у вас будет окно отмены, "
    "перечитайте текст в превью.\n"
    "\n"
    "Если передумали — кнопка «❌ Отменить рассылку» ниже."
)
OP_BROADCAST_TOO_LONG = (
    "Текст длиннее {limit} символов (сейчас {actual}). Сократите и "
    "пришлите ещё раз."
)
OP_BROADCAST_CANCELLED_BY_USER = "Подготовка рассылки отменена."
OP_BROADCAST_NO_SUBSCRIBERS = (
    "Подписчиков нет, рассылать некому. Попробуйте позже."
)
OP_BROADCAST_PREVIEW = (
    "Предпросмотр рассылки\n"
    "· · · · · · · ·\n"
    "{text}\n"
    "· · · · · · · ·\n"
    "📷 Картинок: {image_count}{image_warning}\n"
    "Готово к отправке. Получателей: {count}."
)
# Подставляется в OP_BROADCAST_PREVIEW.image_warning, когда оператор
# приложил больше, чем broadcast_max_images. Молчаливая обрезка ломает
# UX («приложил 7, разошлось 5, никто не сказал») — показываем явно.
OP_BROADCAST_PREVIEW_TRIM_WARN = (
    " ⚠️ (приложили {provided}, разойдутся первые {limit} — лимит на одну рассылку)"
)
# Подсказка после нажатия «✏️ Изменить текст». Новое сообщение
# полностью заменяет содержимое черновика — включая ранее приложенные
# картинки. Если оператор не приложит их заново — превью обнулится.
OP_BROADCAST_EDIT_HINT = (
    "Введите новый текст рассылки одним сообщением. Лимит {limit} символов.\n"
    "⚠️ Новое сообщение полностью заменит черновик — если хотите оставить "
    "картинки, приложите их заново к новому тексту.\n"
    "Если передумали — кнопка «❌ Отменить рассылку» ниже."
)
OP_BROADCAST_STARTED = "Рассылка #{number} запущена.\nДоставлено: 0/{total}"
OP_BROADCAST_PROGRESS = (
    "Рассылка #{number}\n"
    "Доставлено: {delivered}/{total}{failed_suffix}"
)
OP_BROADCAST_FAILED_SUFFIX = " · не доставлено: {failed}"
OP_BROADCAST_DONE = (
    "Рассылка #{number} завершена.\n"
    "Доставлено: {delivered} из {total}.{failed_line}"
)
OP_BROADCAST_FAILED_LINE = "\nНе доставлено: {failed}."
OP_BROADCAST_CANCELLED = (
    "Рассылка #{number} остановлена.\n"
    "До остановки доставлено: {delivered} из {total}."
)
OP_BROADCAST_LIST_EMPTY = "Рассылок ещё не было."
OP_BROADCAST_LIST_HEADER = "📜 Недавние рассылки:\n"
OP_BROADCAST_LIST_ITEM = "#{number} · {created_at} · {status} · {delivered}/{total}"

# Карточка рассылки в истории (PR G)
OP_BROADCAST_CARD = (
    "📜 Рассылка #{number}\n"
    "Статус: {status}\n"
    "Создана: {created_at}\n"
    "Доставлено: {delivered}/{total}{failed_line}\n"
    "Картинок: {image_count}\n"
    "· · · · · · · ·\n"
    "{text}"
)
OP_BROADCAST_CARD_FAILED_LINE = "\nНе доставлено: {failed}"

OP_BROADCAST_NOT_FOUND = "Рассылка не найдена."

OP_BROADCAST_FAILED_LIST_EMPTY = (
    "📥 У рассылки #{number} нет неуспешных доставок."
)
OP_BROADCAST_FAILED_LIST_HEADER = (
    "👥 Не доставлено по рассылке #{number} (всего {count}):\n"
)
OP_BROADCAST_FAILED_LIST_ITEM = "• {name} — {error}"
OP_BROADCAST_FAILED_LIST_TRUNCATED = (
    "\n…и ещё {more} — показаны первые {limit}."
)

OP_BROADCAST_CLONE_NO_SUBSCRIBERS = (
    "Рассылка взята за основу, но подписчиков сейчас нет — отправлять "
    "некому. Дождитесь, пока кто-то подпишется."
)
OP_BROADCAST_WIZARD_EXPIRED = (
    "Ввод текста занял слишком долго, мастер закрыт. Откройте "
    "«Сделать рассылку» заново."
)


# ---------------------------------------------------------------------
# Шаблоны рассылок (PR H)
# ---------------------------------------------------------------------

OP_TMPL_LIST_EMPTY = (
    "📋 Шаблоны рассылок\n"
    "━━━━━━━━━━━━━━━━\n"
    "У вас пока нет ни одного шаблона.\n\n"
    "💡 Что это даёт:\n"
    "• Сохраните частые тексты — «Отключение воды», «Расписание "
    "автобусов», «Объявление ЧС» — один раз.\n"
    "• В следующий раз — пара тапов: открыть шаблон → «📨 Отправить "
    "как рассылку» → подтвердить.\n"
    "• К шаблону можно приложить картинки (афиша, схема). Они "
    "сохранятся и пойдут вместе с текстом.\n\n"
    "Нажмите «➕ Создать шаблон», чтобы начать."
)
OP_TMPL_LIST_HEADER = (
    "📋 Шаблоны рассылок ({count})\n"
    "━━━━━━━━━━━━━━━━\n"
    "Нажмите шаблон, чтобы открыть карточку с действиями. "
    "«➕ Создать» — добавить новый."
)

OP_TMPL_CARD = (
    "📋 Шаблон «{name}» (#{number})\n"
    "━━━━━━━━━━━━━━━━\n"
    "📅 Создан: {created_at}\n"
    "🖼 Картинок: {image_count}\n"
    "📏 Длина текста: {char_count} симв.\n"
    "📊 Применений: {use_count}{last_used_line}\n"
    "━━━━━━━━━━━━━━━━\n"
    "{text}"
)
OP_TMPL_CARD_LAST_USED = " · последний раз {when}"
OP_TMPL_CARD_NEVER_USED = " (ни разу не отправлен)"

# Wizard «создать новый шаблон»: шаг 1 — имя
OP_TMPL_NEW_NAME_PROMPT = (
    "📋 Новый шаблон рассылки\n"
    "━━━━━━━━━━━━━━━━\n"
    "🔵 Шаг 1 из 2 — название\n"
    "⚪ Шаг 2 — текст и картинки\n"
    "━━━━━━━━━━━━━━━━\n"
    "Придумайте короткое название (до {limit} символов). Оно нужно, "
    "чтобы потом быстро найти шаблон в списке.\n\n"
    "💡 Хорошие примеры:\n"
    "• «Отключение воды»\n"
    "• «Расписание на праздники»\n"
    "• «Запись на приём — переезд»\n"
    "• «Объявление ЧС: ветер»\n\n"
    "Введите название одним сообщением."
)
OP_TMPL_NAME_TOO_LONG = (
    "⚠️ Слишком длинно: {actual} симв. (лимит {limit}). "
    "Попробуйте короче — это будет название кнопки в списке."
)
OP_TMPL_NAME_EMPTY = (
    "⚠️ Пустое название. Введите хотя бы одно слово."
)
OP_TMPL_NAME_TAKEN = (
    "⚠️ Шаблон «{name}» уже существует.\n\n"
    "Варианты:\n"
    "• Введите другое название.\n"
    "• Откройте существующий шаблон в списке и нажмите "
    "«📝 Изменить текст», чтобы обновить его."
)

# Wizard «создать новый шаблон»: шаг 2 — текст (+картинки)
OP_TMPL_NEW_TEXT_PROMPT = (
    "📋 Новый шаблон «{name}»\n"
    "━━━━━━━━━━━━━━━━\n"
    "✅ Шаг 1 — название\n"
    "🔵 Шаг 2 из 2 — текст и картинки\n"
    "━━━━━━━━━━━━━━━━\n"
    "Введите текст рассылки (до {limit} символов).\n\n"
    "🖼 К этому же сообщению можно приложить картинки — афиша, схема, "
    "фото. Они сохранятся в шаблоне и будут уходить подписчикам "
    "вместе с текстом при каждом применении.\n\n"
    "💡 Советы:\n"
    "• Пишите так, будто отправляете рассылку прямо сейчас — без "
    "плейсхолдеров «{{дата}}». Если меняется только цифра, проще "
    "после применения нажать «✏️ Изменить текст» и подправить.\n"
    "• Сообщение должно быть автономным — у получателя нет контекста "
    "переписки.\n"
    "• Для отмены — нажмите «❌ Отменить»."
)
OP_TMPL_TEXT_TOO_LONG = (
    "⚠️ Слишком длинно: {actual} симв. (лимит {limit}). "
    "Сократите или разбейте на несколько рассылок."
)
OP_TMPL_CREATED = (
    "✅ Шаблон «{name}» сохранён (#{number}).\n"
    "Теперь его можно отправить как рассылку или отредактировать "
    "из карточки ниже."
)

OP_TMPL_RENAME_PROMPT = (
    "✏️ Переименование шаблона\n"
    "━━━━━━━━━━━━━━━━\n"
    "Текущее название: «{old_name}»\n\n"
    "Введите новое название (до {limit} символов).\n"
    "💡 Короткое и узнаваемое — это надпись на кнопке в списке.\n\n"
    "❌ Отменить — оставить как есть."
)
OP_TMPL_RENAMED = (
    "✅ Переименовано:\n«{old_name}» → «{new_name}»"
)

OP_TMPL_EDIT_PROMPT = (
    "📝 Редактирование шаблона «{name}»\n"
    "━━━━━━━━━━━━━━━━\n"
    "Введите новый текст рассылки (до {limit} символов).\n\n"
    "🖼 Про картинки:\n"
    "• Если приложите новые картинки — они ПОЛНОСТЬЮ заменят сохранённые.\n"
    "• Если ничего не прикладывать — старые картинки останутся.\n\n"
    "❌ Отменить — шаблон останется без изменений."
)
OP_TMPL_EDITED_TEXT_ONLY = (
    "✅ Текст шаблона «{name}» обновлён. Картинки оставлены без изменений."
)
OP_TMPL_EDITED_WITH_IMAGES = (
    "✅ Шаблон «{name}» обновлён:\n"
    "• новый текст\n"
    "• {image_count} картинок (заменены)"
)

# Превью перед сохранением (новая фича)
OP_TMPL_PREVIEW_HEADER_NEW = (
    "👀 Так это увидит подписчик\n"
    "━━━━━━━━━━━━━━━━\n"
    "Шаблон «{name}», ниже — сообщение и {image_count} картинок "
    "ровно в том виде, в котором они уйдут жителю при рассылке.\n\n"
    "Если всё ок — нажмите «✅ Сохранить».\n"
    "Хотите поправить — «↩️ Назад исправить».\n"
    "━━━━━━━━━━━━━━━━"
)
OP_TMPL_PREVIEW_HEADER_EDIT = (
    "👀 Так это увидит подписчик\n"
    "━━━━━━━━━━━━━━━━\n"
    "Обновлённый шаблон «{name}», {image_count} картинок. Если "
    "всё ок — «✅ Сохранить», иначе — «↩️ Назад исправить».\n"
    "━━━━━━━━━━━━━━━━"
)

# Поиск (новая фича)
OP_TMPL_SEARCH_PROMPT = (
    "🔍 Поиск шаблонов\n"
    "━━━━━━━━━━━━━━━━\n"
    "Введите слово или фрагмент. Бот найдёт шаблоны, в имени или "
    "тексте которых это слово встречается.\n\n"
    "Примеры запросов: «вода», «расписание», «ЧС»."
)
OP_TMPL_SEARCH_RESULTS_HEADER = (
    "🔍 Найдено по запросу «{query}»: {count}"
)
OP_TMPL_SEARCH_NOTHING_FOUND = (
    "🔍 Ничего не найдено по запросу «{query}».\n\n"
    "Попробуйте более короткий или другой фрагмент."
)

# Клонирование (новая фича)
OP_TMPL_CLONE_NAME_PROMPT = (
    "📑 Клонирование шаблона\n"
    "━━━━━━━━━━━━━━━━\n"
    "Текст и {image_count} картинок берутся из шаблона «{source_name}» "
    "за основу. Введите название НОВОГО шаблона (до {limit} симв.).\n\n"
    "💡 Чтобы отличать от исходника, добавьте уточнение в имя: «"
    "{source_name} — Заречный», «{source_name} — Микро», «{source_name} (Y)»."
)
OP_TMPL_CLONED = (
    "✅ Шаблон «{name}» создан как копия «{source_name}» (#{number}).\n"
    "Теперь его можно отправить или отредактировать."
)

OP_TMPL_DELETE_CONFIRM = (
    "🗑 Удалить шаблон «{name}»?\n\n"
    "После удаления он не будет показываться в списке. Уже отправленные "
    "на его основе рассылки не пострадают."
)
OP_TMPL_DELETED = "✅ Шаблон «{name}» удалён."

OP_TMPL_CANCELLED = "Действие отменено."
OP_TMPL_NOT_FOUND = "Шаблон не найден или уже удалён."
OP_TMPL_NO_SUBSCRIBERS = (
    "Шаблон применён, но подписчиков сейчас нет. Рассылка не запущена."
)


__all__ = [
    "OP_BROADCAST_PROMPT",
    "OP_BROADCAST_TOO_LONG",
    "OP_BROADCAST_CANCELLED_BY_USER",
    "OP_BROADCAST_NO_SUBSCRIBERS",
    "OP_BROADCAST_PREVIEW",
    "OP_BROADCAST_PREVIEW_TRIM_WARN",
    "OP_BROADCAST_EDIT_HINT",
    "OP_BROADCAST_STARTED",
    "OP_BROADCAST_PROGRESS",
    "OP_BROADCAST_FAILED_SUFFIX",
    "OP_BROADCAST_DONE",
    "OP_BROADCAST_FAILED_LINE",
    "OP_BROADCAST_CANCELLED",
    "OP_BROADCAST_LIST_EMPTY",
    "OP_BROADCAST_LIST_HEADER",
    "OP_BROADCAST_LIST_ITEM",
    "OP_BROADCAST_CARD",
    "OP_BROADCAST_CARD_FAILED_LINE",
    "OP_BROADCAST_NOT_FOUND",
    "OP_BROADCAST_FAILED_LIST_EMPTY",
    "OP_BROADCAST_FAILED_LIST_HEADER",
    "OP_BROADCAST_FAILED_LIST_ITEM",
    "OP_BROADCAST_FAILED_LIST_TRUNCATED",
    "OP_BROADCAST_CLONE_NO_SUBSCRIBERS",
    "OP_BROADCAST_WIZARD_EXPIRED",
    "OP_TMPL_LIST_EMPTY",
    "OP_TMPL_LIST_HEADER",
    "OP_TMPL_CARD",
    "OP_TMPL_CARD_LAST_USED",
    "OP_TMPL_CARD_NEVER_USED",
    "OP_TMPL_NEW_NAME_PROMPT",
    "OP_TMPL_NAME_TOO_LONG",
    "OP_TMPL_NAME_EMPTY",
    "OP_TMPL_NAME_TAKEN",
    "OP_TMPL_NEW_TEXT_PROMPT",
    "OP_TMPL_TEXT_TOO_LONG",
    "OP_TMPL_CREATED",
    "OP_TMPL_RENAME_PROMPT",
    "OP_TMPL_RENAMED",
    "OP_TMPL_EDIT_PROMPT",
    "OP_TMPL_EDITED_TEXT_ONLY",
    "OP_TMPL_EDITED_WITH_IMAGES",
    "OP_TMPL_PREVIEW_HEADER_NEW",
    "OP_TMPL_PREVIEW_HEADER_EDIT",
    "OP_TMPL_SEARCH_PROMPT",
    "OP_TMPL_SEARCH_RESULTS_HEADER",
    "OP_TMPL_SEARCH_NOTHING_FOUND",
    "OP_TMPL_CLONE_NAME_PROMPT",
    "OP_TMPL_CLONED",
    "OP_TMPL_DELETE_CONFIRM",
    "OP_TMPL_DELETED",
    "OP_TMPL_CANCELLED",
    "OP_TMPL_NOT_FOUND",
    "OP_TMPL_NO_SUBSCRIBERS",
]
