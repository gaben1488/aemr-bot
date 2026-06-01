"""Coverage-добор кластера operator_reply / admin_appeal_ops / admin_panel /
admin_commands. Цель — реальные непокрытые ветки, не дубли существующих.

Существующие наборы уже плотно покрывают:
  - operator_reply: intent/dedupe/mid/_deliver/handle_* (test_handlers_operator_reply
    + test_operator_reply_characterization + closed_guard + with_image);
  - admin_appeal_ops: 100% (test_admin_appeal_ops + characterization);
  - admin_panel: show_op_menu / run_* гейты / _do_backup / _do_open_tickets пустой
    (test_admin_panel).

Чего НЕТ нигде (проверено coverage term-missing):
  1. admin_commands.register(...) — slash-команды cmd_reply / cmd_reopen /
     cmd_close / cmd_erase / cmd_setting / cmd_add_operators / cmd_stats /
     cmd_open_tickets: парсинг аргументов, int-валидация, роль-гарды,
     anonymous-user guard, self-promotion guard, маппинг reopen-исходов,
     audit-метаданные. Был 7.9% покрытия — голый register-блок.
  2. admin_panel._do_diag — агрегатор счётчиков + pulse-warn ветки +
     сборка warnings (зависших рассылок / failed-доставок). Был не покрыт.
  3. operator_reply — мелкие ветки: spoofing-маркер «🆔 №N» в НЕ-bot
     сообщении логируется и игнорируется (handler → False); swipe без user_id;
     _do_open_tickets непустой listing.

Декорированные хендлеры register(dp) недостаём напрямую — используем
_CapturingDispatcher (паттерн из test_appeal_dispatcher.py), который
сохраняет каждую команду по её имени из Command(...).commands[0].

Все service-вызовы замоканы; session_scope — fake. Локально skip без
maxapi, в CI идёт.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event

pytest.importorskip("maxapi", reason="handlers тесты требуют maxapi")


# --- capturing dispatcher для admin_commands.register -------------------------


class _CmdCapturingDispatcher:
    """Минимальный mock Dispatcher для admin_commands.register.

    register подписывает команды через `@dp.message_created(Command("name"))`.
    Декоратор вызывается с фильтром-аргументом, поэтому message_created
    принимает позиционный фильтр и возвращает декоратор, сохраняющий
    coroutine по имени команды (`Command(...).commands[0]`)."""

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def message_created(self, command_filter=None):
        # Имя команды достаём из maxapi Command.commands (['reply'] и т.п.).
        names = getattr(command_filter, "commands", None) or []
        name = names[0] if names else f"_anon_{len(self.handlers)}"

        def deco(fn):
            self.handlers[name] = fn
            return fn

        return deco


@pytest.fixture
def cmds():
    """Регистрирует admin_commands.register на mock dp, возвращает
    словарь {имя_команды: coroutine}."""
    from aemr_bot.handlers import admin_commands

    dp = _CmdCapturingDispatcher()
    admin_commands.register(dp)
    # Sanity: ключевые команды кластера зарегистрированы.
    for required in ("reply", "reopen", "close", "erase", "setting",
                     "add_operators", "stats", "open_tickets"):
        assert required in dp.handlers, f"команда /{required} не подписана"
    return dp.handlers


def _cmd_event(*, text: str, chat_id: int = 123, user_id: int = 7):
    """Событие slash-команды. chat_id по умолчанию = ADMIN_GROUP_ID (123,
    выставлен в conftest). Хендлеры читают event.message.answer и
    get_text(event) → event.message.body.text."""
    return make_event(chat_id=chat_id, user_id=user_id, text=text)


# =====================================================================
#  admin_commands.register — slash-команды
# =====================================================================


class TestCmdReply:
    @pytest.mark.asyncio
    async def test_not_admin_chat_silently_returns(self, cmds) -> None:
        """/reply вне admin-группы → тихий выход (первый гейт is_admin_chat),
        даже не доходим до ensure_operator."""
        event = _cmd_event(text="/reply 5 текст", chat_id=999)
        ensure_op = AsyncMock(return_value=True)
        with patch("aemr_bot.handlers.admin_commands._ensure_operator", ensure_op):
            await cmds["reply"](event)
        ensure_op.assert_not_called()
        event.message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_operator_blocked_before_parse(self, cmds) -> None:
        """SEC #9: defense-in-depth — не оператор в admin-группе → отбой
        ещё до парсинга, handle_command_reply не зовётся."""
        from aemr_bot.handlers import operator_reply as opr

        event = _cmd_event(text="/reply 5 текст")
        hcr = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=False)), \
             patch.object(opr, "handle_command_reply", hcr):
            await cmds["reply"](event)
        hcr.assert_not_called()

    @pytest.mark.asyncio
    async def test_too_few_parts_shows_usage(self, cmds) -> None:
        """`/reply 42` без текста (всего 2 части) → usage-подсказка."""
        event = _cmd_event(text="/reply 42")
        with patch("aemr_bot.handlers.admin_commands._is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)):
            await cmds["reply"](event)
        text = event.message.answer.call_args.args[0]
        assert "Используйте" in text and "/reply" in text

    @pytest.mark.asyncio
    async def test_non_int_appeal_id_rejected(self, cmds) -> None:
        """`/reply abc текст` → «abc — не номер обращения»."""
        event = _cmd_event(text="/reply abc привет текст")
        with patch("aemr_bot.handlers.admin_commands._is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)):
            await cmds["reply"](event)
        text = event.message.answer.call_args.args[0]
        assert "abc" in text and "не номер" in text.lower()

    @pytest.mark.asyncio
    async def test_empty_reply_text_rejected(self, cmds) -> None:
        """`/reply 42    ` (текст из пробелов после split) → «не может быть
        пустым». parts[2].strip() == ''."""
        # 3 части: ['/reply', '42', '   '] — но maxsplit=2 сохранит хвост;
        # делаем текст из табов/пробелов, чтобы strip() обнулил.
        event = _cmd_event(text="/reply 42  \t  ")
        with patch("aemr_bot.handlers.admin_commands._is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)):
            await cmds["reply"](event)
        # Либо usage (если <3 частей), либо «пустым» — оба валидны как
        # отказ. Проверяем, что доставки не было.
        answered = event.message.answer.call_args.args[0]
        assert "пуст" in answered.lower() or "Используйте" in answered

    @pytest.mark.asyncio
    async def test_valid_delegates_to_handle_command_reply(self, cmds) -> None:
        """Happy-path: `/reply 42 официальный ответ` → парсится appeal_id=42,
        текст обрезается, делегируется в operator_reply.handle_command_reply."""
        from aemr_bot.handlers import operator_reply as opr

        event = _cmd_event(text="/reply 42 официальный ответ жителю")
        hcr = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch.object(opr, "handle_command_reply", hcr):
            await cmds["reply"](event)
        hcr.assert_awaited_once()
        # позиционные: (event, appeal_id, reply_text)
        assert hcr.await_args.args[1] == 42
        assert hcr.await_args.args[2] == "официальный ответ жителю"


class TestCmdReopen:
    @pytest.mark.asyncio
    async def test_not_operator_returns(self, cmds) -> None:
        event = _cmd_event(text="/reopen 5")
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=False)):
            await cmds["reopen"](event)
        event.message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_int_arg_shows_usage(self, cmds) -> None:
        event = _cmd_event(text="/reopen xyz")
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)):
            await cmds["reopen"](event)
        text = event.message.answer.call_args.args[0]
        assert "/reopen" in text and "номер" in text.lower()

    @pytest.mark.asyncio
    async def test_reopened_writes_audit_and_maps_text(self, cmds) -> None:
        """result='reopened' → пишем audit (action='reopen') и отвечаем
        текстом OP_APPEAL_REOPENED с номером."""
        from aemr_bot import texts

        event = _cmd_event(text="/reopen 5")
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.services.appeals.reopen",
                   AsyncMock(return_value="reopened")), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["reopen"](event)
        write_audit.assert_awaited_once()
        assert write_audit.await_args.kwargs["action"] == "reopen"
        assert event.message.answer.call_args.args[0] == \
            texts.OP_APPEAL_REOPENED.format(number=5)

    @pytest.mark.asyncio
    async def test_already_open_no_audit(self, cmds) -> None:
        """result='already_open' → audit НЕ пишем, текст OP_APPEAL_ALREADY_OPEN."""
        from aemr_bot import texts

        event = _cmd_event(text="/reopen 5")
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.services.appeals.reopen",
                   AsyncMock(return_value="already_open")), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["reopen"](event)
        write_audit.assert_not_awaited()
        assert event.message.answer.call_args.args[0] == \
            texts.OP_APPEAL_ALREADY_OPEN.format(number=5)

    @pytest.mark.asyncio
    async def test_blocked_by_revoke_maps_pdn_text(self, cmds) -> None:
        """result='blocked_by_revoke' → audit НЕ пишем, текст про ПДн-гард."""
        from aemr_bot import texts

        event = _cmd_event(text="/reopen 7")
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.services.appeals.reopen",
                   AsyncMock(return_value="blocked_by_revoke")), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["reopen"](event)
        write_audit.assert_not_awaited()
        assert event.message.answer.call_args.args[0] == \
            texts.OP_APPEAL_BLOCKED_BY_REVOKE.format(number=7)


class TestCmdClose:
    @pytest.mark.asyncio
    async def test_not_operator_returns(self, cmds) -> None:
        event = _cmd_event(text="/close 5")
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=False)):
            await cmds["close"](event)
        event.message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_int_arg_shows_usage(self, cmds) -> None:
        event = _cmd_event(text="/close ???")
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)):
            await cmds["close"](event)
        text = event.message.answer.call_args.args[0]
        assert "/close" in text

    @pytest.mark.asyncio
    async def test_ok_writes_audit_and_closed_text(self, cmds) -> None:
        """close ok=True → audit (action='close'), текст OP_APPEAL_CLOSED."""
        from aemr_bot import texts

        event = _cmd_event(text="/close 5")
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.services.appeals.close",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["close"](event)
        write_audit.assert_awaited_once()
        assert write_audit.await_args.kwargs["action"] == "close"
        assert event.message.answer.call_args.args[0] == \
            texts.OP_APPEAL_CLOSED.format(number=5)

    @pytest.mark.asyncio
    async def test_not_found_no_audit(self, cmds) -> None:
        """close ok=False → audit НЕ пишем, текст OP_APPEAL_NOT_FOUND."""
        from aemr_bot import texts

        event = _cmd_event(text="/close 999")
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.services.appeals.close",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["close"](event)
        write_audit.assert_not_awaited()
        assert event.message.answer.call_args.args[0] == \
            texts.OP_APPEAL_NOT_FOUND.format(number=999)


class TestCmdErase:
    @pytest.mark.asyncio
    async def test_not_it_returns(self, cmds) -> None:
        event = _cmd_event(text="/erase max_user_id=42")
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=False)):
            await cmds["erase"](event)
        event.message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_arg_shows_usage(self, cmds) -> None:
        event = _cmd_event(text="/erase")
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)):
            await cmds["erase"](event)
        text = event.message.answer.call_args.args[0]
        assert "max_user_id=" in text and "phone=" in text

    @pytest.mark.asyncio
    async def test_bad_max_user_id_rejected(self, cmds) -> None:
        """`/erase max_user_id=notnum` → «Некорректный max_user_id»."""
        event = _cmd_event(text="/erase max_user_id=notnum")
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)):
            await cmds["erase"](event)
        text = event.message.answer.call_args.args[0]
        assert "Некорректный" in text

    @pytest.mark.asyncio
    async def test_anonymous_sentinel_protected(self, cmds) -> None:
        """152-ФЗ guard: попытка стереть ANONYMOUS_MAX_USER_ID запрещена —
        на этой записи висят обезличенные обращения, erase_pdn не зовётся."""
        from aemr_bot.db.models import ANONYMOUS_MAX_USER_ID

        event = _cmd_event(text=f"/erase max_user_id={ANONYMOUS_MAX_USER_ID}")
        erase = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.users_service.erase_pdn",
                   erase):
            await cmds["erase"](event)
        erase.assert_not_called()
        text = event.message.answer.call_args.args[0]
        assert "Запрещено" in text or "⛔" in text
        assert str(ANONYMOUS_MAX_USER_ID) in text

    @pytest.mark.asyncio
    async def test_empty_phone_rejected(self, cmds) -> None:
        """`/erase phone=` (пусто после =) → просьба указать телефон."""
        event = _cmd_event(text="/erase phone=")
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)):
            await cmds["erase"](event)
        text = event.message.answer.call_args.args[0]
        assert "телефон" in text.lower()

    @pytest.mark.asyncio
    async def test_unknown_arg_shows_usage(self, cmds) -> None:
        """`/erase garbage` (не max_user_id=/phone=) → usage."""
        event = _cmd_event(text="/erase garbage")
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)):
            await cmds["erase"](event)
        text = event.message.answer.call_args.args[0]
        assert "max_user_id=" in text and "phone=" in text

    @pytest.mark.asyncio
    async def test_erase_by_max_user_id_success_writes_audit(self, cmds) -> None:
        """`/erase max_user_id=42`, erase_pdn=True → audit (action='erase'),
        текст OP_USER_ERASED."""
        from aemr_bot import texts

        event = _cmd_event(text="/erase max_user_id=42")
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.users_service.erase_pdn",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["erase"](event)
        write_audit.assert_awaited_once()
        assert write_audit.await_args.kwargs["action"] == "erase"
        assert event.message.answer.call_args.args[0] == \
            texts.OP_USER_ERASED.format(max_user_id=42)

    @pytest.mark.asyncio
    async def test_erase_by_phone_resolves_id(self, cmds) -> None:
        """`/erase phone=+7...`, erase_pdn_by_phone возвращает разрешённый
        max_user_id → audit с этим id, текст OP_USER_ERASED."""
        from aemr_bot import texts

        event = _cmd_event(text="/erase phone=+79001234567")
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.users_service.erase_pdn_by_phone",
                   AsyncMock(return_value=777)), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["erase"](event)
        write_audit.assert_awaited_once()
        assert write_audit.await_args.kwargs["target"] == "user max_id=777"
        assert event.message.answer.call_args.args[0] == \
            texts.OP_USER_ERASED.format(max_user_id=777)

    @pytest.mark.asyncio
    async def test_erase_by_phone_not_found(self, cmds) -> None:
        """erase_pdn_by_phone вернул None → «Пользователь не найден», audit нет."""
        event = _cmd_event(text="/erase phone=+79990000000")
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.users_service.erase_pdn_by_phone",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["erase"](event)
        write_audit.assert_not_awaited()
        text = event.message.answer.call_args.args[0]
        assert "не найден" in text.lower()


class TestCmdSetting:
    @pytest.mark.asyncio
    async def test_not_it_returns(self, cmds) -> None:
        event = _cmd_event(text="/setting list")
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=False)):
            await cmds["setting"](event)
        event.message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_keys_when_no_arg(self, cmds) -> None:
        """`/setting` без аргумента → список ключей через list_keys."""
        event = _cmd_event(text="/setting")
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.settings_store.list_keys",
                   AsyncMock(return_value=["welcome_text", "topics"])):
            await cmds["setting"](event)
        text = event.message.answer.call_args.args[0]
        assert "welcome_text" in text and "topics" in text

    @pytest.mark.asyncio
    async def test_key_without_value_shows_usage(self, cmds) -> None:
        """`/setting onlykey` (1 часть после split) → usage <key> <value>."""
        event = _cmd_event(text="/setting onlykey")
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope):
            await cmds["setting"](event)
        text = event.message.answer.call_args.args[0]
        assert "Используйте" in text and "key" in text

    @pytest.mark.asyncio
    async def test_validate_failure_reports_reason(self, cmds) -> None:
        """validate вернул (False, reason) → «Настройка не обновлена: reason»,
        set_value НЕ зовётся."""
        event = _cmd_event(text='/setting topics "плохое"')
        set_value = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.settings_store.validate",
                   return_value=(False, "ожидается список")), \
             patch("aemr_bot.handlers.admin_commands.settings_store.set_value",
                   set_value):
            await cmds["setting"](event)
        set_value.assert_not_called()
        text = event.message.answer.call_args.args[0]
        assert "не обновлена" in text and "ожидается список" in text

    @pytest.mark.asyncio
    async def test_valid_json_value_persisted_with_meta_audit(self, cmds) -> None:
        """Валидное JSON-значение: set_value + audit с details kind/items.
        Для list пишется items=N, само значение в audit НЕ дублируется."""
        event = _cmd_event(text='/setting topics [1, 2, 3]')
        set_value = AsyncMock()
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.settings_store.validate",
                   return_value=(True, "")), \
             patch("aemr_bot.handlers.admin_commands.settings_store.set_value",
                   set_value), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["setting"](event)
        set_value.assert_awaited_once()
        # JSON распарсился в list → set_value получает реальный список.
        assert set_value.await_args.args[2] == [1, 2, 3]
        write_audit.assert_awaited_once()
        details = write_audit.await_args.kwargs["details"]
        assert details["kind"] == "list"
        assert details["items"] == 3

    @pytest.mark.asyncio
    async def test_int_value_audit_meta_has_kind_only(self, cmds) -> None:
        """JSON-число (не str, не list) → details содержит только kind='int',
        без chars/items (ветка, где оба isinstance ложны)."""
        event = _cmd_event(text="/setting sla_response_hours 24")
        set_value = AsyncMock()
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.settings_store.validate",
                   return_value=(True, "")), \
             patch("aemr_bot.handlers.admin_commands.settings_store.set_value",
                   set_value), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["setting"](event)
        # JSON «24» → int 24, передаётся как есть.
        assert set_value.await_args.args[2] == 24
        details = write_audit.await_args.kwargs["details"]
        assert details == {"kind": "int"}

    @pytest.mark.asyncio
    async def test_non_json_value_kept_as_string_with_chars_meta(self, cmds) -> None:
        """Невалидный JSON → значение остаётся строкой; audit details
        пишет chars=len(value)."""
        from aemr_bot import texts

        event = _cmd_event(text="/setting welcome_text Привет жителям")
        set_value = AsyncMock()
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.settings_store.validate",
                   return_value=(True, "")), \
             patch("aemr_bot.handlers.admin_commands.settings_store.set_value",
                   set_value), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["setting"](event)
        # raw_value «Привет жителям» — не JSON → строка as-is.
        assert set_value.await_args.args[2] == "Привет жителям"
        details = write_audit.await_args.kwargs["details"]
        assert details["kind"] == "str"
        assert details["chars"] == len("Привет жителям")
        assert event.message.answer.call_args.args[0] == \
            texts.OP_SETTING_UPDATED.format(key="welcome_text")


class TestCmdAddOperators:
    @pytest.mark.asyncio
    async def test_not_it_returns(self, cmds) -> None:
        event = _cmd_event(text="/add_operators\n123 it Имя")
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=False)):
            await cmds["add_operators"](event)
        event.message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_body_shows_usage(self, cmds) -> None:
        from aemr_bot import texts

        event = _cmd_event(text="/add_operators")
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)):
            await cmds["add_operators"](event)
        assert event.message.answer.call_args.args[0] == \
            texts.OP_ADD_OPERATORS_USAGE

    @pytest.mark.asyncio
    async def test_added_vs_updated_counts(self, cmds) -> None:
        """Две валидные строки: первый id новый (added), второй существует
        (updated). upsert+audit зовутся на каждую."""
        event = _cmd_event(
            text="/add_operators\n111 it Иванов Иван\n222 aemr Петров Пётр"
        )
        # get: первый None (новый), второй — существует.
        get = AsyncMock(side_effect=[None, SimpleNamespace(id=2)])
        upsert = AsyncMock()
        write_audit = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.get_user_id",
                   return_value=7), \
             patch("aemr_bot.handlers.admin_commands.operators_service.get", get), \
             patch("aemr_bot.handlers.admin_commands.operators_service.upsert",
                   upsert), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   write_audit):
            await cmds["add_operators"](event)
        assert upsert.await_count == 2
        assert write_audit.await_count == 2
        report = event.message.answer.call_args.args[0]
        assert "Добавлено: 1" in report
        assert "Обновлено: 1" in report

    @pytest.mark.asyncio
    async def test_malformed_line_collected_as_error(self, cmds) -> None:
        """Строка из <3 полей → ошибка «нужно: <max_user_id> <role> <ФИО>»,
        upsert не зовётся для неё."""
        event = _cmd_event(text="/add_operators\n111 it")  # нет ФИО
        upsert = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.get_user_id",
                   return_value=7), \
             patch("aemr_bot.handlers.admin_commands.operators_service.upsert",
                   upsert):
            await cmds["add_operators"](event)
        upsert.assert_not_called()
        report = event.message.answer.call_args.args[0]
        assert "Ошибок: 1" in report
        assert "нужно" in report.lower()

    @pytest.mark.asyncio
    async def test_non_numeric_id_collected_as_error(self, cmds) -> None:
        """max_user_id не число → ошибка «max_user_id не число»."""
        event = _cmd_event(text="/add_operators\nabc it Иванов Иван")
        upsert = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.get_user_id",
                   return_value=7), \
             patch("aemr_bot.handlers.admin_commands.operators_service.upsert",
                   upsert):
            await cmds["add_operators"](event)
        upsert.assert_not_called()
        report = event.message.answer.call_args.args[0]
        assert "не число" in report

    @pytest.mark.asyncio
    async def test_unknown_role_collected_as_error(self, cmds) -> None:
        """Неизвестная роль → ошибка с перечнем доступных ролей."""
        event = _cmd_event(text="/add_operators\n111 superuser Иванов Иван")
        upsert = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.get_user_id",
                   return_value=7), \
             patch("aemr_bot.handlers.admin_commands.operators_service.upsert",
                   upsert):
            await cmds["add_operators"](event)
        upsert.assert_not_called()
        report = event.message.answer.call_args.args[0]
        assert "неизвестна" in report.lower()

    @pytest.mark.asyncio
    async def test_self_promotion_blocked(self, cmds) -> None:
        """Глубокая защита: target_id == actor_id → нельзя менять свою роль,
        upsert не зовётся."""
        event = _cmd_event(text="/add_operators\n7 it Я Сам")
        upsert = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.get_user_id",
                   return_value=7), \
             patch("aemr_bot.handlers.admin_commands.operators_service.upsert",
                   upsert):
            await cmds["add_operators"](event)
        upsert.assert_not_called()
        report = event.message.answer.call_args.args[0]
        assert "свою роль" in report

    @pytest.mark.asyncio
    async def test_comment_and_blank_lines_skipped(self, cmds) -> None:
        """Пустые строки и строки-комментарии (#) пропускаются без ошибок."""
        event = _cmd_event(
            text="/add_operators\n# заголовок\n\n111 it Иванов Иван"
        )
        get = AsyncMock(return_value=None)
        upsert = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_commands.get_user_id",
                   return_value=7), \
             patch("aemr_bot.handlers.admin_commands.operators_service.get", get), \
             patch("aemr_bot.handlers.admin_commands.operators_service.upsert",
                   upsert), \
             patch("aemr_bot.handlers.admin_commands.operators_service.write_audit",
                   AsyncMock()):
            await cmds["add_operators"](event)
        # Только одна валидная строка обработана.
        assert upsert.await_count == 1
        report = event.message.answer.call_args.args[0]
        assert "Добавлено: 1" in report
        assert "Ошибок: 0" in report


