"""Security cluster: P1-2 (обход whitelist рассылок) + P3-3 (authz шаблонов).

Доказывает три фикса (security-review 2026-06-02):

P1-2a — confirm-gate в broadcast_wizard._handle_confirm. Free-text путь
(`_handle_wizard_text`) уже форсил URL-whitelist; пути «применить
шаблон»/«клон» минуют его, ставя state сразу в awaiting_confirm
(prefill_wizard_from_template). Поэтому confirm — единая точка отправки,
где фишинг-URL блокируется как последний рубеж: create_broadcast НЕ
зовётся, черновик сбрасывается.

P1-2b — write-time валидация в services/broadcast_templates. Шаблон с
non-gov URL нельзя СОЗДАТЬ (create_template) и нельзя СОХРАНИТЬ правку
(update_text). Клон идёт через create_template, поэтому тоже закрыт.

P3-3 — ensure_role(IT, COORDINATOR) на write-callback'ах
broadcast_templates_wizard._save_new / _save_edit. Между стартом
wizard'а и нажатием «Сохранить» роль могли понизить; save — отдельный
callback. Отказ ensure_role → ранний выход без записи в БД.

Тесты держат поведение легитимных сценариев: гос-ссылка проходит,
plain-текст проходит, оператор с ролью сохраняет шаблон.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="нужен maxapi для broadcast/handler импортов")

from tests._helpers import fake_session_scope as _fake_session_scope
from tests._helpers import make_event


# ──────────────────────────────────────────────────────────────────────
# P1-2a: confirm-gate в _handle_confirm блокирует non-gov URL на всех
#        путях (включая apply/clone, минующие free-text whitelist).
# ──────────────────────────────────────────────────────────────────────


class TestConfirmWhitelistGate:
    """`_handle_confirm` — последний рубеж. URL-whitelist форсится здесь
    независимо от того, как state попал в awaiting_confirm (набор текста,
    apply шаблона, clone-применение)."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import broadcast_wizard as mod
        mod._wizards.clear()
        yield
        mod._wizards.clear()

    @pytest.mark.asyncio
    async def test_template_with_bad_url_cannot_be_broadcast(self) -> None:
        """Сценарий «применить шаблон с non-gov URL → разослать».

        prefill_wizard_from_template (то, что делает `_apply`) ставит
        state сразу в awaiting_confirm, минуя free-text whitelist. На
        confirm gate обязан отбить: create_broadcast НЕ зовётся, state
        сброшен."""
        from aemr_bot.handlers import broadcast_wizard as mod

        # Эмулируем именно apply/clone-путь: state заряжен «из шаблона».
        mod.prefill_wizard_from_template(
            42,
            text="Скидки тут http://evil.example/phish — переходите!",
            attachments=[],
        )
        assert mod._wizards[42].step == "awaiting_confirm"

        event = make_event(user_id=42, with_callback=True)
        create_broadcast = AsyncMock()
        with patch.object(mod, "_ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "ack_callback", AsyncMock()) as ack, \
             patch.object(mod, "send_or_edit_screen", AsyncMock()) as send, \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod.broadcasts_service, "count_subscribers",
                          AsyncMock(return_value=100)), \
             patch.object(mod.broadcasts_service, "create_broadcast",
                          create_broadcast):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod._handle_confirm(event)

        # ГЛАВНОЕ: рассылка НЕ создана.
        create_broadcast.assert_not_awaited()
        # Черновик сброшен (fail-closed) — нельзя повторно нажать «Разослать».
        assert 42 not in mod._wizards
        # Оператору сообщили про сторонние сайты.
        ack.assert_awaited()
        send.assert_awaited()
        sent_text = send.await_args.kwargs.get("text", "")
        assert "сторонние" in sent_text.lower()
        assert "evil.example" in sent_text

    @pytest.mark.asyncio
    async def test_clean_gov_url_passes_confirm_and_creates_broadcast(self) -> None:
        """Легитимный сценарий: гос-ссылка проходит confirm, рассылка
        создаётся. Поведение-сохранение для нормального flow."""
        from aemr_bot.handlers import broadcast_wizard as mod

        mod.prefill_wizard_from_template(
            42,
            text="Расписание автобусов: https://elizovomr.ru/schedule",
            attachments=[],
        )
        event = make_event(user_id=42, with_callback=True, send_returns_mid=True)
        created = SimpleNamespace(id=777)
        with patch.object(mod, "_ensure_role", AsyncMock(return_value=True)), \
             patch.object(mod, "ack_callback", AsyncMock()), \
             patch.object(mod, "send_or_edit_screen",
                          AsyncMock(return_value=None)), \
             patch.object(mod, "session_scope") as scope, \
             patch.object(mod, "_get_operator",
                          AsyncMock(return_value=SimpleNamespace(id=9))), \
             patch.object(mod.broadcasts_service, "count_subscribers",
                          AsyncMock(return_value=100)), \
             patch.object(mod.broadcasts_service, "create_broadcast",
                          AsyncMock(return_value=created)) as create_broadcast, \
             patch.object(mod.operators_service, "write_audit", AsyncMock()), \
             patch("aemr_bot.utils.typing_indicator.mark_typing", AsyncMock()), \
             patch("aemr_bot.handlers.broadcast._pending_broadcasts", {}), \
             patch("aemr_bot.handlers.broadcast._run_with_cooldown",
                   MagicMock()), \
             patch.object(mod, "spawn_background_task", MagicMock()):
            scope.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            scope.return_value.__aexit__ = AsyncMock(return_value=False)
            await mod._handle_confirm(event)

        # Гос-ссылка не мешает: рассылка создана.
        create_broadcast.assert_awaited_once()


