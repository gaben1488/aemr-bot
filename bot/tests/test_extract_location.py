"""Тесты на utils.attachments.extract_location.

Покрывают все известные форматы payload location-attachment от MAX:
1. Pydantic-модель maxapi.types.attachments.location.Location —
   latitude/longitude прямо на att.
2. Dict с теми же полями.
3. Dict с lat/lon (legacy).
4. Dict с lat/lng (Google Maps стиль).
5. Nested location в payload.

Если в будущем MAX поменяет формат — здесь нужно будет добавить
вариант. До тех пор любая регрессия (например удаление try/except)
сразу здесь проявится.
"""
from __future__ import annotations

from types import SimpleNamespace

from aemr_bot.utils.attachments import extract_location


def _make_message(attachments: list) -> SimpleNamespace:
    """Имитирует event.message.body.attachments — то что передаётся
    в extract_location через get_message_body(event)."""
    return SimpleNamespace(attachments=attachments)


class TestPydanticModel:
    """maxapi.types.attachments.location.Location — это то что мы
    реально получаем из maxapi-парсера."""

    def test_real_maxapi_location(self) -> None:
        try:
            from maxapi.types.attachments.location import Location
        except ImportError:
            import pytest
            pytest.skip("maxapi не установлен локально")
        loc = Location.model_validate({
            "type": "location",
            "latitude": 53.184,
            "longitude": 158.385,
        })
        body = _make_message([loc])
        assert extract_location(body) == (53.184, 158.385)


class TestDictFormats:
    """Альтернативные форматы — на случай если какой-то клиент или
    legacy-версия maxapi шлёт dict вместо pydantic-модели."""

    def test_flat_lat_lon(self) -> None:
        att = {"type": "location", "lat": 53.184, "lon": 158.385}
        body = _make_message([att])
        assert extract_location(body) == (53.184, 158.385)

    def test_flat_latitude_longitude(self) -> None:
        att = {"type": "location", "latitude": 53.184, "longitude": 158.385}
        body = _make_message([att])
        assert extract_location(body) == (53.184, 158.385)

    def test_lat_lng_google_style(self) -> None:
        att = {"type": "location", "lat": 53.184, "lng": 158.385}
        body = _make_message([att])
        assert extract_location(body) == (53.184, 158.385)

    def test_nested_in_payload(self) -> None:
        att = {
            "type": "location",
            "payload": {"latitude": 53.184, "longitude": 158.385},
        }
        body = _make_message([att])
        assert extract_location(body) == (53.184, 158.385)

    def test_nested_location_object(self) -> None:
        att = {
            "type": "location",
            "location": {"lat": 53.184, "lng": 158.385},
        }
        body = _make_message([att])
        assert extract_location(body) == (53.184, 158.385)


class TestEdgeCases:
    def test_empty_attachments(self) -> None:
        body = _make_message([])
        assert extract_location(body) is None

    def test_no_attachments_attr(self) -> None:
        body = SimpleNamespace()
        assert extract_location(body) is None

    def test_other_attachment_type_ignored(self) -> None:
        att = {"type": "image", "lat": 1, "lon": 2}
        body = _make_message([att])
        assert extract_location(body) is None

    def test_location_without_coords(self) -> None:
        att = {"type": "location"}
        body = _make_message([att])
        assert extract_location(body) is None

    def test_location_with_invalid_coords(self) -> None:
        att = {"type": "location", "latitude": "not-a-number", "longitude": "x"}
        body = _make_message([att])
        assert extract_location(body) is None

    def test_message_body_unwrap(self) -> None:
        """Если передан Message (а не MessageBody) — extract_location
        должен спустится в .body."""
        att = {"type": "location", "latitude": 53.0, "longitude": 158.0}
        msg = SimpleNamespace(body=_make_message([att]))
        assert extract_location(msg) == (53.0, 158.0)


class TestPydanticAttributeAccess:
    """Проверяет работу с pydantic-объектами (не dict)."""

    def test_pydantic_like_object_with_latitude_attr(self) -> None:
        att = SimpleNamespace(type="location", latitude=53.184, longitude=158.385)
        body = _make_message([att])
        assert extract_location(body) == (53.184, 158.385)

    def test_pydantic_object_with_payload_subobject(self) -> None:
        payload = SimpleNamespace(latitude=53.184, longitude=158.385)
        att = SimpleNamespace(type="location", payload=payload)
        body = _make_message([att])
        assert extract_location(body) == (53.184, 158.385)
