"""Характеризационные тесты для services/card_format.

Покрывают функции, которые не были задеты в test_card_format.py:
- `admin_followup` (followup-карточка с URL/без)
- `citizen_reply` (формальная обёртка ответа жителю)
- `user_card` + `user_appeal_timeline_block` (карточка обращения для жителя)
- `appeal_timeline_block` edge cases — hidden_count > 0, attachments в
  сообщении, defang URL для FROM_USER, FROM_OPERATOR без defang.
- `_maybe_url_warning` ветка threat-intel malicious URL (без сети,
  через monkeypatch get_store()).

Cluster #12 coverage waves (2026-05-28). Тесты — safety net для будущих
рефакторингов B1 callback-dispatcher и B2 god-objects.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from aemr_bot.db.models import MessageDirection
from aemr_bot.services import card_format as cf


# ---- Хелперы построения SimpleNamespace-фейков -----------------------------


def _msg(
    *,
    direction: str,
    text: str = "",
    created_at: datetime | None = None,
    attachments: list[dict] | None = None,
) -> SimpleNamespace:
    """Сообщение для timeline. Минимальная структура — то, что
    реально читает `_render_timeline`."""
    return SimpleNamespace(
        direction=direction,
        text=text,
        created_at=created_at or datetime(2026, 5, 27, 10, 0, tzinfo=timezone.utc),
        attachments=attachments or [],
    )


def _appeal(
    *,
    id: int = 18,
    locality: str | None = "Елизовское ГП",
    address: str | None = "ул. Ленина, 5",
    topic: str | None = "Дороги",
    summary: str | None = "Яма во дворе.",
    attachments: list[dict] | None = None,
    messages: list | None = None,
    status: str = "new",
    created_at: datetime | None = None,
    answered_at: datetime | None = None,
    closed_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        locality=locality,
        address=address,
        topic=topic,
        summary=summary,
        attachments=attachments or [],
        messages=messages or [],
        status=status,
        created_at=created_at
        or datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc),
        answered_at=answered_at,
        closed_at=closed_at,
    )


def _user(
    *,
    first_name: str | None = "Сергей",
    phone: str | None = "+79991234567",
    subscribed_broadcast: bool = True,
    consent_pdn_at: datetime | None = None,
    consent_revoked_at: datetime | None = None,
    is_blocked: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        first_name=first_name,
        phone=phone,
        subscribed_broadcast=subscribed_broadcast,
        consent_pdn_at=consent_pdn_at
        or datetime(2026, 5, 1, tzinfo=timezone.utc),
        consent_revoked_at=consent_revoked_at,
        is_blocked=is_blocked,
    )


# ---- admin_followup -----------------------------------------------------


class TestAdminFollowup:
    """Карточка дополнения от жителя для admin-чата."""

    def test_simple_text_no_url(self) -> None:
        appeal = _appeal(id=42)
        user = _user(first_name="Анна")
        text = "Уточняю: яма во дворе, ближе к подъезду."

        result = cf.admin_followup(appeal, user, text)

        # Шаблон вмещает имя, номер и текст. URL-warning не должен
        # появиться — в тексте нет ссылок.
        assert "Анна" in result
        assert "42" in result or "#42" in result
        assert "яма во дворе" in result
        assert "⚠️" not in result
        assert "⛔" not in result

    def test_text_with_url_adds_warning(self) -> None:
        appeal = _appeal(id=42)
        user = _user()
        text = "Посмотрите видео https://example.org/road.mp4"

        result = cf.admin_followup(appeal, user, text)

        # 1. URL должен пройти defang (без точки в hostname).
        assert "https://example.org" not in result
        # 2. Внизу — warning «не открывайте напрямую».
        assert "⚠️" in result
        assert "ссылк" in result.lower()


# ---- citizen_reply -----------------------------------------------------


class TestCitizenReply:
    """Формальная обёртка ответа оператора жителю в личке."""

    def test_includes_appeal_number_and_topic(self) -> None:
        appeal = _appeal(
            id=77,
            topic="Дороги",
            locality="Елизовское ГП",
            address="ул. Ленина, 5",
            created_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        )
        reply = "Ваше обращение принято. Дорога будет отремонтирована."

        result = cf.citizen_reply(appeal, reply)

        assert "#77" in result
        assert "Дороги" in result
        assert "Елизовское ГП" in result
        assert "ул. Ленина, 5" in result
        assert reply in result

    def test_none_fields_substituted_with_dash(self) -> None:
        """Защита от None в БД (после erase) — поля должны рендериться
        как `—`, без crash."""
        appeal = _appeal(
            id=99,
            topic=None,
            locality=None,
            address=None,
        )
        result = cf.citizen_reply(appeal, "Готово.")
        # Хотя бы один прочерк попадает в карточку — без crash на None.
        assert "—" in result
        assert "#99" in result


# ---- user_card + user_appeal_timeline_block ---------------------------


class TestUserCard:
    """Карточка обращения для жителя — он видит «📨 Ответ Администрации»
    и «📩 Ваше дополнение», не служебные маркеры оператора."""

    def test_status_emoji_for_new(self) -> None:
        appeal = _appeal(id=42, status="new")
        result = cf.user_card(appeal)
        assert "#42" in result
        # Статусный эмодзи для new — «🆕».
        assert "🆕" in result

    def test_timeline_uses_citizen_markers(self) -> None:
        """Лента переписки у жителя — маркеры от 2-го лица, не служебные."""
        appeal = _appeal(
            id=42,
            messages=[
                _msg(
                    direction=MessageDirection.FROM_OPERATOR.value,
                    text="Принято в работу.",
                ),
                _msg(
                    direction=MessageDirection.FROM_USER.value,
                    text="Спасибо!",
                ),
            ],
        )
        result = cf.user_card(appeal)

        assert "📨 Ответ Администрации" in result
        assert "📩 Ваше дополнение" in result
        # У жителя НЕ должно быть служебных маркеров оператора.
        assert "📨 Ответ оператора" not in result
        assert "📩 Дополнение жителя" not in result

    def test_no_timeline_when_no_messages(self) -> None:
        """Только что поданное обращение — без блока «История переписки»."""
        appeal = _appeal(id=42, messages=[])
        result = cf.user_card(appeal)
        assert "История переписки" not in result


class TestUserAppealTimelineBlock:
    """`user_appeal_timeline_block` — отдельная функция, дублирует логику
    admin-варианта с другими маркерами."""

    def test_empty_messages_returns_empty(self) -> None:
        appeal = _appeal(messages=[])
        assert cf.user_appeal_timeline_block(appeal) == ""

    def test_text_limit_is_700_for_citizen(self) -> None:
        """Житель хочет видеть полный ответ — лимит 700 char vs 400 у admin.
        Проверяем что текст 600 char проходит без обрезания «…»."""
        long_reply = "А" * 600
        appeal = _appeal(
            messages=[
                _msg(
                    direction=MessageDirection.FROM_OPERATOR.value,
                    text=long_reply,
                )
            ]
        )
        result = cf.user_appeal_timeline_block(appeal)
        # 600 char < 700 limit — обрезания нет.
        assert "…" not in result
        assert long_reply in result


# ---- appeal_timeline_block edge cases ----------------------------------


class TestAppealTimelineBlockEdges:
    """Хронологическая лента admin-карточки: edge cases которые не
    тестировались отдельно."""

    def test_hidden_count_appears_when_over_max(self) -> None:
        """При >10 сообщений рендерится «Ранее ещё N сообщений (скрыты)»."""
        base_dt = datetime(2026, 5, 27, 9, 0, tzinfo=timezone.utc)
        messages = [
            _msg(
                direction=MessageDirection.FROM_USER.value,
                text=f"msg-{i}",
                created_at=base_dt + timedelta(minutes=i),
            )
            for i in range(15)
        ]
        appeal = _appeal(messages=messages)

        result = cf.appeal_timeline_block(appeal)

        # 15 - 10 = 5 скрытых.
        assert "Ранее ещё 5" in result
        # Последние 10 — видны (msg-5 .. msg-14). msg-0 — нет.
        assert "msg-14" in result
        assert "msg-5" in result
        assert "msg-0" not in result

    def test_defang_applies_to_user_messages(self) -> None:
        """SECURITY: URL в сообщении жителя проходит defang.
        У оператора URL приходит уже whitelist'нутым — defang не нужен."""
        appeal = _appeal(
            messages=[
                _msg(
                    direction=MessageDirection.FROM_USER.value,
                    text="Смотрите: https://phish.example.com/login",
                ),
                _msg(
                    direction=MessageDirection.FROM_OPERATOR.value,
                    text="Ответ с https://gosuslugi.ru/portal",
                ),
            ]
        )
        result = cf.appeal_timeline_block(appeal)

        # URL жителя defang'нут — оригинального hostname с точкой нет.
        assert "https://phish.example.com" not in result
        # URL оператора целый (он прошёл whitelist на outgoing).
        assert "https://gosuslugi.ru/portal" in result

    def test_system_direction_falls_back_to_bullet(self) -> None:
        """Сообщение с direction='system' (или неизвестным) — маркер «•»."""
        appeal = _appeal(
            messages=[
                _msg(
                    direction=MessageDirection.SYSTEM.value,
                    text="Системное событие",
                )
            ]
        )
        result = cf.appeal_timeline_block(appeal)
        # Маркер «•» появляется в строке заголовка сообщения.
        assert "•" in result
        assert "Системное событие" in result

    def test_message_without_text_renders_placeholder(self) -> None:
        """Сообщение только с вложениями (без текста) — placeholder
        «Без текста.», чтобы строка не была пустой."""
        appeal = _appeal(
            messages=[
                _msg(
                    direction=MessageDirection.FROM_USER.value,
                    text="",
                    attachments=[{"type": "image"}],
                )
            ]
        )
        result = cf.appeal_timeline_block(appeal)
        assert "Без текста" in result
        # Вложение тоже отрисовалось как summary-line.
        assert "фото 1" in result

    def test_text_limit_400_for_admin(self) -> None:
        """admin-вариант обрезает текст до 400 char + «…»."""
        long_text = "А" * 500
        appeal = _appeal(
            messages=[
                _msg(
                    direction=MessageDirection.FROM_USER.value,
                    text=long_text,
                )
            ]
        )
        result = cf.appeal_timeline_block(appeal)
        assert "…" in result

    def test_ordering_by_created_at_ascending(self) -> None:
        """Сообщения сортируются по created_at — старое сверху, новое снизу."""
        old = datetime(2026, 5, 27, 8, 0, tzinfo=timezone.utc)
        new = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        appeal = _appeal(
            messages=[
                _msg(direction=MessageDirection.FROM_USER.value, text="new-msg", created_at=new),
                _msg(direction=MessageDirection.FROM_USER.value, text="old-msg", created_at=old),
            ]
        )
        result = cf.appeal_timeline_block(appeal)
        assert result.index("old-msg") < result.index("new-msg")