class TestCmdStats:
    @pytest.mark.asyncio
    async def test_not_operator_returns(self, cmds) -> None:
        event = _cmd_event(text="/stats today")
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=False)):
            await cmds["stats"](event)
        event.message.answer.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_period_shows_usage(self, cmds) -> None:
        """Неизвестный период → usage-строка с перечнем периодов,
        _send_stats_xlsx не зовётся."""
        event = _cmd_event(text="/stats decade")
        send_xlsx = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands._send_stats_xlsx", send_xlsx):
            await cmds["stats"](event)
        send_xlsx.assert_not_called()
        text = event.message.answer.call_args.args[0]
        assert "today" in text and "week" in text

    @pytest.mark.asyncio
    async def test_valid_period_delegates(self, cmds) -> None:
        """Валидный период → делегируется в _send_stats_xlsx(event, period)."""
        event = _cmd_event(text="/stats week")
        send_xlsx = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands._send_stats_xlsx", send_xlsx):
            await cmds["stats"](event)
        send_xlsx.assert_awaited_once()
        assert send_xlsx.await_args.args[1] == "week"

    @pytest.mark.asyncio
    async def test_default_period_today(self, cmds) -> None:
        """`/stats` без аргумента → период по умолчанию 'today'."""
        event = _cmd_event(text="/stats")
        send_xlsx = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands._send_stats_xlsx", send_xlsx):
            await cmds["stats"](event)
        send_xlsx.assert_awaited_once()
        assert send_xlsx.await_args.args[1] == "today"


