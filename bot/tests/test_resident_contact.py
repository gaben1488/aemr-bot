"""Resident contact ingestion against the installed MAX SDK contract."""
from types import SimpleNamespace

import pytest
from maxapi.types.attachments import Contact, ContactAttachmentPayload

from aemr_bot.utils.attachments import VCF_INFO_MAX_CHARS, extract_resident_contact


def contact(*, name=None, last_name=None, user_id=7, vcard="", as_dict=False):
    payload = ContactAttachmentPayload(
        vcf_info=vcard,
        max_info={"user_id": user_id, "first_name": name, "last_name": last_name, "is_bot": False}
        if name is not None else None,
    )
    attachment = Contact(type="contact", payload=payload)
    return attachment.model_dump() if as_dict else attachment


def read(*attachments):
    return extract_resident_contact(SimpleNamespace(attachments=list(attachments)), 7)


@pytest.mark.parametrize("as_dict", [False, True])
def test_sdk_contact_structured_name_overrides_nickname(as_dict):
    card = "BEGIN:VCARD\r\nN:Иванов;Иван;Иванович;;\r\nFN:Иванов Иван Иванович\r\nTEL:+79991112233\r\nEND:VCARD"
    assert read(contact(name="Котик77", vcard=card, as_dict=as_dict)) == (
        "+79991112233", "Иван Иванович Иванов", False,
    )


@pytest.mark.parametrize("as_dict", [False, True])
def test_foreign_contact_imports_neither_phone_nor_name(as_dict):
    assert read(contact(name="Иван", user_id=8, vcard="TEL:+79991112233", as_dict=as_dict)) == (
        None, None, True,
    )


def test_matching_profile_name_with_surname():
    assert read(contact(name="Иван", last_name="Иванов", vcard="TEL:+79991112233")) == (
        "+79991112233", "Иван Иванов", False,
    )


def test_missing_id_allows_self_declared_phone_and_valid_fn():
    assert read(contact(vcard="FN:Иван Иванов\nTEL:+79991112233")) == (
        "+79991112233", "Иван Иванов", False,
    )


def test_nickname_candidate_can_fall_through_to_valid_fn():
    assert read(contact(name="Котик77", vcard="FN:Иван Иванов\nTEL:+79991112233"))[1] == "Иван Иванов"


@pytest.mark.parametrize("name", ["Котик77", "Ivan", "Ивaн", "Беспаленко Никнейм Лариса", "👍"])
def test_invalid_name_never_imported(name):
    assert read(contact(name=name, vcard=f"FN:{name}\nTEL:+79991112233")) == (
        "+79991112233", None, False,
    )


def test_folded_vcard_name_and_parameterized_phone():
    card = "BEGIN:VCARD\r\nFN:Иван Ива\r\n нов\r\nitem1.TEL;TYPE=CELL:+79991112233\r\nEND:VCARD"
    assert read(contact(vcard=card)) == ("+79991112233", "Иван Иванов", False)


def test_escaped_semicolon_does_not_become_name_separator():
    # Escaped ';' is part of the surname, not an N-component boundary.
    assert read(contact(vcard=r"N:Иванов\;Петров;Иван;;;"))[1] is None


def test_oversized_vcard_is_not_truncated_into_a_valid_name():
    card = "FN:Иван\nTEL:+79991112233\nNOTE:" + "x" * VCF_INFO_MAX_CHARS
    assert read(contact(vcard=card)) == (None, None, False)


def test_two_contacts_do_not_mix_phone_and_name():
    assert read(
        contact(vcard="TEL:+79991112233"),
        contact(name="Иван", vcard="TEL:+79994445566"),
    ) == ("+79991112233", None, False)


def test_foreign_first_contact_cannot_be_masked_by_second_contact():
    assert read(contact(name="Иван", user_id=8), contact(name="Иван")) == (None, None, True)


def test_no_contact():
    assert read() == (None, None, False)


@pytest.mark.parametrize("name", ["Беспаленко Лариса", "Лариса Беспаленко", "Беспаленко Лариса Ивановна"])
@pytest.mark.parametrize("as_dict", [False, True])
@pytest.mark.parametrize("source", ["max_info", "vcard"])
def test_contact_accepts_given_name_in_first_or_second_position(name, as_dict, source):
    attachment = contact(
        name=name if source == "max_info" else None,
        vcard=(f"FN:{name}\n" if source == "vcard" else "") + "TEL:+79991112233",
        as_dict=as_dict,
    )
    assert read(attachment) == ("+79991112233", name, False)
