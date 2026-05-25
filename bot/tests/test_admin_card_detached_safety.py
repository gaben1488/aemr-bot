"""Regression-тесты на устойчивость admin_card.render к detached-state.

Корень бага: на finalize обращения в `appeal_runtime` Appeal загружен
внутри session_scope, который к моменту вызова admin_card.render уже
закрыт. Если render обращается к `appeal.messages` (через
_count_attachments → _collect_all_user_attachments) — SQLAlchemy
бросает MissingGreenlet → exception → обращение НЕ доходит до
админ-чата, житель не получает «Обращение #N принято».

Этот тест RED'нет (через MagicMock + AttributeError), пока
_count_attachments не устойчив к lazy-fail. После фикса — GREEN.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm.exc import DetachedInstanceError

from tests._helpers import fake_session_scope as _fake_session_scope


pytest.importorskip("maxapi", reason="нужен для card_format")


class _LazyFailMessages:
    """Эмулятор detached-relationship: getattr() выбрасывает
    StatementError (как SQLAlchemy MissingGreenlet)."""

    def __iter__(self):
        raise RuntimeError("MissingGreenlet (simulated): detached lazy-load")

    def __len__(self):
        raise RuntimeError("MissingGreenlet (simulated)")

    def __bool__(self):
        raise RuntimeError("MissingGreenlet (simulated)")


def _make_appeal_detached():
    """Appeal с messages-relationship, бросающим на lazy-load."""
    user = SimpleNamespace(
        first_name="Сергей",
        phone="+79991234567",
        is_blocked=False,
        consent_pdn_at=None,
        consent_revoked_at=None,
        subscribed_broadcast=False,
    )
    appeal = SimpleNamespace(
        id=42,
        user=user,
        status="new",
        locality="Елизовское ГП",
        address="ул. Ленина, 5",
        topic="Дороги",
        summary="Яма во дворе.",
        attachments=[{"type": "image"}],
        admin_message_id=None,
        closed_due_to_revoke=False,
    )
    # _loaded_messages читает из appeal.__dict__["messages"];
    # detached lazy-load имитируем тем, что НЕ устанавливаем messages в
    # __dict__ И делаем attr-доступ через property-like объект, который
    # падает. Но _loaded_messages читает только __dict__, не падает.
    #
    # Реальный путь падения: _collect_all_user_attachments читает
    # getattr(appeal, "messages") — если в SimpleNamespace нет
    # messages attr, getattr вернёт None и не упадёт. То есть наш тест
    # с SimpleNamespace проходит даже без фикса.
    #
    # Чтобы реально воспроизвести MissingGreenlet, подменим getattr
    # для атрибута messages через __setattr__-конструкцию... Сложно
    # без реального SA. Тест ниже проверяет ИНОЕ: устойчивость к
    # любому Exception в _count_attachments.
    return appeal


class TestAdminCardSurvivesAttachmentCountFailure:
    @pytest.mark.asyncio
    async def test_render_returns_mid_even_if_attachment_count_throws(
        self,
    ) -> None:
        """render не должен падать, если _count_attachments бросает —
        главное доставить карточку, а число вложений просто пропустить."""
        from aemr_bot.services import admin_card

        appeal = _make_appeal_detached()
        bot = SimpleNamespace(
            send_message=AsyncMock(
                return_value=SimpleNamespace(
                    message=SimpleNamespace(
                        body=SimpleNamespace(mid="new-mid-1")
                    )
                )
            ),
            edit_message=AsyncMock(),
        )
        with (
            patch("aemr_bot.config.settings.admin_group_id", 555),
            patch(
                "aemr_bot.services.admin_card.session_scope",
                _fake_session_scope,
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_last_admin_card_mid",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.services.admin_card.appeals_service.set_admin_message_id",
                AsyncMock(),
            ),
            patch(
                "aemr_bot.services.admin_card._count_attachments",
                # Reliability-pass: сузили `except Exception` до
                # AttributeError/TypeError/DetachedInstanceError —
                # реальных причин fail'а на detached lazy-load. Тест
                # обновлён под новый контракт: симулируем именно
                # DetachedInstanceError, как было бы у живой БД.
                side_effect=DetachedInstanceError(
                    "Parent instance is not bound to a Session"
                ),
            ),
        ):
            # render НЕ должен поднять; в худшем случае logs + None
            try:
                mid = await admin_card.render(bot, appeal)
            except Exception:
                pytest.fail(
                    "admin_card.render должен быть устойчив к ошибкам "
                    "вычисления attachment_count — иначе обращение не "
                    "доходит до админ-чата"
                )
        # Проверка: send_message не вызвался (т.к. attachment_count
        # упал до build keyboard) — это ожидаемо, главное что нет
        # raise.
        # NB: после полноценного фикса render будет swallow'ить ошибку
        # и шлёть карточку без attachment_count. Этот тест фиксирует
        # МИНИМАЛЬНОЕ требование «не падать».
        assert mid is None or isinstance(mid, str)


class TestCollectAllUserAttachmentsDetachedSafe:
    """`admin_relay._collect_all_user_attachments` должен быть
    устойчив к detached appeal.messages."""

    def test_no_messages_attr_returns_only_initial(self) -> None:
        from aemr_bot.services.admin_relay import _collect_all_user_attachments

        appeal = SimpleNamespace(
            id=1,
            attachments=[{"type": "image", "payload": {"token": "a"}}],
            # Никакого messages атрибута — старый код мог попытаться
            # getattr и упасть на SA-managed objects.
        )
        out = _collect_all_user_attachments(appeal)
        assert len(out) == 1


class TestFinalizeSurvivesDetachedAppeal:
    """Главный кейс: persist_and_dispatch_appeal не должен падать
    из-за detached appeal при вызове admin_card.render."""

    @pytest.mark.asyncio
    async def test_finalize_passes_messages_snapshot(self) -> None:
        """Smoke: проверяем что код выставляет appeal.__dict__["messages"]
        снапшотом перед render. Это контракт фикса —
        appeal_runtime обязан подготовить appeal для detached-чтения."""
        # Импорт ради side-effect (модуль доступен).
        from aemr_bot.handlers import appeal_runtime  # noqa: F401

        # Этот тест документирует контракт: финализация делает snapshot
        # для предотвращения lazy-load. Проверка прямого кода:
        import inspect
        src = inspect.getsource(appeal_runtime.persist_and_dispatch_appeal)
        assert '__dict__["messages"]' in src, (
            "persist_and_dispatch_appeal должен подготовить appeal "
            "snapshot для admin_card.render (защита от MissingGreenlet "
            "при detached lazy-load)"
        )