class TestCmdOpenTicketsAndGuards:
    @pytest.mark.asyncio
    async def test_open_tickets_not_operator_returns(self, cmds) -> None:
        event = _cmd_event(text="/open_tickets")
        do_open = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.admin_commands._do_open_tickets", do_open):
            await cmds["open_tickets"](event)
        do_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_open_tickets_operator_delegates(self, cmds) -> None:
        event = _cmd_event(text="/open_tickets")
        do_open = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands._do_open_tickets", do_open):
            await cmds["open_tickets"](event)
        do_open.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_op_help_outside_admin_chat_returns(self, cmds) -> None:
        """/op_help вне admin-чата → тихий выход, show_op_menu не зовётся."""
        event = _cmd_event(text="/op_help", chat_id=999)
        show_menu = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._is_admin_chat",
                   return_value=False), \
             patch("aemr_bot.handlers.admin_commands.show_op_menu", show_menu):
            await cmds["op_help"](event)
        show_menu.assert_not_called()

    @pytest.mark.asyncio
    async def test_op_help_in_admin_chat_pins_menu(self, cmds) -> None:
        """/op_help в admin-чате → show_op_menu(event, pin=True)."""
        event = _cmd_event(text="/op_help")
        show_menu = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._is_admin_chat",
                   return_value=True), \
             patch("aemr_bot.handlers.admin_commands.show_op_menu", show_menu):
            await cmds["op_help"](event)
        show_menu.assert_awaited_once()
        assert show_menu.await_args.kwargs.get("pin") is True

    @pytest.mark.asyncio
    async def test_diag_not_operator_returns(self, cmds) -> None:
        event = _cmd_event(text="/diag")
        do_diag = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.admin_commands._do_diag", do_diag):
            await cmds["diag"](event)
        do_diag.assert_not_called()

    @pytest.mark.asyncio
    async def test_diag_operator_delegates(self, cmds) -> None:
        """/diag оператором → делегируется в _do_diag(event)."""
        event = _cmd_event(text="/diag")
        do_diag = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_operator",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands._do_diag", do_diag):
            await cmds["diag"](event)
        do_diag.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_backup_not_it_returns(self, cmds) -> None:
        event = _cmd_event(text="/backup")
        do_backup = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=False)), \
             patch("aemr_bot.handlers.admin_commands._do_backup", do_backup):
            await cmds["backup"](event)
        do_backup.assert_not_called()

    @pytest.mark.asyncio
    async def test_backup_it_delegates(self, cmds) -> None:
        """/backup ролью IT → делегируется в _do_backup(event)."""
        event = _cmd_event(text="/backup")
        do_backup = AsyncMock()
        with patch("aemr_bot.handlers.admin_commands._ensure_role",
                   AsyncMock(return_value=True)), \
             patch("aemr_bot.handlers.admin_commands._do_backup", do_backup):
            await cmds["backup"](event)
        do_backup.assert_awaited_once()


