# Подписка и broadcast — дизайн-спека

**Статус:** Approved
**Дата:** 2026-05-04
**Связано с:** [ADR-001](../../ADR-001-architecture.md) §10
**Источник:** brainstorming-сессия (см. историю переписки)

## Цель

Дать жителю явную возможность подписаться на новости от Администрации Елизовского муниципального района. Дать координатору АЕМР команду `/broadcast`, по которой администрация рассылает текстовое объявление всем подписавшимся.

## Не-цели

- Шаблоны рассылок (ЧС / праздник / работы) — не делаем, оператор пишет каждый раз с нуля.
- Планирование на будущее (`/broadcast schedule "завтра в 10:00"`) — только «сейчас».
- Сегментация подписчиков по адресу или тематикам — рассылка идёт всем подписавшимся.
- Auto-validator текста рассылки против Стандарта качества — кандидат на фазу 4.

## Со стороны жителя

Подписка — явная (opt-in). Согласие на ПДн в воронке обращения остаётся отдельным механизмом для конкретного обращения; рассылка — отдельная цель обработки, отдельное согласие.

Точки входа в подписку:
- Пункт «🔔 Подписаться на новости» в подменю «Полезная информация». Если житель уже подписан — пункт показывает «🔕 Отписаться от новостей». Нажатие toggling-ит статус.
- Команды `/subscribe` и `/unsubscribe` — для тех, кто привык командами. Добавляются в `/help`.
- Inline-кнопка «🔕 Отписаться» под каждым broadcast-сообщением. Однократное нажатие отписывает с подтверждением «Подписка отключена. Вернуть — командой /subscribe.»

Текст рассылки приходит как обычное сообщение в личке с заголовком «📢 Объявление от Администрации Елизовского муниципального района» и собственно текстом. Под сообщением — кнопка отписки.

## Со стороны координатора в админ-группе

Команда `/broadcast` без аргументов запускает двухшаговый wizard:

1. Бот пишет: «Введите текст рассылки одним сообщением. Лимит 1000 символов. /cancel чтобы отменить.»
2. Координатор пишет текст.
3. Бот делает предпросмотр: показывает текст «как увидит житель», под ним строка «Разослать N подписчикам?» и две кнопки: «✅ Разослать» / «❌ Отмена». Если N == 0 — отмена с сообщением «Подписчиков нет, рассылать некому.»
4. На «✅ Разослать» создаётся запись в `broadcasts` со статусом `sending`, запускается фоновая задача через `asyncio.create_task`. В админ-группу шлётся стартовое сообщение «Рассылка #M запущена. ⏳ 0/N» с кнопкой «⛔ Экстренно остановить».
5. Фоновая задача отправляет broadcast по списку с rate-limit одно сообщение в секунду. Каждые ~5 секунд обновляет (через `bot.edit_message`) progress-сообщение в группе: счётчик доставленных, счётчик упавших.
6. По завершении: «✅ Рассылка #M завершена. Доставлено: X. Не доставлено: Y.» Кнопка «⛔ Экстренно остановить» убирается. Кнопка «⛔» в процессе работы доступна **любому оператору в группе** — на случай компрометации учётки координатора.

Команда `/broadcast list` — последние десять рассылок со статусами и счётчиками. Доступна тем же ролям, что и `/broadcast`.

Доступ — `coordinator` и `it`. `aemr`/`egp` — нет.

## Техническая модель

### Новое поле в `users`

```python
subscribed_broadcast: Mapped[bool] = mapped_column(
    Boolean, default=False, server_default="false"
)
```

Инициализация — `false`. Меняется только через `/subscribe`/`/unsubscribe`/кнопки.

### Таблица `broadcasts`

```python
class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="SET NULL")
    )
    text: Mapped[str] = mapped_column(Text)
    subscriber_count_at_start: Mapped[int]
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), default="draft", server_default="draft"
    )  # draft | sending | done | cancelled | failed
    delivered_count: Mapped[int] = mapped_column(default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    admin_message_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

### Таблица `broadcast_deliveries`

```python
class BroadcastDelivery(Base):
    __tablename__ = "broadcast_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(
        ForeignKey("broadcasts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
```

### Wizard-стейт оператора

Не в БД (оператор — не житель, у него нет `dialog_state`). Простой `dict[int, BroadcastWizardState]` в памяти процесса с TTL 5 минут — если оператор передумал и не закончил, состояние сбрасывается. На рестарте бота незавершённый wizard теряется — приемлемо, потому что оператор может просто запустить `/broadcast` заново.

## Безопасность

- Каждая рассылка пишется в `audit_log` через `operators_service.write_audit` с действием `broadcast_send`, target = `broadcast #M`, details = полный текст и счётчик подписчиков.
- Кнопка экстренной остановки доступна любому оператору в группе. Callback ставит `broadcasts.status='cancelled'`, фоновая задача проверяет флаг между каждой отправкой и останавливается.
- Тексты рассылок не логируются в stdout — только id'шники и счётчики.
- Заблокированные жителями (`is_blocked=true`) и анонимизированные через `/forget` (`first_name='Удалено'`) автоматически фильтруются из списка получателей.

## Лимиты

- Длина текста рассылки — до 1000 символов (`BROADCAST_MAX_CHARS`, env-tunable).
- Rate-limit отправки — одно сообщение в секунду (`BROADCAST_RATE_LIMIT_PER_SEC=1.0`). Это половина MAX-лимита 2 RPS, оставляет половину для обычной активности бота.
- Progress-update интервал — каждые 5 секунд (`BROADCAST_PROGRESS_UPDATE_SEC=5`).
- Wizard TTL в памяти — 300 секунд (`BROADCAST_WIZARD_TTL_SEC=300`).

## Тестирование

- Unit на `subscribe`/`unsubscribe` toggling.
- Unit на `count_subscribers` с фильтрацией заблокированных и анонимизированных.
- Unit на создание `Broadcast` и обновление счётчиков `delivered_count`/`failed_count`.
- Smoke с моком `bot.send_message`: рассылка по двум подписчикам, один отписан — два сообщения отправлены, не три.

## Файлы

Создать:
- `bot/aemr_bot/services/broadcasts.py` — service layer
- `bot/aemr_bot/handlers/broadcast.py` — wizard и `/broadcast list`
- `bot/aemr_bot/db/alembic/versions/<rev>_broadcast.py` — миграция
- `bot/tests/test_broadcast_flow.py` — тесты

Изменить:
- `bot/aemr_bot/db/models.py` — добавить `User.subscribed_broadcast`, `Broadcast`, `BroadcastDelivery`
- `bot/aemr_bot/handlers/start.py` — команды `/subscribe`, `/unsubscribe`
- `bot/aemr_bot/handlers/menu.py` — хендлер callback'ов кнопки подписки
- `bot/aemr_bot/handlers/__init__.py` — регистрация broadcast.register
- `bot/aemr_bot/keyboards.py` — кнопка подписки в `useful_info_keyboard`, кнопка отписки в broadcast-сообщении, кнопки confirm/cancel/stop
- `bot/aemr_bot/texts.py` — все строки broadcast
- `bot/aemr_bot/config.py` — `BROADCAST_MAX_CHARS`, `BROADCAST_RATE_LIMIT_PER_SEC`, `BROADCAST_PROGRESS_UPDATE_SEC`, `BROADCAST_WIZARD_TTL_SEC`
- `docs/RUNBOOK.md` — раздел «Как сделать рассылку»

## Открытые вопросы

Нет блокирующих. Возможные правки в процессе реализации зафиксирую отдельным коммитом.