# ---- _maybe_url_warning + threat-intel --------------------------------


class TestMaybeUrlWarning:
    """Проверяем все три ветки `_maybe_url_warning`:
    1. без URL → пустая строка;
    2. URL есть, threat-intel чист → ⚠️ standard warning;
    3. URL в threat-intel базе → ⛔ enhanced warning.
    """

    def test_no_url_returns_empty(self) -> None:
        # Тестируем напрямую через подмену в admin_followup — fast path.
        appeal = _appeal()
        user = _user()
        result = cf.admin_followup(appeal, user, "Текст без ссылок.")
        assert "⚠️" not in result
        assert "⛔" not in result

    def test_threat_intel_malicious_enhanced_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Если threat-intel помечает host как malicious — warning
        усиленный, с указанием host'а и упоминанием 112."""

        class _FakeStore:
            def is_malicious(self, url: str) -> tuple[bool, str | None]:
                if "phish.example.com" in url:
                    return True, "URLhaus"
                return False, None

        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store",
            lambda: _FakeStore(),
        )
        appeal = _appeal()
        user = _user()
        text = "Прислали ссылку https://phish.example.com/login"

        result = cf.admin_followup(appeal, user, text)

        assert "⛔" in result
        assert "phish.example.com" in result
        assert "112" in result  # экстренный номер полиции

    def test_threat_intel_store_broken_falls_back_to_standard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Если get_store() кинул exception — warning должен остаться
        стандартным ⚠️, без crash."""

        def _broken_get_store():
            raise RuntimeError("threat-intel unavailable")

        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store",
            _broken_get_store,
        )
        appeal = _appeal()
        user = _user()
        text = "Любая ссылка https://example.org/path"

        result = cf.admin_followup(appeal, user, text)

        assert "⚠️" in result
        assert "⛔" not in result


# ---- admin_card + lazy-load defence ------------------------------------


class TestAdminCardLazyLoadDefence:
    """admin_card имеет try/except вокруг appeal.messages — защита от
    MissingGreenlet, когда ORM lazy-load трогает detached relationship."""

    def test_summary_with_url_warning_when_messages_detached(self) -> None:
        """Если `appeal.messages` бросает Exception при access (detached
        session) — URL warning по summary всё равно должен сработать."""

        class _DetachedAppeal:
            id = 42
            locality = "Елизово"
            address = "ул. Ленина, 5"
            topic = "Дороги"
            summary = "Пишут https://scam.example.com/win"
            attachments: list = []
            __dict__ = {}  # пустой dict — getattr на messages даёт AttributeError

            @property
            def messages(self):
                raise RuntimeError("detached session: lazy-load forbidden")

        appeal = _DetachedAppeal()
        user = _user()
        result = cf.admin_card(appeal, user)

        # URL warning должен появиться по summary, несмотря на сломанный
        # `messages`-property.
        assert "⚠️" in result or "⛔" in result

    def test_no_warning_when_summary_clean_and_messages_detached(self) -> None:
        """Чистый summary + detached messages — без warning."""

        class _DetachedAppeal:
            id = 42
            locality = "Елизово"
            address = "ул. Ленина, 5"
            topic = "Дороги"
            summary = "Дорога разбита."
            attachments: list = []
            __dict__ = {}

            @property
            def messages(self):
                raise RuntimeError("detached session")

        appeal = _DetachedAppeal()
        user = _user()
        result = cf.admin_card(appeal, user)
        assert "⚠️" not in result
        assert "⛔" not in result


# ---- attachments_summary_line edge cases -------------------------------


class TestAttachmentsSummaryLineEdges:
    def test_none_input_returns_empty(self) -> None:
        """None вместо list — без crash, пустая строка."""
        assert cf.attachments_summary_line(None) == ""

    def test_unknown_type_silently_ignored(self) -> None:
        """Тип, которого нет в _ATTACHMENT_LABELS, не ломает рендер."""
        result = cf.attachments_summary_line([
            {"type": "image"},
            {"type": "voice"},  # не в labels
            {"type": "video"},
        ])
        assert "фото 1" in result
        assert "видео 1" in result
        assert "voice" not in result.lower()

    def test_image_count_aggregates(self) -> None:
        """Несколько image-объектов суммируются в один счётчик."""
        result = cf.attachments_summary_line([
            {"type": "image"},
            {"type": "image"},
            {"type": "image"},
        ])
        assert "фото 3" in result