# =====================================================================
#  admin_panel._do_diag — агрегатор счётчиков и pulse-warn
# =====================================================================


def _diag_event(*, user_id: int = 7):
    event = make_event(chat_id=555, user_id=user_id)
    return event


def _diag_rows(*, last_event, users=None, appeals=None, broadcasts=None):
    """Собирает 4 Row-подобных namespace для _fetch_row и 3 скаляра.

    _do_diag читает атрибуты .total/.active/.blocked/.new_24h (users),
    .total/.in_progress/.new_24h (appeals), .done/.failed/.count_24h/.stuck
    (broadcasts), .total/.last_at (events). Возвращаем готовый
    side_effect-список под asyncio.gather в порядке вызова."""
    users = users or SimpleNamespace(total=10, active=8, blocked=1, new_24h=2)
    appeals = appeals or SimpleNamespace(total=5, in_progress=3, new_24h=1)
    broadcasts = broadcasts or SimpleNamespace(
        done=4, failed=0, count_24h=1, stuck=0
    )
    events_row = SimpleNamespace(total=100, last_at=last_event)
    return users, appeals, broadcasts, events_row


class TestDoDiag:
    @pytest.mark.asyncio
    async def test_healthy_pulse_no_warnings(self) -> None:
        """Свежий last_event (минуту назад), нет зависших рассылок и
        failed-доставок → блок «✅ Аномалий не обнаружено», pulse с ✅."""
        from aemr_bot.handlers import admin_panel

        event = _diag_event()
        now = datetime.now(timezone.utc)
        users, appeals, broadcasts, events_row = _diag_rows(
            last_event=now - timedelta(minutes=2)
        )

        async def fake_gather(*coros, **kw):
            # Закрываем корутины, чтобы не было RuntimeWarning, и отдаём
            # детерминированные значения в порядке вызова в _do_diag.
            for c in coros:
                c.close()
            return (users, appeals, broadcasts, events_row, 5, 0, 50)

        with patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_panel.mark_typing", AsyncMock()), \
             patch("aemr_bot.handlers.admin_panel.asyncio.gather", fake_gather):
            await admin_panel._do_diag(event)

        body = event.bot.send_message.call_args.kwargs["text"]
        assert "Диагностика" in body
        assert "✅ Аномалий не обнаружено" in body
        # счётчик ответов оператора за 24ч = 5 (replies scalar).
        assert "Ответов оператора за 24ч: 5" in body
        # pulse зелёный (минуты < 15).
        assert "✅" in body

    @pytest.mark.asyncio
    async def test_no_events_triggers_pulse_warning(self) -> None:
        """last_event=None → pulse_warn, блок «Внимание» с предупреждением
        про молчащий cron."""
        from aemr_bot.handlers import admin_panel

        event = _diag_event()
        users, appeals, broadcasts, events_row = _diag_rows(last_event=None)

        async def fake_gather(*coros, **kw):
            for c in coros:
                c.close()
            return (users, appeals, broadcasts, events_row, 0, 0, 0)

        with patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_panel.mark_typing", AsyncMock()), \
             patch("aemr_bot.handlers.admin_panel.asyncio.gather", fake_gather):
            await admin_panel._do_diag(event)

        body = event.bot.send_message.call_args.kwargs["text"]
        assert "событий нет вовсе" in body
        assert "Внимание" in body
        assert "Pulse" in body

    @pytest.mark.asyncio
    async def test_stale_pulse_and_stuck_and_failed_warnings(self) -> None:
        """Старый last_event (>15 мин), зависшие SENDING и >=20 failed-
        доставок → три предупреждения в блоке «Внимание», pulse с ⚠️."""
        from aemr_bot.handlers import admin_panel

        event = _diag_event()
        now = datetime.now(timezone.utc)
        # last_event час назад → minutes_ago > 15 → pulse_warn.
        users, appeals, broadcasts, events_row = _diag_rows(
            last_event=now - timedelta(hours=1, minutes=5),
            broadcasts=SimpleNamespace(done=2, failed=1, count_24h=3, stuck=2),
        )

        async def fake_gather(*coros, **kw):
            for c in coros:
                c.close()
            # replies=0, delivery_failed_24h=25 (>=20), subscribers=80.
            return (users, appeals, broadcasts, events_row, 0, 25, 80)

        with patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_panel.mark_typing", AsyncMock()), \
             patch("aemr_bot.handlers.admin_panel.asyncio.gather", fake_gather):
            await admin_panel._do_diag(event)

        body = event.bot.send_message.call_args.kwargs["text"]
        assert "Внимание" in body
        # pulse стрелка часами назад → ⚠️.
        assert "⚠️" in body
        # зависшие рассылки.
        assert "Зависших" in body and "2" in body
        # failed-доставки.
        assert "25" in body
        # «Аномалий не обнаружено» НЕ должно быть.
        assert "Аномалий не обнаружено" not in body

    @pytest.mark.asyncio
    async def test_pulse_hours_format_branch(self) -> None:
        """last_event >60 мин назад → формат «N ч M мин назад» (ветка hours)."""
        from aemr_bot.handlers import admin_panel

        event = _diag_event()
        now = datetime.now(timezone.utc)
        users, appeals, broadcasts, events_row = _diag_rows(
            last_event=now - timedelta(hours=2, minutes=10)
        )

        async def fake_gather(*coros, **kw):
            for c in coros:
                c.close()
            return (users, appeals, broadcasts, events_row, 1, 0, 10)

        with patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_panel.mark_typing", AsyncMock()), \
             patch("aemr_bot.handlers.admin_panel.asyncio.gather", fake_gather):
            await admin_panel._do_diag(event)

        body = event.bot.send_message.call_args.kwargs["text"]
        assert " ч " in body and "мин назад" in body

    @pytest.mark.asyncio
    async def test_real_closures_aggregate_via_gather(self) -> None:
        """Прогон БЕЗ мока asyncio.gather: реальные _fetch_row/_fetch_scalar/
        _fetch_subscribers исполняются. Мокаем только нижний слой —
        session.execute (→ Row через .one()), session.scalar и
        broadcasts_service.count_subscribers. Закрывает ветки сбора
        счётчиков, которые при моке gather не исполнялись."""
        from aemr_bot.handlers import admin_panel

        event = _diag_event()
        now = datetime.now(timezone.utc)
        users, appeals, broadcasts, events_row = _diag_rows(
            last_event=now - timedelta(minutes=3)
        )
        # execute(...).one() вызывается для users/appeals/broadcasts/events
        # в порядке gather. Каждый execute даёт объект с .one().
        row_iter = iter([users, appeals, broadcasts, events_row])

        class _ExecResult:
            def __init__(self, row):
                self._row = row

            def one(self):
                return self._row

        async def fake_execute(query):
            return _ExecResult(next(row_iter))

        # scalar() для replies_24h и delivery_failed_24h.
        scalar_iter = iter([7, 0])

        async def fake_scalar(query):
            return next(scalar_iter)

        session = MagicMock()
        session.execute = fake_execute
        session.scalar = fake_scalar

        @asynccontextmanager
        async def fake_scope():
            yield session

        with patch("aemr_bot.handlers.admin_panel.session_scope", fake_scope), \
             patch("aemr_bot.handlers.admin_panel.mark_typing", AsyncMock()), \
             patch("aemr_bot.handlers.admin_panel.broadcasts_service."
                   "count_subscribers", AsyncMock(return_value=42)):
            await admin_panel._do_diag(event)

        body = event.bot.send_message.call_args.kwargs["text"]
        assert "Диагностика" in body
        assert "Получателей рассылки: 42" in body
        assert "Ответов оператора за 24ч: 7" in body

    @pytest.mark.asyncio
    async def test_naive_last_event_is_tz_normalized(self) -> None:
        """last_event без tzinfo (naive) не должен ронять расчёт — код
        доклеивает UTC. Берём naive «минуту назад» → pulse зелёный."""
        from aemr_bot.handlers import admin_panel

        event = _diag_event()
        # Naive datetime «минуту назад» без deprecated utcnow():
        # берём aware-UTC и срезаем tzinfo, имитируя naive из БД.
        naive_recent = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).replace(tzinfo=None)
        users, appeals, broadcasts, events_row = _diag_rows(
            last_event=naive_recent
        )

        async def fake_gather(*coros, **kw):
            for c in coros:
                c.close()
            return (users, appeals, broadcasts, events_row, 3, 0, 20)

        with patch("aemr_bot.handlers.admin_panel.session_scope",
                   _fake_session_scope), \
             patch("aemr_bot.handlers.admin_panel.mark_typing", AsyncMock()), \
             patch("aemr_bot.handlers.admin_panel.asyncio.gather", fake_gather):
            # Не должно бросить (naive vs aware вычитание).
            await admin_panel._do_diag(event)

        body = event.bot.send_message.call_args.kwargs["text"]
        assert "Диагностика" in body


