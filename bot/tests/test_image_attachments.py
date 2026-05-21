"""Тесты `utils/image_attachments.py` — тонкая надстройка над `utils/attachments.py`.

Модуль выделяет картинки из общего потока вложений MAX-события: жителю
надо отправить ровно изображение (не файл, не голосовое), а
`utils/attachments.py:collect_attachments` отдаёт все типы из
`ALLOWED_APPEAL_TYPES = {"image","video","file"}`. Здесь — фильтр.

Контракт фиксируется тестами:
- `is_image_attachment` распознаёт image из dict и из объекта-имитации
  pydantic-attachment (поле `.type`).
- `image_attachments_from_body` / `..._from_event` отдаёт только
  картинки, по умолчанию ≤ 1 (защита от спама в рассылке).
- `build_outbound_image_attachments` пропускает только image-типы перед
  передачей в `deserialize_for_relay` (которая на maxapi-зависимости).
- `attachment_meta` отдаёт счётчик для логов/UI.

Тесты на стороне unit — реальная maxapi не нужна.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from aemr_bot.utils import image_attachments as ia


# ---- is_image_attachment ---------------------------------------------------


class TestIsImageAttachment:
    def test_dict_with_type_image(self) -> None:
        assert ia.is_image_attachment({"type": "image"}) is True

    def test_dict_with_qualified_type(self) -> None:
        # MAX иногда отдаёт `AttachmentType.IMAGE` или `image.IMAGE` —
        # _type_name берёт хвост после последней точки.
        assert ia.is_image_attachment({"type": "AttachmentType.image"}) is True

    def test_dict_with_video_type(self) -> None:
        assert ia.is_image_attachment({"type": "video"}) is False

    def test_object_with_type_attr(self) -> None:
        # имитация pydantic-attachment c .type
        att = SimpleNamespace(type="image")
        assert ia.is_image_attachment(att) is True

    def test_none(self) -> None:
        # Защита от мусора: None / без type → False, не падаем.
        assert ia.is_image_attachment(None) is False

    def test_object_without_type(self) -> None:
        # Если у объекта нет .type — _type_name берёт сам объект,
        # str() → класс, в нижнем регистре, без "image" в конце.
        assert ia.is_image_attachment(SimpleNamespace()) is False


# ---- image_attachments_from_body / _from_event -----------------------------


def _body_with(attachments: list) -> SimpleNamespace:
    return SimpleNamespace(attachments=attachments)


def _event_with(attachments: list) -> SimpleNamespace:
    return SimpleNamespace(message=SimpleNamespace(body=_body_with(attachments)))


class TestImageAttachmentsFromBody:
    def test_picks_only_images(self) -> None:
        body = _body_with([
            {"type": "image", "payload": {"url": "a.jpg"}},
            {"type": "video", "payload": {}},
            {"type": "file", "payload": {}},
        ])
        out = ia.image_attachments_from_body(body, limit=10)
        assert len(out) == 1
        assert out[0]["type"] == "image"

    def test_default_limit_is_one(self) -> None:
        # Защита: в рассылку и в ответ оператора кладём не больше
        # одной картинки по дефолту, чтобы поток MAX не плодил
        # тяжёлые multi-image сообщения.
        body = _body_with([
            {"type": "image", "payload": {"url": "a.jpg"}},
            {"type": "image", "payload": {"url": "b.jpg"}},
            {"type": "image", "payload": {"url": "c.jpg"}},
        ])
        out = ia.image_attachments_from_body(body)
        assert len(out) == 1

    def test_limit_zero_means_unlimited(self) -> None:
        # Когда явно надо все картинки (например для admin-карточки).
        body = _body_with([{"type": "image"}] * 5)
        out = ia.image_attachments_from_body(body, limit=0)
        assert len(out) == 5

    def test_empty_body(self) -> None:
        out = ia.image_attachments_from_body(_body_with([]))
        assert out == []

    def test_none_body(self) -> None:
        # collect_attachments устойчив к None — здесь сквозной тест.
        out = ia.image_attachments_from_body(None)
        assert out == []


class TestImageAttachmentsFromEvent:
    def test_event_with_message_body(self) -> None:
        event = _event_with([{"type": "image"}, {"type": "video"}])
        out = ia.image_attachments_from_event(event)
        assert len(out) == 1
        assert out[0]["type"] == "image"

    def test_event_without_message(self) -> None:
        # Защита: callback-события без message → пустой список.
        event = SimpleNamespace(callback=SimpleNamespace())
        out = ia.image_attachments_from_event(event)
        assert out == []

    def test_event_with_none_message(self) -> None:
        event = SimpleNamespace(message=None)
        out = ia.image_attachments_from_event(event)
        assert out == []


# ---- build_outbound_image_attachments --------------------------------------


class TestBuildOutboundImageAttachments:
    def test_filters_to_images_before_relay(self) -> None:
        # Контракт: даже если в storage-dict оказались video/file
        # (другая воронка их пометила), на исход уходят только image.
        stored = [
            {"type": "image", "payload": {}},
            {"type": "video", "payload": {}},
            {"type": "file", "payload": {}},
        ]
        with patch.object(ia, "deserialize_for_relay") as m:
            m.return_value = ["IMG_OBJ"]
            out = ia.build_outbound_image_attachments(stored)
        # deserialize_for_relay вызван с отфильтрованным списком
        m.assert_called_once()
        passed = m.call_args.args[0]
        assert len(passed) == 1
        assert passed[0]["type"] == "image"
        # возвращает то, что вернула maxapi-надстройка
        assert out == ["IMG_OBJ"]

    def test_none_input(self) -> None:
        with patch.object(ia, "deserialize_for_relay") as m:
            m.return_value = []
            out = ia.build_outbound_image_attachments(None)
        m.assert_called_once_with([])
        assert out == []

    def test_empty_input(self) -> None:
        with patch.object(ia, "deserialize_for_relay") as m:
            m.return_value = []
            out = ia.build_outbound_image_attachments([])
        m.assert_called_once_with([])
        assert out == []


# ---- attachment_meta -------------------------------------------------------


class TestAttachmentMeta:
    def test_count(self) -> None:
        assert ia.attachment_meta([{"type": "image"}, {"type": "image"}]) == {"images": 2}

    def test_none(self) -> None:
        assert ia.attachment_meta(None) == {"images": 0}

    def test_empty(self) -> None:
        assert ia.attachment_meta([]) == {"images": 0}
