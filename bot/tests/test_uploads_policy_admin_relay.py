"""Тесты services/uploads, services/policy, services/admin_relay.

Все три модуля либо импортируют maxapi напрямую, либо лазево внутри
функций. Локально skip без maxapi; в CI работает.

Покрываем:
- uploads.upload_path: успешный путь, ошибки upload_media, ошибка импорта
- uploads.upload_bytes: путь через InputMediaBuffer
- uploads.file_attachment: создание AttachmentUpload
- policy.build_file_attachment, _resolve_pdf_path
- policy.ensure_uploaded: cached token, миссинг файл, успешная загрузка
- admin_relay.relay_attachments_to_admin: пустой список, без admin_group,
  с маленьким батчем, с большим батчем (chunking)
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("maxapi", reason="uploads/policy тесты требуют maxapi")


@asynccontextmanager
async def _fake_session_scope():
    yield MagicMock()


class TestUploadPath:
    @pytest.mark.asyncio
    async def test_returns_token_on_success(self, tmp_path: Path) -> None:
        from aemr_bot.services import uploads

        f = tmp_path / "x.bin"
        f.write_bytes(b"data")

        bot = MagicMock()
        bot.upload_media = AsyncMock(
            return_value=SimpleNamespace(
                payload=SimpleNamespace(token="TOK-1")
            )
        )
        token = await uploads.upload_path(bot, f)
        assert token == "TOK-1"

    @pytest.mark.asyncio
    async def test_returns_none_when_upload_fails(self, tmp_path: Path) -> None:
        from aemr_bot.services import uploads

        f = tmp_path / "x.bin"
        f.write_bytes(b"data")

        bot = MagicMock()
        bot.upload_media = AsyncMock(side_effect=RuntimeError("network"))
        token = await uploads.upload_path(bot, f)
        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_payload(self, tmp_path: Path) -> None:
        from aemr_bot.services import uploads

        f = tmp_path / "x.bin"
        f.write_bytes(b"data")

        bot = MagicMock()
        # Возврат без payload.
        bot.upload_media = AsyncMock(return_value=SimpleNamespace(payload=None))
        token = await uploads.upload_path(bot, f)
        assert token is None


class TestUploadBytes:
    @pytest.mark.asyncio
    async def test_uses_input_media_buffer_first(self) -> None:
        from aemr_bot.services import uploads

        bot = MagicMock()
        bot.upload_media = AsyncMock(
            return_value=SimpleNamespace(
                payload=SimpleNamespace(token="BUF-TOK")
            )
        )
        token = await uploads.upload_bytes(bot, b"test-bytes", suffix=".txt")
        assert token == "BUF-TOK"

    @pytest.mark.asyncio
    async def test_falls_back_to_disk_on_type_error(self) -> None:
        from aemr_bot.services import uploads

        bot = MagicMock()
        # Первый вызов (с InputMediaBuffer) — TypeError; второй (через
        # upload_path → InputMedia(path=...)) — success.
        bot.upload_media = AsyncMock(
            side_effect=[
                TypeError("buffer arg unsupported"),
                SimpleNamespace(payload=SimpleNamespace(token="DISK-TOK")),
            ]
        )
        token = await uploads.upload_bytes(bot, b"x", suffix=".txt")
        assert token == "DISK-TOK"


class TestFileAttachment:
    def test_creates_attachment_upload(self) -> None:
        from aemr_bot.services import uploads

        att = uploads.file_attachment("TOK-1")
        # Должен иметь .payload.token = TOK-1
        assert att.payload.token == "TOK-1"


class TestPolicy:
    def test_resolve_pdf_path(self) -> None:
        from aemr_bot.services import policy

        path = policy._resolve_pdf_path()
        assert path.name == policy.POLICY_PDF_REL

    def test_build_file_attachment(self) -> None:
        from aemr_bot.services import policy

        att = policy.build_file_attachment("TOK")
        assert att.payload.token == "TOK"

    @pytest.mark.asyncio
    async def test_ensure_uploaded_returns_cached_token(self) -> None:
        from aemr_bot.services import policy

        bot = MagicMock()
        with patch("aemr_bot.services.policy.session_scope", _fake_session_scope), \
             patch("aemr_bot.services.policy.settings_store.get",
                   AsyncMock(return_value="CACHED-TOK")):
            token = await policy.ensure_uploaded(bot)
        assert token == "CACHED-TOK"

    @pytest.mark.asyncio
    async def test_ensure_uploaded_skips_when_pdf_missing(self) -> None:
        from aemr_bot.services import policy

        bot = MagicMock()
        with patch("aemr_bot.services.policy.session_scope", _fake_session_scope), \
             patch("aemr_bot.services.policy.settings_store.get",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.services.policy._resolve_pdf_path",
                   return_value=Path("/nonexistent/PRIVACY.pdf")):
            token = await policy.ensure_uploaded(bot)
        assert token is None

    @pytest.mark.asyncio
    async def test_ensure_uploaded_uploads_and_caches(self, tmp_path: Path) -> None:
        from aemr_bot.services import policy

        # Создаём PDF на месте.
        seed_pdf = tmp_path / "PRIVACY.pdf"
        seed_pdf.write_bytes(b"%PDF-1.4 fake")

        bot = MagicMock()
        set_value = AsyncMock()
        with patch("aemr_bot.services.policy.session_scope", _fake_session_scope), \
             patch("aemr_bot.services.policy.settings_store.get",
                   AsyncMock(return_value=None)), \
             patch("aemr_bot.services.policy.settings_store.set_value", set_value), \
             patch("aemr_bot.services.policy._resolve_pdf_path",
                   return_value=seed_pdf), \
             patch("aemr_bot.services.policy.uploads.upload_path",
                   AsyncMock(return_value="NEW-TOK")):
            token = await policy.ensure_uploaded(bot)
        assert token == "NEW-TOK"
        set_value.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_uploaded_force_refreshes(self, tmp_path: Path) -> None:
        from aemr_bot.services import policy

        seed_pdf = tmp_path / "PRIVACY.pdf"
        seed_pdf.write_bytes(b"%PDF")
        bot = MagicMock()
        set_value = AsyncMock()
        # Cached token есть, но force=True — игнорируем кэш.
        with patch("aemr_bot.services.policy.session_scope", _fake_session_scope), \
             patch("aemr_bot.services.policy.settings_store.get",
                   AsyncMock(return_value="OLD-TOK")), \
             patch("aemr_bot.services.policy.settings_store.set_value", set_value), \
             patch("aemr_bot.services.policy._resolve_pdf_path",
                   return_value=seed_pdf), \
             patch("aemr_bot.services.policy.uploads.upload_path",
                   AsyncMock(return_value="NEW-TOK")):
            token = await policy.ensure_uploaded(bot, force=True)
        assert token == "NEW-TOK"


class TestAdminRelay:
    @pytest.mark.asyncio
    async def test_skips_when_no_attachments(self) -> None:
        from aemr_bot.services import admin_relay

        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch.object(admin_relay.cfg, "admin_group_id", 555):
            await admin_relay.relay_attachments_to_admin(
                bot, appeal_id=1, admin_mid=None, stored_attachments=[]
            )
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_admin_group(self) -> None:
        from aemr_bot.services import admin_relay

        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch.object(admin_relay.cfg, "admin_group_id", None):
            await admin_relay.relay_attachments_to_admin(
                bot, appeal_id=1, admin_mid=None,
                stored_attachments=[{"type": "image", "payload": {"token": "T"}}],
            )
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_deserialize_returns_empty(self) -> None:
        from aemr_bot.services import admin_relay

        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch.object(admin_relay.cfg, "admin_group_id", 555), \
             patch("aemr_bot.services.admin_relay.deserialize_for_relay",
                   return_value=[]):
            await admin_relay.relay_attachments_to_admin(
                bot, appeal_id=1, admin_mid=None,
                stored_attachments=[{"type": "image"}],
            )
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_single_batch(self) -> None:
        from aemr_bot.services import admin_relay

        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch.object(admin_relay.cfg, "admin_group_id", 555), \
             patch.object(admin_relay.cfg, "attachments_per_relay_message", 10), \
             patch("aemr_bot.services.admin_relay.deserialize_for_relay",
                   return_value=[{"type": "image"}]):
            await admin_relay.relay_attachments_to_admin(
                bot, appeal_id=42, admin_mid=None,
                stored_attachments=[{"type": "image"}],
            )
        bot.send_message.assert_called_once()
        text = bot.send_message.call_args.kwargs.get("text", "")
        # Один батч → без «(1/N)»
        assert "#42" in text
        assert "1/" not in text

    @pytest.mark.asyncio
    async def test_chunks_into_batches_with_indicator(self) -> None:
        from aemr_bot.services import admin_relay

        bot = MagicMock()
        bot.send_message = AsyncMock()
        # 7 вложений по 3 за раз → 3 батча.
        relayable = [{"type": "image"}] * 7
        with patch.object(admin_relay.cfg, "admin_group_id", 555), \
             patch.object(admin_relay.cfg, "attachments_per_relay_message", 3), \
             patch("aemr_bot.services.admin_relay.deserialize_for_relay",
                   return_value=relayable):
            await admin_relay.relay_attachments_to_admin(
                bot, appeal_id=42, admin_mid=None,
                stored_attachments=relayable,
            )
        assert bot.send_message.call_count == 3
        # В первом батче должна быть пометка «(1/3)»
        first_text = bot.send_message.call_args_list[0].kwargs.get("text", "")
        assert "(1/3)" in first_text

    @pytest.mark.asyncio
    async def test_swallows_send_message_exceptions(self) -> None:
        from aemr_bot.services import admin_relay

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("network"))
        with patch.object(admin_relay.cfg, "admin_group_id", 555), \
             patch.object(admin_relay.cfg, "attachments_per_relay_message", 10), \
             patch("aemr_bot.services.admin_relay.deserialize_for_relay",
                   return_value=[{"type": "image"}]):
            # Не должно бросить.
            await admin_relay.relay_attachments_to_admin(
                bot, appeal_id=1, admin_mid=None,
                stored_attachments=[{"type": "image"}],
            )
