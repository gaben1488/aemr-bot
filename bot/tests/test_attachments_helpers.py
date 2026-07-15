"""Тесты на utils/attachments — вспомогательные парсеры вложений MAX."""
from __future__ import annotations

from types import SimpleNamespace

from aemr_bot.utils.attachments import (
    collect_attachments,
    count_by_type,
    extract_contact_name,
    extract_location,
    extract_phone,
)


def _msg(attachments: list) -> SimpleNamespace:
    return SimpleNamespace(attachments=attachments)


class TestCollectAttachments:
    def test_only_image_video_file_pass(self) -> None:
        atts = [
            {"type": "image", "payload": {"url": "u1"}},
            {"type": "video", "payload": {"url": "u2"}},
            {"type": "file", "payload": {"url": "u3"}},
            {"type": "audio", "payload": {"url": "u4"}},  # игнор
            {"type": "contact", "payload": {}},  # игнор
            {"type": "location", "latitude": 1, "longitude": 2},  # игнор
        ]
        result = collect_attachments(_msg(atts))
        types = [a.get("type") for a in result]
        assert types == ["image", "video", "file"]

    def test_empty(self) -> None:
        assert collect_attachments(_msg([])) == []

    def test_unwraps_message_to_body(self) -> None:
        body = _msg([{"type": "image"}])
        outer = SimpleNamespace(body=body)
        result = collect_attachments(outer)
        assert len(result) == 1


class TestExtractPhone:
    def test_extracts_from_max_info(self) -> None:
        max_info = SimpleNamespace(phone="79991234567")
        payload = SimpleNamespace(max_info=max_info)
        att = SimpleNamespace(type="contact", payload=payload)
        assert extract_phone(_msg([att])) == "79991234567"

    def test_no_contact_returns_none(self) -> None:
        atts = [
            SimpleNamespace(type="image", payload=None),
        ]
        assert extract_phone(_msg(atts)) is None

    def test_empty_attachments(self) -> None:
        assert extract_phone(_msg([])) is None


class TestExtractContactName:
    def test_from_max_info_first_name(self) -> None:
        max_info = SimpleNamespace(first_name="Иван", last_name="Петров")
        payload = SimpleNamespace(max_info=max_info)
        att = SimpleNamespace(type="contact", payload=payload)
        result = extract_contact_name(_msg([att]))
        assert result is not None
        # имя содержится в результате
        assert "Иван" in result

    def test_no_contact(self) -> None:
        assert extract_contact_name(_msg([])) is None


class TestRawAttachmentsDescent:
    """FIX 2 (P3): все 4 парсера используют общий `_raw_attachments` для
    спуска `Update/Message → .body → .attachments`. Регресс-проверка: после
    дедупликации спуск не сломан — каждый парсер по-прежнему видит вложение
    как напрямую на message, так и завёрнутое в `.body`.
    """

    def test_collect_attachments_descends_through_body(self) -> None:
        inner = _msg([{"type": "image", "payload": {"url": "u"}}])
        outer = SimpleNamespace(body=inner)
        assert len(collect_attachments(outer)) == 1

    def test_extract_phone_descends_through_body(self) -> None:
        max_info = SimpleNamespace(phone="79990001122")
        att = SimpleNamespace(type="contact", payload=SimpleNamespace(max_info=max_info))
        inner = _msg([att])
        outer = SimpleNamespace(body=inner)
        assert extract_phone(outer) == "79990001122"

    def test_extract_contact_name_descends_through_body(self) -> None:
        max_info = SimpleNamespace(first_name="Мария")
        att = SimpleNamespace(type="contact", payload=SimpleNamespace(max_info=max_info))
        inner = _msg([att])
        outer = SimpleNamespace(body=inner)
        result = extract_contact_name(outer)
        assert result is not None and "Мария" in result

    def test_extract_location_descends_through_body(self) -> None:
        att = SimpleNamespace(type="location", latitude=53.19, longitude=158.38, payload=None)
        inner = _msg([att])
        outer = SimpleNamespace(body=inner)
        assert extract_location(outer) == (53.19, 158.38)

    def test_all_parsers_direct_message_still_work(self) -> None:
        # Без обёртки `.body` — вложение прямо на message.
        att = SimpleNamespace(type="location", latitude=1.0, longitude=2.0, payload=None)
        assert extract_location(_msg([att])) == (1.0, 2.0)


class TestCountByType:
    def test_empty(self) -> None:
        assert count_by_type([]) == {}

    def test_groups(self) -> None:
        stored = [
            {"type": "image", "payload": {}},
            {"type": "image", "payload": {}},
            {"type": "video", "payload": {}},
            {"type": "file", "payload": {}},
            {"type": "file", "payload": {}},
            {"type": "file", "payload": {}},
        ]
        result = count_by_type(stored)
        assert result == {"image": 2, "video": 1, "file": 3}

    def test_unknown_type_skipped_or_other(self) -> None:
        stored = [
            {"type": "image"},
            {"type": "audio"},  # не в нашем allowlist
        ]
        result = count_by_type(stored)
        # image учтён, audio либо в other либо skipped — главное чтоб
        # image была верно
        assert result.get("image") == 1
