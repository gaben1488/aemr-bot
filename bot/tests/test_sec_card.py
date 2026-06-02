"""Security-тесты для services/card_format — кластер P2-3 / P3-4.

Покрывают два фикса (2026-06-02):

P2-3 (scope + cap для URL-скана карточки обращения):
  `admin_card` раньше прогонял `extract_urls` + threat-intel по ВСЕЙ
  истории `appeal.messages` синхронно на каждом рендере. Карточка с
  тысячами длинных followup'ов = подвисание event-loop (DoS-вектор) и
  рассинхрон (⛔ мог сработать по ссылке вне видимой части ленты).
  Фикс: сканируем только видимый срез (`_visible_timeline_messages`,
  последние `_TIMELINE_MAX_MESSAGES`) + cap длины конкатенации на
  `_URL_SCAN_MAX_CHARS`.

P3-4 (bare-domain threat-intel):
  `_maybe_url_warning` через `extract_urls` ловит только `http(s)://`
  (+ unicode-омоглиф quasi). Голые домены (`login-gosuslugi.top`)
  дефангались, но ⛔ threat-intel warning по ним НЕ срабатывал. Фикс:
  извлекать хосты через `url_defang._BARE_DOMAIN_PATTERN` и прогонять
  через `threat_intel.is_malicious`. Базовый ⚠️ остаётся гейтнут на
  http/quasi-URL — benign bare-domain поведение не меняется.

Чистые юнит-тесты: без БД, threat-intel мокается через monkeypatch
`get_store` (тот же приём, что в test_card_format_extra.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("maxapi", reason="нужен maxapi для config/texts/card_format")

from aemr_bot.db.models import MessageDirection  # noqa: E402
from aemr_bot.services import card_format as cf  # noqa: E402

_UTC = timezone.utc


def _msg(text: str, *, minutes_offset: int = 0,
         direction: str = MessageDirection.FROM_USER.value) -> SimpleNamespace:
    return SimpleNamespace(
        direction=direction,
        text=text,
        attachments=[],
        created_at=datetime(2026, 5, 27, 10, 0, tzinfo=_UTC)
        + timedelta(minutes=minutes_offset),
    )


def _appeal(*, summary: str = "Во дворе яма.", messages=None) -> SimpleNamespace:
    appeal = SimpleNamespace(
        id=42,
        locality="Елизовское ГП",
        address="ул. Ленина, 5",
        topic="Дороги",
        summary=summary,
        attachments=[],
        status="new",
        created_at=datetime(2026, 5, 27, 9, 0, tzinfo=_UTC),
        answered_at=None,
        closed_at=None,
    )
    # _loaded_messages / appeal.messages — оба читают __dict__ у
    # SimpleNamespace; ставим как обычный атрибут.
    appeal.messages = list(messages or [])
    return appeal


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        first_name="Сергей",
        phone="+79991234567",
        subscribed_broadcast=True,
        consent_pdn_at=datetime(2026, 5, 1, tzinfo=_UTC),
        consent_revoked_at=None,
        is_blocked=False,
    )


class _CountingStore:
    """Fake threat-intel store: помечает заданные подстроки как
    malicious и СЧИТАЕТ, сколько раз вызвали is_malicious — чтобы
    доказать, что мы не сканим всю историю целиком (P2-3)."""

    def __init__(self, bad_substrings: tuple[str, ...] = ()) -> None:
        self.bad = bad_substrings
        self.calls = 0

    def is_malicious(self, url: str) -> tuple[bool, str | None]:
        self.calls += 1
        for bad in self.bad:
            if bad in url:
                return True, "URLhaus"
        return False, None


# --------------------------------------------------------------------------
# P2-3: scope ограничен видимым срезом + cap длины
# --------------------------------------------------------------------------


class TestVisibleSliceScope:
    def test_long_history_not_scanned_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1000 сообщений: is_malicious НЕ вызывается по всей истории.

        Доказательство, что admin_card сканирует только видимый срез
        (последние `_TIMELINE_MAX_MESSAGES`), а не O(N). Без фикса
        store.is_malicious звался бы ~по каждому URL из всех 1000
        сообщений.
        """
        store = _CountingStore()
        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", lambda: store
        )
        # 1000 сообщений, в каждом — http(s)-URL (чтобы был кандидат
        # для is_malicious, если бы сканилось всё).
        messages = [
            _msg(f"ссылка https://host{i}.example/path", minutes_offset=i)
            for i in range(1000)
        ]
        appeal = _appeal(summary="Во дворе яма.", messages=messages)
        cf.admin_card(appeal, _user())

        # Видимый срез — 10 сообщений (_TIMELINE_MAX_MESSAGES), значит
        # is_malicious вызывается максимум по 10 URL (+0 из summary,
        # в ней URL нет). Жёсткая верхняя граница << 1000 фиксирует,
        # что полная история не сканится.
        assert store.calls <= cf._TIMELINE_MAX_MESSAGES + 1
        assert store.calls < 50  # с огромным запасом ниже 1000

    def test_malicious_url_outside_visible_slice_not_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⛔ warning НЕ срабатывает по вредоносной ссылке, которой
        оператор в карточке не видит (старое сообщение за пределами
        последних `_TIMELINE_MAX_MESSAGES`).

        Раньше scope warning'а (вся история) расходился со scope показа
        (видимый срез) — ⛔ кричал про ссылку, которой в карточке нет.
        """
        store = _CountingStore(bad_substrings=("malware-old.example",))
        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", lambda: store
        )
        # Самое СТАРОЕ сообщение содержит malware-URL; затем 20 свежих
        # чистых сообщений вытесняют его из видимого среза (10).
        messages = [
            _msg("https://malware-old.example/login", minutes_offset=0),
        ]
        messages += [
            _msg(f"чистое уточнение #{i}", minutes_offset=i + 1)
            for i in range(20)
        ]
        appeal = _appeal(summary="Во дворе яма.", messages=messages)
        result = cf.admin_card(appeal, _user())

        # Вредоносная ссылка вне видимого среза → ⛔ не показываем.
        assert "⛔" not in result
        assert "malware-old.example" not in result

    def test_malicious_url_inside_visible_slice_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Контроль: та же вредоносная ссылка в СВЕЖЕМ (видимом)
        сообщении — ⛔ срабатывает. Фикс не глушит реальные сигналы."""
        store = _CountingStore(bad_substrings=("phish.example",))
        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", lambda: store
        )
        messages = [
            _msg(f"чистое #{i}", minutes_offset=i) for i in range(5)
        ]
        messages.append(
            _msg("вот: https://phish.example/login", minutes_offset=100)
        )
        appeal = _appeal(summary="Во дворе яма.", messages=messages)
        result = cf.admin_card(appeal, _user())

        assert "⛔" in result
        assert "phish.example" in result
        assert "112" in result


