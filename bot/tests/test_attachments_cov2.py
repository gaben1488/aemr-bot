"""Покрытие непокрытых веток utils/attachments.

Базовые test_attachments_helpers.py и test_extract_location.py покрывают
основные пути, но оставляют без тестов:
- extract_phone: dict-форму max_info, vcf-объект (.vcf.phone), сырой
  vcf_info с TEL-строкой, защиту VCF_INFO_MAX_CHARS (truncation).
- extract_contact_name: vcf-объект (.fn), dict-форму max_info, сырой
  vcf_info с FN-строкой.
- count_by_type: пропуск не-dict элементов.
- deserialize_for_relay: фильтр нерелейных типов, пропуск не-dict,
  успешную десериализацию image, и ветку проваленной валидации (warn+skip).
- extract_location: ветку debug-логирования типов вложений.

Все стабы — SimpleNamespace / dict, без БД и без MAX-рантайма
(deserialize_for_relay использует maxapi TypeAdapter, который доступен
в dev-окружении; при его отсутствии функция fail-open возвращает []).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace as NS

from aemr_bot.utils import attachments as A


def _msg(attachments: list) -> NS:
    return NS(attachments=attachments)


class TestAttachmentToDict:
    def test_model_dump_used_when_available(self) -> None:
        class M:
            def model_dump(self, by_alias=False):  # noqa: ARG002
                return {"type": "image", "k": "v"}

        assert A._attachment_to_dict(M()) == {"type": "image", "k": "v"}

    def test_model_dump_failure_falls_through_to_empty(self) -> None:
        class M:
            def model_dump(self, by_alias=False):  # noqa: ARG002
                raise ValueError("broken")

        # model_dump кидает, объект не dict → возвращаем {}.
        assert A._attachment_to_dict(M()) == {}

    def test_dict_passthrough(self) -> None:
        assert A._attachment_to_dict({"type": "file"}) == {"type": "file"}

    def test_unknown_object_returns_empty(self) -> None:
        assert A._attachment_to_dict(12345) == {}


class TestCollectAttachmentsDictType:
    def test_dict_attachment_type_via_get(self) -> None:
        # att без атрибута .type, но dict с ключом type → ветка 63->65.
        out = A.collect_attachments(_msg([{"type": "image", "payload": {}}]))
        assert [a.get("type") for a in out] == ["image"]


class TestExtractPhonePaths:
    def test_dict_max_info_phone_number(self) -> None:
        att = {"type": "contact", "payload": {"max_info": {"phone_number": "79991110000"}}}
        assert A.extract_phone(_msg([att])) == "79991110000"

    def test_vcf_object_phone(self) -> None:
        att = NS(type="contact", payload=NS(max_info=None, vcf=NS(phone="79993334455")))
        assert A.extract_phone(_msg([att])) == "79993334455"

    def test_raw_vcf_info_tel_line(self) -> None:
        att = {
            "type": "contact",
            "payload": {"vcf_info": "BEGIN:VCARD\r\nTEL;CELL:+79990001122\r\nEND:VCARD"},
        }
        assert A.extract_phone(_msg([att])) == "+79990001122"

    def test_vcf_info_truncated_when_too_long(self) -> None:
        # TEL-строка спрятана за пределами VCF_INFO_MAX_CHARS → после
        # обрезки не находится, возвращаем None (но без падения/зависания).
        filler = "X" * (A.VCF_INFO_MAX_CHARS + 50)
        att = {"type": "contact", "payload": {"vcf_info": filler + "\nTEL:+79990001122"}}
        assert A.extract_phone(_msg([att])) is None

    def test_payload_none_skipped(self) -> None:
        att = {"type": "contact", "payload": None}
        assert A.extract_phone(_msg([att])) is None

    def test_tel_line_without_value_ignored(self) -> None:
        att = {"type": "contact", "payload": {"vcf_info": "TEL:   \nEND"}}
        assert A.extract_phone(_msg([att])) is None


class TestExtractContactNamePaths:
    def test_vcf_object_fn(self) -> None:
        att = NS(
            type="contact",
            payload=NS(max_info=None, vcf=NS(first_name=None, name=None, fn="Сидор")),
        )
        assert A.extract_contact_name(_msg([att])) == "Сидор"

    def test_dict_max_info_name(self) -> None:
        att = {"type": "contact", "payload": {"max_info": {"name": "Анна"}}}
        assert A.extract_contact_name(_msg([att])) == "Анна"

    def test_raw_vcf_info_fn_line(self) -> None:
        att = {
            "type": "contact",
            "payload": {"vcf_info": "BEGIN:VCARD\nFN:Иван Петров\nEND:VCARD"},
        }
        assert A.extract_contact_name(_msg([att])) == "Иван Петров"

    def test_fn_with_params_semicolon(self) -> None:
        att = {"type": "contact", "payload": {"vcf_info": "FN;CHARSET=UTF-8:Пётр"}}
        assert A.extract_contact_name(_msg([att])) == "Пётр"

    def test_payload_none_skipped(self) -> None:
        att = {"type": "contact", "payload": None}
        assert A.extract_contact_name(_msg([att])) is None

    def test_no_recognizable_name_returns_none(self) -> None:
        att = {"type": "contact", "payload": {"vcf_info": "BEGIN:VCARD\nTEL:+7999\nEND"}}
        assert A.extract_contact_name(_msg([att])) is None

    def test_empty_max_info_names_fall_through_to_vcf_object(self) -> None:
        # max_info есть, но имена пустые → проходим к vcf-объекту.
        att = NS(
            type="contact",
            payload=NS(
                max_info=NS(first_name="", name=""),
                vcf=NS(first_name="Лев", name=None, fn=None),
            ),
        )
        assert A.extract_contact_name(_msg([att])) == "Лев"

    def test_body_unwrap_for_contact(self) -> None:
        # extract_contact_name спускается в .body как и остальные парсеры.
        att = {"type": "contact", "payload": {"max_info": {"first_name": "Ольга"}}}
        outer = NS(body=_msg([att]))
        assert A.extract_contact_name(outer) == "Ольга"


class TestCountByTypeSkipsNonDict:
    def test_non_dict_items_skipped(self) -> None:
        stored = [{"type": "image"}, "garbage", None, 42, {"type": "image"}]
        assert A.count_by_type(stored) == {"image": 2}

    def test_attachment_without_type_skipped(self) -> None:
        # type отсутствует → _normalize_type вернёт "" → не учитывается.
        assert A.count_by_type([{"payload": {}}]) == {}


class TestDeserializeForRelay:
    def test_empty_list(self) -> None:
        assert A.deserialize_for_relay([]) == []

    def test_filters_non_relayable_type(self) -> None:
        # contact не в RELAYABLE_TYPES → отбрасывается.
        out = A.deserialize_for_relay([{"type": "contact", "payload": {}}])
        assert out == []

    def test_skips_non_dict_entries(self) -> None:
        out = A.deserialize_for_relay(["not-a-dict", 123])
        assert out == []

    def test_valid_image_deserialized(self) -> None:
        img = {"type": "image", "payload": {"token": "abc", "url": "http://x/y.jpg"}}
        out = A.deserialize_for_relay([img])
        assert len(out) == 1
        assert type(out[0]).__name__ == "Image"

    def test_malformed_payload_skipped_not_raised(self) -> None:
        # payload неверной формы → validate_python кидает → warn + skip,
        # отправка обращения не блокируется.
        bad = {"type": "image", "payload": "should-be-object-not-string"}
        good = {"type": "image", "payload": {"token": "ok", "url": "http://x/z.jpg"}}
        out = A.deserialize_for_relay([bad, good])
        # только корректное вложение проходит
        assert len(out) == 1
        assert type(out[0]).__name__ == "Image"


class TestExtractLocationDebugLog:
    def test_debug_logging_branch_executes(self, caplog) -> None:
        # При DEBUG-уровне extract_location логирует список типов вложений
        # (без координат — PII). Покрывает ветку log.isEnabledFor(DEBUG).
        att = {"type": "location", "latitude": 53.0, "longitude": 158.0}
        with caplog.at_level(logging.DEBUG, logger="aemr_bot.utils.attachments"):
            result = A.extract_location(_msg([att]))
        assert result == (53.0, 158.0)
        assert any("attachments types" in r.message for r in caplog.records)