# =====================================================================
#  admin_panel._do_backup — категоризированные fail_kind ветки
# =====================================================================


class TestDoBackupFailKinds:
    @pytest.mark.asyncio
    async def test_config_fail_kind_message(self) -> None:
        """fail_kind='config' → сообщение про пустой BACKUP_LOCAL_DIR и .env.
        Эта ветка не покрыта существующим test_admin_panel (там только
        pg_dump/gpg/exception/success)."""
        from aemr_bot.handlers import admin_panel
        from aemr_bot.services.db_backup import BackupResult

        event = make_event(chat_id=555, user_id=7)
        fail = BackupResult(
            path=None, fail_kind="config",
            fail_detail="BACKUP_LOCAL_DIR пуст",
        )
        with patch("aemr_bot.services.db_backup.backup_db",
                   AsyncMock(return_value=fail)):
            await admin_panel._do_backup(event)
        last_text = event.bot.send_message.call_args_list[-1].kwargs["text"]
        assert "BACKUP_LOCAL_DIR" in last_text
        assert ".env" in last_text

    @pytest.mark.asyncio
    async def test_unknown_fail_kind_message(self) -> None:
        """fail_kind вне известных ('unknown') → общая ветка else с
        упоминанием логов docker compose."""
        from aemr_bot.handlers import admin_panel
        from aemr_bot.services.db_backup import BackupResult

        event = make_event(chat_id=555, user_id=7)
        fail = BackupResult(
            path=None, fail_kind="unknown",
            fail_detail="RuntimeError: что-то странное",
        )
        with patch("aemr_bot.services.db_backup.backup_db",
                   AsyncMock(return_value=fail)):
            await admin_panel._do_backup(event)
        last_text = event.bot.send_message.call_args_list[-1].kwargs["text"]
        assert "unknown" in last_text
        assert "docker compose logs" in last_text