class TestBoundedScanSource:
    def test_caps_at_max_chars(self) -> None:
        """Конкатенация обрезается до `_URL_SCAN_MAX_CHARS` — синхронный
        regex не должен жевать мегабайты."""
        # Одно гигантское сообщение (намного длиннее cap'а).
        giant = "x" * (cf._URL_SCAN_MAX_CHARS * 3)
        out = cf._bounded_scan_source("summary", [_msg(giant)])
        assert len(out) == cf._URL_SCAN_MAX_CHARS

    def test_short_source_not_truncated(self) -> None:
        out = cf._bounded_scan_source("summary", [_msg("короткий текст")])
        assert out == "summary\nкороткий текст"
        assert len(out) < cf._URL_SCAN_MAX_CHARS

    def test_giant_message_does_not_explode_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Даже с гигантским сообщением admin_card отрабатывает быстро и
        корректно (cap применён до скана)."""
        store = _CountingStore()
        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", lambda: store
        )
        giant = "нет ссылок " * (cf._URL_SCAN_MAX_CHARS // 5)
        appeal = _appeal(summary="Во дворе яма.", messages=[_msg(giant)])
        result = cf.admin_card(appeal, _user())
        # URL нет → ни ⛔ ни ⚠️. Главное — не виснет на мегабайтах.
        assert "⛔" not in result
        assert "⚠️" not in result


# --------------------------------------------------------------------------
# P3-4: bare-domain (без схемы) прогоняется через threat-intel
# --------------------------------------------------------------------------


class TestBareDomainThreatIntel:
    def test_bare_malware_domain_in_summary_triggers_block_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Голый домен `login-gosuslugi.top` (без http://) в сути →
        ⛔ warning. Раньше extract_urls его не видел, ⛔ молчал."""
        store = _CountingStore(bad_substrings=("login-gosuslugi.top",))
        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", lambda: store
        )
        appeal = _appeal(
            summary="Мне прислали login-gosuslugi.top, это правда?",
            messages=[],
        )
        result = cf.admin_card(appeal, _user())

        assert "⛔" in result
        assert "login-gosuslugi.top" in result
        assert "112" in result

    def test_bare_malware_domain_in_followup_triggers_block_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Тот же кейс, но bare-host в followup'е жителя в видимой
        части ленты."""
        store = _CountingStore(bad_substrings=("vk-id-verify.ru",))
        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", lambda: store
        )
        appeal = _appeal(
            summary="Во дворе яма.",
            messages=[
                _msg("просили зайти на vk-id-verify.ru/auth", minutes_offset=5),
            ],
        )
        result = cf.admin_card(appeal, _user())

        assert "⛔" in result
        assert "vk-id-verify.ru" in result

    def test_benign_bare_domain_no_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Поведение сохранено: безвредный голый домен (НЕ в
        threat-intel) сам по себе ⚠️ НЕ триггерит. Defang его уже
        экранировал; warning только при http(s)-URL или malware-хосте."""
        store = _CountingStore()  # ничего не malicious
        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", lambda: store
        )
        appeal = _appeal(
            summary="Посмотрите расписание на kamgov.ru пожалуйста.",
            messages=[],
        )
        result = cf.admin_card(appeal, _user())

        assert "⛔" not in result
        assert "⚠️" not in result

    def test_maybe_url_warning_bare_host_direct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Юнит на сам _maybe_url_warning: bare malware-host без схемы
        → ⛔, без http(s)-URL в тексте."""
        store = _CountingStore(bad_substrings=("phish-bank.xyz",))
        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", lambda: store
        )
        out = cf._maybe_url_warning("деньги уводят через phish-bank.xyz срочно")
        assert "⛔" in out
        assert "phish-bank.xyz" in out

    def test_maybe_url_warning_http_url_still_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Регрессия: http(s)-URL без malware → базовый ⚠️ как раньше."""
        store = _CountingStore()  # ничего не malicious
        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", lambda: store
        )
        out = cf._maybe_url_warning("ссылка https://example.com/page")
        assert "⚠️" in out
        assert "⛔" not in out

    def test_maybe_url_warning_no_link_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ни URL, ни bare-домена → пустая строка (никакого warning)."""
        store = _CountingStore()
        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", lambda: store
        )
        assert cf._maybe_url_warning("Во дворе яма, помогите.") == ""

    def test_threat_intel_broken_falls_back_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Если get_store бросает — не падаем; bare-host без http-URL
        даёт пустую строку (нет http/quasi для базового ⚠️), http-URL
        даёт ⚠️."""
        def _boom() -> object:
            raise RuntimeError("threat_intel down")

        monkeypatch.setattr(
            "aemr_bot.services.threat_intel.get_store", _boom
        )
        # bare-host без http → "" (⛔ не доказать, ⚠️ не для bare).
        assert cf._maybe_url_warning("зайди на phish-bank.xyz") == ""
        # http-URL → базовый ⚠️ даже при сломанном threat-intel.
        out = cf._maybe_url_warning("ссылка https://example.com")
        assert "⚠️" in out


# --------------------------------------------------------------------------
# P2-3: видимый срез == то, что реально рендерится
# --------------------------------------------------------------------------


class TestVisibleSliceHelper:
    def test_returns_chronological_tail(self) -> None:
        """_visible_timeline_messages возвращает последние N в
        хронологическом порядке — тот же срез, что показывает
        _render_timeline."""
        msgs = [
            _msg(f"#{i}", minutes_offset=i) for i in range(25)
        ]
        # Перемешаем порядок на входе — функция должна отсортировать.
        shuffled = msgs[::-1]
        visible = cf._visible_timeline_messages(shuffled)
        assert len(visible) == cf._TIMELINE_MAX_MESSAGES
        texts = [m.text for m in visible]
        # Последние 10 по времени: #15..#24, по возрастанию.
        assert texts == [f"#{i}" for i in range(15, 25)]

    def test_empty_returns_empty(self) -> None:
        assert cf._visible_timeline_messages([]) == []
