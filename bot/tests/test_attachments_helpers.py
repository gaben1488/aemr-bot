"""Тесты на utils/attachments — вспомогательные парсеры вложений MAX."""
from __future__ import annotations

from types import SimpleNamespace

from aemr_bot.utils.attachments import (
    collect_attachments,
    count_by_type,
    extract_contact_name,
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