# ──────────────────────────────────────────────────────────────────────
# P1-2b: write-time валидация в services/broadcast_templates.
# ──────────────────────────────────────────────────────────────────────


class TestTemplateWriteTimeUrlValidation:
    """`create_template` / `update_text` отвергают non-gov URL на
    write-time — фишинг-ссылка не попадает в хранилище шаблонов вообще
    (и, как следствие, в clone, который идёт через create_template)."""

    @pytest.mark.asyncio
    async def test_create_template_rejects_non_gov_url(self) -> None:
        from aemr_bot.services import broadcast_templates as svc

        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        with pytest.raises(ValueError) as ei:
            await svc.create_template(
                session,
                name="Фишинг",
                text="Переходите http://evil.example/win приз ждёт",
            )
        assert "сторонние" in str(ei.value).lower()
        # Валидация — ДО session.add: ничего не записано.
        session.add.assert_not_called()
        session.flush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_template_allows_gov_url(self) -> None:
        """Легитимный сценарий: шаблон с гос-ссылкой создаётся."""
        from aemr_bot.services import broadcast_templates as svc

        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        tmpl = await svc.create_template(
            session,
            name="Расписание",
            text="Подробнее: https://elizovomr.ru/schedule",
        )
        assert tmpl.text == "Подробнее: https://elizovomr.ru/schedule"
        session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_text_rejects_non_gov_url(self) -> None:
        from aemr_bot.services import broadcast_templates as svc

        session = MagicMock()
        session.flush = AsyncMock()
        # get_by_id не должен даже зваться — валидация текста раньше.
        with patch.object(svc, "get_by_id", AsyncMock()) as get_by_id, \
             pytest.raises(ValueError) as ei:
            await svc.update_text(
                session,
                5,
                "Новый текст со ссылкой http://evil.example/phish",
            )
        assert "сторонние" in str(ei.value).lower()
        get_by_id.assert_not_awaited()
        session.flush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clone_path_blocked_via_create_template(self) -> None:
        """Clone-применение зовёт create_template с текстом источника.
        Если в источнике (legacy) затесался non-gov URL — клон не
        создастся: та же write-time валидация."""
        from aemr_bot.services import broadcast_templates as svc

        session = MagicMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        # Текст «как у клонируемого шаблона».
        with pytest.raises(ValueError):
            await svc.create_template(
                session,
                name="Копия фишинга",
                text="Срочно http://evil.example/now",
            )
        session.add.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# P3-3: ensure_role на _save_new / _save_edit.
# ──────────────────────────────────────────────────────────────────────