# =====================================================================
#  admin_panel._do_open_tickets — непустой listing
# =====================================================================


class TestDoOpenTicketsNonEmpty:
    @pytest.mark.asyncio
    async def test_listing_with_appeals_builds_keyboard_items(self) -> None:
        """Непустой список открытых обращений → одно сообщение с
        заголовком «Открытые обращения (N)» и клавиатурой-listing.
        Длинная тема обрезается до 40 символов с многоточием."""
        from aemr_bot.handlers import admin_panel

        event = _diag_event()
        long_topic = "Очень длинная тема обращения которая точно превышает лимит"
        appeals = [
            SimpleNamespace(id=1, status="new", topic="Дороги", summary=None),
            SimpleNamespace(id=2, status="in_progress", topic=long_topic,
                            summary=None),
            # topic пустой → fallback на summary.
            SimpleNamespace(id=3, status="new", topic=None, summary="яма у дома"),
        ]
        scalars_result = MagicMock()
        scalars_result.all = MagicMock(return_value=appeals)
        session = MagicMock()
        session.scalars = AsyncMock(return_value=scalars_result)

        @asynccontextmanager
        async def fake_scope():
            yield session

        listing_kbd = MagicMock()
        with patch("aemr_bot.handlers.admin_panel.session_scope", fake_scope), \
             patch("aemr_bot.handlers.admin_panel.mark_typing", AsyncMock()), \
             patch("aemr_bot.handlers.admin_panel.kbds.open_tickets_listing_keyboard",
                   listing_kbd) as kbd:
            await admin_panel._do_open_tickets(event)

        text = event.bot.send_message.call_args.kwargs["text"]
        assert "Открытые обращения (3)" in text
        # keyboard получил 3 элемента (id, status, preview).
        kbd.assert_called_once()
        items = kbd.call_args.args[0]
        assert len(items) == 3
        # Длинная тема обрезана с «…».
        long_preview = next(p for (i, s, p) in items if i == 2)
        assert long_preview.endswith("…")
        assert len(long_preview) <= 40
        # topic=None → summary использован.
        summary_preview = next(p for (i, s, p) in items if i == 3)
        assert summary_preview == "яма у дома"