class TestTemplateSaveAuthz:
    """`_save_new` / `_save_edit` — write-callback'и. Без активного
    оператора нужной роли запись отбивается ДО обращения к
    templates_service (fail-closed)."""

    _WIZ = "aemr_bot.handlers.broadcast_templates_wizard"

    @pytest.fixture(autouse=True)
    def _clean(self):
        from aemr_bot.handlers import broadcast_templates as bt
        bt._wizards.clear()
        yield
        bt._wizards.clear()

    @pytest.mark.asyncio
    async def test_save_new_denied_without_role_does_not_create(self) -> None:
        """ensure_role=False (нет активного оператора нужной роли) →
        create_template НЕ зовётся, wizard НЕ тронут."""
        from aemr_bot.handlers import broadcast_templates as bt

        bt._wizards[7] = bt._TmplWizardState(
            step="new_preview",
            pending_name="X",
            pending_text="Текст",
            pending_attachments=[],
        )
        event = make_event(chat_id=123, user_id=7, with_callback=True)
        create = AsyncMock()
        with patch(f"{self._WIZ}.ensure_role",
                   AsyncMock(return_value=False)), \
             patch(f"{self._WIZ}.templates_service.create_template", create), \
             patch(f"{self._WIZ}.session_scope", _fake_session_scope), \
             patch(f"{self._WIZ}.send_or_edit_screen", AsyncMock()):
            await bt._save_new(event)
        create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_new_allowed_with_role_creates(self) -> None:
        """ensure_role=True → штатное сохранение (поведение-сохранение)."""
        from aemr_bot.handlers import broadcast_templates as bt

        bt._wizards[7] = bt._TmplWizardState(
            step="new_preview",
            pending_name="Новый",
            pending_text="Текст рассылки",
            pending_attachments=[],
        )
        event = make_event(chat_id=123, user_id=7, with_callback=True)
        create = AsyncMock(return_value=SimpleNamespace(id=11, name="Новый"))
        with patch(f"{self._WIZ}.ensure_role",
                   AsyncMock(return_value=True)), \
             patch(f"{self._WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{self._WIZ}.session_scope", _fake_session_scope), \
             patch(f"{self._WIZ}.templates_service.create_template", create), \
             patch(f"{self._WIZ}.operators_service.write_audit", AsyncMock()), \
             patch(f"{self._WIZ}.send_or_edit_screen", AsyncMock()):
            await bt._save_new(event)
        create.assert_awaited_once()
        assert 7 not in bt._wizards

    @pytest.mark.asyncio
    async def test_save_edit_denied_without_role_does_not_update(self) -> None:
        """ensure_role=False → update_text НЕ зовётся."""
        from aemr_bot.handlers import broadcast_templates as bt

        state = bt._TmplWizardState(
            step="edit_preview",
            target_id=5,
            pending_text="новый текст",
            pending_name="Док",
            pending_attachments=[],
        )
        state._edit_image_replaced = False  # type: ignore[attr-defined]
        bt._wizards[7] = state
        event = make_event(chat_id=123, user_id=7, with_callback=True)
        update = AsyncMock()
        with patch(f"{self._WIZ}.ensure_role",
                   AsyncMock(return_value=False)), \
             patch(f"{self._WIZ}.templates_service.update_text", update), \
             patch(f"{self._WIZ}.session_scope", _fake_session_scope), \
             patch(f"{self._WIZ}.send_or_edit_screen", AsyncMock()):
            await bt._save_edit(event, 5)
        update.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_edit_allowed_with_role_updates(self) -> None:
        """ensure_role=True → штатное сохранение правки."""
        from aemr_bot.handlers import broadcast_templates as bt

        state = bt._TmplWizardState(
            step="edit_preview",
            target_id=5,
            pending_text="новый текст",
            pending_name="Док",
            pending_attachments=[],
        )
        state._edit_image_replaced = False  # type: ignore[attr-defined]
        bt._wizards[7] = state
        event = make_event(chat_id=123, user_id=7, with_callback=True)
        update = AsyncMock()
        with patch(f"{self._WIZ}.ensure_role",
                   AsyncMock(return_value=True)), \
             patch(f"{self._WIZ}.get_operator",
                   AsyncMock(return_value=SimpleNamespace(id=99))), \
             patch(f"{self._WIZ}.session_scope", _fake_session_scope), \
             patch(f"{self._WIZ}.templates_service.update_text", update), \
             patch(f"{self._WIZ}.operators_service.write_audit", AsyncMock()), \
             patch(f"{self._WIZ}.send_or_edit_screen", AsyncMock()):
            await bt._save_edit(event, 5)
        update.assert_awaited_once()
        assert 7 not in bt._wizards