# =====================================================================
#  operator_reply — мелкие непокрытые ветки
# =====================================================================


class TestOperatorReplySwipeEdges:
    @pytest.mark.asyncio
    async def test_spoofed_marker_in_non_bot_message_logs_and_ignores(self) -> None:
        """SEC #3 лог-ветка: маркер «🆔 №N» присутствует, но автор реплая НЕ
        бот → отдельная ветка с log.warning про spoofing; target_mid тоже
        нет → handler возвращает False, ничего не доставляет."""
        from aemr_bot.handlers import operator_reply as opr
        from aemr_bot.services import wizard_registry as _wr

        _wr._reply_intent.clear()
        event = make_event(chat_id=100, user_id=7, with_edit_message=True)
        # link без type=reply (target_mid=None), bot=False, маркер в тексте.
        event.message.link = SimpleNamespace(
            type="forward",
            message=SimpleNamespace(
                mid=None,
                text="Обсуждаю 🆔 №321 с коллегой",
                sender=SimpleNamespace(is_bot=False),
            ),
        )
        deliver = AsyncMock(return_value=True)
        with patch("aemr_bot.handlers.operator_reply._deliver_operator_reply",
                   deliver), \
             patch("aemr_bot.handlers.operator_reply.log") as mock_log:
            result = await opr.handle_operator_reply(
                event, body=None, text="спуфинг-попытка"
            )
        assert result is False
        deliver.assert_not_called()
        # Сработала именно warning-ветка про игнор маркера в не-bot.
        assert mock_log.warning.called

    @pytest.mark.asyncio
    async def test_swipe_with_target_mid_but_no_user_id_returns_false(self) -> None:
        """Валидная ссылка-ответ (target_mid есть), но get_user_id вернул
        None (нет sender id) → handler возвращает False до работы с БД."""
        from aemr_bot.handlers import operator_reply as opr
        from aemr_bot.services import wizard_registry as _wr

        _wr._reply_intent.clear()
        event = make_event(chat_id=100, user_id=7, with_edit_message=True)
        event.message.link = SimpleNamespace(
            type="reply", message=SimpleNamespace(mid="MID-9")
        )
        get_op = AsyncMock()
        # get_user_id None и в consume_reply_intent (author None → skip),
        # и в основной проверке author_id is None → False.
        with patch("aemr_bot.handlers.operator_reply.get_user_id",
                   return_value=None), \
             patch("aemr_bot.handlers.operator_reply.operators_service.get",
                   get_op):
            result = await opr.handle_operator_reply(
                event, body=None, text="ответ"
            )
        assert result is False
        # До запроса оператора не дошли (author_id None → ранний return).
        get_op.assert_not_called()
