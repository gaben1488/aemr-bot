"""Целевые тесты на ВЫЖИВШИХ мутантов (mutation testing).

mutmut 3.5 не запускается на нативном Windows (требует WSL), поэтому был
проведён РУЧНОЙ mutation-анализ критичных модулей: в логику каждого
вносились типичные мутации (границы `<`/`<=`, инверсии guard'ов,
fail-open вместо fail-closed, off-by-one в срезах), затем прогонялся
профильный тест-сабсет. Мутанты, которых существующие тесты НЕ ловили
(SURVIVED), означают пробел в покрытии — этот файл их закрывает.

Каждый тест в комментарии помечает, какого именно мутанта он убивает,
чтобы при будущем рефакторинге было видно назначение проверки.

Модули и выжившие мутанты, закрытые здесь:
- services/idempotency.py   — claim() return-логика (вкл. SEC#7 fail-closed),
                              build_key cb_id-guard, try_mark_processed_raw.
- services/settings_store.py — _is_whitelisted_url suffix-dot (anti-phishing!),
                              mixed-case/non-ascii reject, validate-границы,
                              _values_equivalent None-логика, get_dirty_keys.
- services/db_backup.py      — passphrase min-length граница, chmod 0600,
                              rc-проверки в _run_pg_dump_encrypted.
- services/admin_events.py   — _mask_phone prefix-граница len>=11.
- services/users.py          — find_by_phone row-count логика, erase_pdn wrapper.
- utils/url_defang.py        — scheme-defang \b-anchor граница.

NB: часть выживших мутантов в users.py (set_blocked, revoke_consent,
search_audience, find_by_phone limit(2)) достижима только с реальным
Postgres (advisory-lock, ILIKE, серверный rowcount) — они покрыты
интеграционными тестами test_users_service_pg.py в CI и здесь намеренно
не дублируются мок-заглушками.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

import aemr_bot.services.admin_events as admin_events
import aemr_bot.services.db_backup as db_backup
import aemr_bot.services.idempotency as idem
import aemr_bot.services.settings_store as ss
import aemr_bot.services.users as users
from aemr_bot.utils.url_defang import _ZWSP, defang_url_in_text

# ──────────────────────────────────────────────────────────────────────
# Тестовые двойники для idempotency.claim / try_mark_processed_raw,
# которые ходят в БД через модульный session_scope.
# ──────────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    """Минимальная замена AsyncSession: execute() либо возвращает результат
    с заданным rowcount, либо бросает заданное исключение."""

    def __init__(self, *, rowcount: int = 0, exc: Exception | None = None) -> None:
        self._rowcount = rowcount
        self._exc = exc

    async def execute(self, _stmt):  # noqa: ANN001
        if self._exc is not None:
            raise self._exc
        return _FakeResult(self._rowcount)


def _scope_factory(*, rowcount: int = 0, exc: Exception | None = None):
    """Фабрика async-context-manager'а, подменяющего session_scope."""

    @asynccontextmanager
    async def _scope():
        yield _FakeSession(rowcount=rowcount, exc=exc)

    return _scope


def _event_with_mid() -> SimpleNamespace:
    """Событие с достаточными полями, чтобы build_idempotency_key вернул ключ."""
    return SimpleNamespace(
        update_type="message_created",
        callback=None,
        message=SimpleNamespace(body=SimpleNamespace(mid="m-1", seq=7), timestamp=None),
        timestamp=1000,
        chat_id=None,
        user=None,
    )


# ══════════════════════════════════════════════════════════════════════
# services/idempotency.py
# ══════════════════════════════════════════════════════════════════════


class TestIdempotencyClaim:
    """claim() — диспетчер дедупа. Существующий test_idempotency.py
    покрывает только build_idempotency_key; return-логика claim() была
    полностью непокрыта (6 выживших мутантов)."""

    @pytest.mark.asyncio
    async def test_fresh_event_returns_true(self) -> None:
        # Убивает мутанта: claim rowcount==0 -> return True.
        # rowcount=1 (вставка прошла) ⇒ событие новое ⇒ обрабатываем.
        with patch.object(idem, "session_scope", _scope_factory(rowcount=1)):
            assert await idem.claim(_event_with_mid()) is True

    @pytest.mark.asyncio
    async def test_duplicate_event_returns_false(self) -> None:
        # Убивает мутанта: claim rowcount==0 -> return True.
        # on_conflict_do_nothing дал rowcount=0 ⇒ дубль ⇒ пропускаем.
        with patch.object(idem, "session_scope", _scope_factory(rowcount=0)):
            assert await idem.claim(_event_with_mid()) is False

    @pytest.mark.asyncio
    async def test_keyless_event_processes(self) -> None:
        # Убивает мутанта: claim key is None -> return False.
        # Без идентифицирующих полей ключ собрать нельзя — лучше
        # обработать (вернуть True), чем потерять событие.
        keyless = SimpleNamespace(
            update_type="x", callback=None, message=None,
            timestamp=None, chat_id=None, user=None,
        )
        # session_scope не должен вызываться вовсе — но если вызовется,
        # пусть упадёт, чтобы тест поймал «полез в БД при None-ключе».
        with patch.object(idem, "session_scope", _scope_factory(exc=AssertionError("DB hit"))):
            assert await idem.claim(keyless) is True

    @pytest.mark.asyncio
    async def test_integrity_error_is_duplicate(self) -> None:
        # Убивает мутанта: claim IntegrityError -> return True.
        # Гонка двух вставок одного ключа: проигравшая ловит
        # IntegrityError ⇒ это дубль ⇒ False.
        exc = IntegrityError("stmt", "params", Exception("unique"))
        with patch.object(idem, "session_scope", _scope_factory(exc=exc)):
            assert await idem.claim(_event_with_mid()) is False

    @pytest.mark.asyncio
    async def test_db_stall_fails_closed_sec7(self) -> None:
        # Убивает мутанта: claim generic Exception -> return True
        # (регрессия SEC #7). Любой сбой БД (timeout, connection drop)
        # ДОЛЖЕН вести к fail-CLOSED (return False): иначе attacker
        # индуцирует stall и заставляет повторно обработать мутирующий
        # callback (op:close / op:erase / broadcast confirm).
        with patch.object(idem, "session_scope", _scope_factory(exc=RuntimeError("db stall"))):
            assert await idem.claim(_event_with_mid()) is False


class TestBuildKeyCallbackGuard:
    def test_empty_callback_id_not_appended(self) -> None:
        # Убивает мутанта: build_key `if cb_id:` -> `if cb_id or True:`.
        # Пустой callback_id не должен попадать в ключ. Без других
        # идентифицирующих полей результат — None (нечего дедуплицировать).
        ev = SimpleNamespace(
            update_type="message_callback",
            callback=SimpleNamespace(callback_id=""),
            message=None, timestamp=None, chat_id=None, user=None,
        )
        assert idem.build_idempotency_key(ev) is None

    def test_present_callback_id_is_appended(self) -> None:
        # Парная проверка: непустой callback_id попадает в ключ.
        ev = SimpleNamespace(
            update_type="message_callback",
            callback=SimpleNamespace(callback_id="cb-xyz"),
            message=None, timestamp=None, chat_id=None, user=None,
        )
        key = idem.build_idempotency_key(ev)
        assert key is not None and "cb=cb-xyz" in key


class TestTryMarkProcessedRaw:
    @pytest.mark.asyncio
    async def test_free_key_claimed(self) -> None:
        # Убивает мутанта: try_mark_processed_raw `> 0` -> `>= 0`.
        with patch.object(idem, "session_scope", _scope_factory(rowcount=1)):
            assert await idem.try_mark_processed_raw("k1", "kind") is True

    @pytest.mark.asyncio
    async def test_taken_key_rejected(self) -> None:
        # Убивает мутанта: `> 0` -> `>= 0`. rowcount=0 ⇒ ключ занят ⇒ False.
        with patch.object(idem, "session_scope", _scope_factory(rowcount=0)):
            assert await idem.try_mark_processed_raw("k1", "kind") is False


# ══════════════════════════════════════════════════════════════════════
# services/settings_store.py
# ══════════════════════════════════════════════════════════════════════


class TestWhitelistSuffixBoundary:
    """SEC #4 anti-phishing: гос-whitelist должен матчить только сам домен
    и его поддомены (`gosuslugi.ru`, `sub.gosuslugi.ru`), но НЕ домены, у
    которых имя whitelиста — лишь суффикс строки (`evilgosuslugi.ru`).
    Это обеспечивает `host.endswith("." + suffix)` — leading dot. Ни один
    тест не пинговал эту границу (3 выживших мутанта)."""

    def test_exact_domain_allowed(self) -> None:
        assert ss._is_whitelisted_url("https://gosuslugi.ru") is True

    def test_subdomain_allowed(self) -> None:
        assert ss._is_whitelisted_url("https://udth.elizovomr.ru") is True

    @pytest.mark.parametrize(
        "phish",
        [
            "https://evilgosuslugi.ru",     # суффикс-склейка без точки
            "https://xkamgov.ru",
            "https://fakeelizovomr.ru",
            "https://attacker-gosuslugi.ru",
        ],
    )
    def test_suffix_glued_phishing_rejected(self, phish: str) -> None:
        # Убивает мутанта: `host.endswith("." + suffix)` -> `host.endswith(suffix)`.
        # Без leading dot `evilgosuslugi.ru` ложно прошёл бы whitelist.
        assert ss._is_whitelisted_url(phish) is False

    def test_mixed_case_host_rejected(self) -> None:
        # Убивает мутанта: §A4 mixed-case reject removed.
        # Важно изолировать ИМЕННО case-guard: берём host, который
        # проходит suffix-проверку (`.gosuslugi.ru`), но содержит
        # uppercase в поддомене. Если бы здесь стоял `Gosuslugi.RU` —
        # его отверг бы и suffix-match, и мутант (с выключенным
        # case-guard'ом) остался бы жив.
        assert ss._is_whitelisted_url("https://SUB.gosuslugi.ru") is False
        # Контроль: тот же домен в lowercase — допустим.
        assert ss._is_whitelisted_url("https://sub.gosuslugi.ru") is True

    def test_homoglyph_host_rejected(self) -> None:
        # Убивает мутанта: §A4 non-ascii reject removed.
        # Изолируем ascii-guard: host оканчивается на `.gosuslugi.ru`
        # (suffix-проверка прошла бы), но в поддомене кириллическая «е»
        # (U+0435) — гомоглиф. Только ascii-guard его отвергает.
        assert ss._is_whitelisted_url("https://tеst.gosuslugi.ru") is False

    def test_lowercase_ascii_legit_still_allowed(self) -> None:
        # Контроль: легитимная lowercase ASCII ссылка не задета §A4.
        assert ss._is_whitelisted_url("https://kamgov.ru/questions") is True


class TestValidateBoundaries:
    """validate() — единственный гейт правки настроек через UI. Границы
    `max_len`, int min/max, max_items, bool-как-int, url-scheme guard
    выживали (6 мутантов)."""

    def test_max_len_exact_ok_over_rejected(self) -> None:
        # Убивает мутанта: `len > max_len` -> `len >= max_len`.
        ok, _ = ss.validate("welcome_text", "x" * 3800)
        assert ok is True
        bad, _ = ss.validate("welcome_text", "x" * 3801)
        assert bad is False

    def test_int_min_boundary(self) -> None:
        # Убивает мутанта: `value < min` -> `value <= min`.
        # broadcast_max_images min=1: ровно 1 — допустимо.
        assert ss.validate("broadcast_max_images", 1)[0] is True
        assert ss.validate("broadcast_max_images", 0)[0] is False

    def test_int_max_boundary(self) -> None:
        # Убивает мутанта: `value > max` -> `value >= max`.
        # broadcast_max_images max=20: ровно 20 — допустимо.
        assert ss.validate("broadcast_max_images", 20)[0] is True
        assert ss.validate("broadcast_max_images", 21)[0] is False

    def test_max_items_boundary(self) -> None:
        # Убивает мутанта: `len > max_items` -> `len >= max_items`.
        # topics max_items=30: ровно 30 — допустимо, 31 — нет.
        assert ss.validate("topics", ["t"] * 30)[0] is True
        assert ss.validate("topics", ["t"] * 31)[0] is False

    def test_bool_rejected_for_int_key(self) -> None:
        # Убивает мутанта: bool-as-int guard removed.
        # bool — подкласс int; True/False НЕ должны проходить как int.
        ok, msg = ss.validate("broadcast_max_images", True)
        assert ok is False
        assert "bool" in msg

    def test_url_requires_scheme_prefix(self) -> None:
        # Убивает мутанта: url scheme-prefix guard removed.
        # Без guard'а сообщение об ошибке стало бы «не в whitelist»;
        # текущий код отдаёт именно «must start with http(s)://».
        ok, msg = ss.validate("policy_url", "gosuslugi.ru")
        assert ok is False
        assert "must start with" in msg


class TestValuesEquivalentNone:
    """_values_equivalent — основа backfill-логики seed_if_empty. None-ветки
    выживали (2 мутанта)."""

    def test_both_none_equivalent(self) -> None:
        # Убивает мутанта: `a is None and b is None: return True` -> False.
        assert ss._values_equivalent(None, None) is True

    @pytest.mark.parametrize("a,b", [(None, []), (None, "x"), ([], None), (0, None)])
    def test_one_none_not_equivalent(self, a, b) -> None:  # noqa: ANN001
        # Убивает мутанта: `a is None or b is None: return False` -> True.
        # None и любое не-None значение — НЕ эквивалентны (иначе backfill
        # пометил бы NULL-ключ как совпадающий с seed-baseline).
        assert ss._values_equivalent(a, b) is False

    def test_dict_order_insensitive(self) -> None:
        # Контроль положительной ветки (нормализация через json.dumps).
        assert ss._values_equivalent({"a": 1, "b": 2}, {"b": 2, "a": 1}) is True


class TestGetDirtyKeys:
    """get_dirty_keys — индикатор «N несинхронизированных изменений». Условие
    `updated_at > synced_at` выживало (1 мутант)."""

    @pytest.mark.asyncio
    async def test_updated_after_synced_is_dirty(self) -> None:
        # Убивает мутанта: `updated > synced` -> `updated < synced`.
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        older = now - timedelta(hours=1)
        newer = now + timedelta(hours=1)

        class _Rows:
            def __init__(self, rows): self._rows = rows
            def all(self): return self._rows

        class _Sess:
            def __init__(self, rows): self._rows = rows
            async def execute(self, _stmt): return _Rows(self._rows)

        rows = [
            ("policy_url", now, None),            # synced None -> dirty
            ("topics", newer, now),               # updated>synced -> dirty
            ("localities", older, now),           # updated<synced -> clean
            ("appointment_text", now, now),       # равны -> clean
        ]
        dirty = await ss.get_dirty_keys(_Sess(rows))
        assert dirty == ["policy_url", "topics"]


# ══════════════════════════════════════════════════════════════════════
# services/db_backup.py
# ══════════════════════════════════════════════════════════════════════


def _backup_settings_patch(tmp_path: Path, passphrase: str):
    """Контекст с замоканными settings для backup_db (без S3)."""
    return [
        patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)),
        patch.object(db_backup.settings, "backup_gpg_passphrase", passphrase),
        patch.object(db_backup.settings, "backup_allow_unencrypted", True),
        patch.object(db_backup.settings, "backup_keep_count", 5),
        patch.object(db_backup.settings, "backup_s3_bucket", ""),
        patch.object(db_backup.settings, "backup_s3_endpoint", ""),
        patch.object(db_backup.settings, "backup_s3_access_key", ""),
        patch.object(db_backup.settings, "backup_s3_secret_key", ""),
    ]


class TestBackupPassphraseBoundary:
    @pytest.mark.asyncio
    async def test_eleven_char_passphrase_not_encrypted(self, tmp_path: Path) -> None:
        # Убивает мутанта: `len(passphrase) < 12` -> `< 6`.
        # 11-символьная фраза слишком слаба для AES-256 ⇒ должна
        # сбрасываться, дамп идёт без шифрования (.sql). Существующий
        # тест использовал 5-символьную фразу, которую и `<6` отвергает,
        # поэтому границу 6..11 никто не проверял.
        async def fake_dump(out_path, _env):  # noqa: ANN001
            out_path.write_bytes(b"plain")

        with (
            patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)),
            patch.object(db_backup.settings, "backup_gpg_passphrase", "elevenchars"),
            patch.object(db_backup.settings, "backup_allow_unencrypted", True),
            patch.object(db_backup.settings, "backup_keep_count", 5),
            patch.object(db_backup.settings, "backup_s3_bucket", ""),
            patch.object(db_backup.settings, "backup_s3_endpoint", ""),
            patch.object(db_backup.settings, "backup_s3_access_key", ""),
            patch.object(db_backup.settings, "backup_s3_secret_key", ""),
            patch.object(db_backup, "_run_pg_dump", side_effect=fake_dump) as plain,
            patch.object(db_backup, "_run_pg_dump_encrypted") as enc,
            patch.object(db_backup, "_build_pg_env", return_value={}),
        ):
            result = await db_backup.backup_db()
        assert result.ok is True
        assert result.path is not None
        assert result.path.suffix == ".sql"  # НЕ .gpg
        plain.assert_called_once()
        enc.assert_not_called()


class TestBackupChmod:
    @pytest.mark.asyncio
    async def test_backup_file_chmod_0600(self, tmp_path: Path) -> None:
        # Убивает мутанта: `os.chmod(out, 0o600)` -> `0o644`.
        # Дамп содержит телефоны/тексты/audit-лог — права должны быть 0600.
        async def fake_dump(out_path, _env):  # noqa: ANN001
            out_path.write_bytes(b"plain")

        with (
            patch.object(db_backup.settings, "backup_local_dir", str(tmp_path)),
            patch.object(db_backup.settings, "backup_gpg_passphrase", ""),
            patch.object(db_backup.settings, "backup_allow_unencrypted", True),
            patch.object(db_backup.settings, "backup_keep_count", 5),
            patch.object(db_backup.settings, "backup_s3_bucket", ""),
            patch.object(db_backup.settings, "backup_s3_endpoint", ""),
            patch.object(db_backup.settings, "backup_s3_access_key", ""),
            patch.object(db_backup.settings, "backup_s3_secret_key", ""),
            patch.object(db_backup, "_run_pg_dump", side_effect=fake_dump),
            patch.object(db_backup, "_build_pg_env", return_value={}),
            patch("aemr_bot.services.db_backup.os.chmod") as mock_chmod,
        ):
            result = await db_backup.backup_db()
        assert result.ok is True
        # Дамп-файл (не каталог) chmod'ится в 0o600.
        modes = [call.args[1] for call in mock_chmod.call_args_list]
        assert 0o600 in modes
        assert 0o644 not in modes


class _RcProc:
    def __init__(self, rc: int) -> None:
        self._rc = rc

    async def wait(self) -> int:
        return self._rc


class TestRunPgDumpEncryptedReturnCodes:
    """_run_pg_dump_encrypted — реальная rc-проверка pg_dump/gpg. Существующие
    тесты всегда мокали эту функцию целиком, поэтому её внутренние
    `if dump_rc != 0` / `if gpg_rc != 0` были непокрыты (2 мутанта)."""

    @pytest.mark.asyncio
    async def test_both_success_no_raise(self, tmp_path: Path) -> None:
        out = tmp_path / "x.sql.gpg"

        async def mk(*_a, **_k):  # noqa: ANN002, ANN003
            return _RcProc(0)

        with (
            patch("asyncio.create_subprocess_exec", side_effect=mk),
            patch("aemr_bot.services.db_backup.os.makedirs"),
        ):
            await db_backup._run_pg_dump_encrypted(out, {}, "passphrase-1234")

    @pytest.mark.asyncio
    async def test_gpg_failure_raises_gpg_error(self, tmp_path: Path) -> None:
        # Убивает мутанта: `if gpg_rc != 0` -> `== 0`.
        out = tmp_path / "x.sql.gpg"

        async def mk(prog, *_a, **_k):  # noqa: ANN001, ANN002, ANN003
            # pg_dump ок (rc=0), gpg падает (rc=2).
            return _RcProc(0 if prog == "pg_dump" else 2)

        with (
            patch("asyncio.create_subprocess_exec", side_effect=mk),
            patch("aemr_bot.services.db_backup.os.makedirs"),
        ):
            with pytest.raises(db_backup.BackupGpgError, match="gpg failed with code 2"):
                await db_backup._run_pg_dump_encrypted(out, {}, "passphrase-1234")

    @pytest.mark.asyncio
    async def test_pgdump_failure_raises_pgdump_error(self, tmp_path: Path) -> None:
        # Убивает мутанта: `if dump_rc != 0` -> `== 0`.
        out = tmp_path / "x.sql.gpg"

        async def mk(prog, *_a, **_k):  # noqa: ANN001, ANN002, ANN003
            # pg_dump падает (rc=3), gpg «ок» (rc=0).
            return _RcProc(3 if prog == "pg_dump" else 0)

        with (
            patch("asyncio.create_subprocess_exec", side_effect=mk),
            patch("aemr_bot.services.db_backup.os.makedirs"),
        ):
            with pytest.raises(db_backup.BackupPgDumpError, match="pg_dump failed with code 3"):
                await db_backup._run_pg_dump_encrypted(out, {}, "passphrase-1234")


# ══════════════════════════════════════════════════════════════════════
# services/admin_events.py
# ══════════════════════════════════════════════════════════════════════


class TestMaskPhonePrefixBoundary:
    def test_ten_digit_number_no_country_prefix(self) -> None:
        # Убивает мутанта: `len(digits) >= 11` -> `>= 10`.
        # 10-значный номер (без кода страны) маскируется как «+***NNNN»,
        # НЕ «+7***NNNN» — префикс +7 ставится только для 11-значного
        # нормализованного номера с кодом страны.
        assert admin_events._mask_phone("7901234567") == "+***4567"

    def test_eleven_digit_number_country_prefix(self) -> None:
        # Парная проверка: 11-значный номер с 7/8 в начале → «+7***NNNN».
        assert admin_events._mask_phone("79001234567") == "+7***4567"
        assert admin_events._mask_phone("89001234567") == "+7***4567"


# ══════════════════════════════════════════════════════════════════════
# services/users.py  (только мок-достижимые ветки; PG-логика — в CI)
# ══════════════════════════════════════════════════════════════════════


class _ScalarsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _PhoneSession:
    """Замена AsyncSession для find_by_phone: scalars(...).all() отдаёт
    заранее заданный список строк."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def scalars(self, _stmt):  # noqa: ANN001
        return _ScalarsResult(self._rows)


class TestFindByPhoneRowCount:
    """find_by_phone — решение 0/1/>1 совпадений. Только row-count логика
    (limit(2) vs limit(1) достижим лишь с реальным курсором БД)."""

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self) -> None:
        assert await users.find_by_phone(_PhoneSession([]), "+79001234567") is None

    @pytest.mark.asyncio
    async def test_single_match_returned(self) -> None:
        u = SimpleNamespace(max_user_id=111, phone_normalized="9001234567")
        result = await users.find_by_phone(_PhoneSession([u]), "+79001234567")
        assert result is u

    @pytest.mark.asyncio
    async def test_ambiguous_match_returns_none(self) -> None:
        # Убивает мутанта: `if len(rows) > 1` -> `> 100`.
        # Два жителя на одном номере (муж/жена на симке) — возвращаем
        # None, чтобы /erase phone= не стёр случайного из совпавших.
        u1 = SimpleNamespace(max_user_id=111, phone_normalized="9001234567")
        u2 = SimpleNamespace(max_user_id=222, phone_normalized="9001234567")
        assert await users.find_by_phone(_PhoneSession([u1, u2]), "+79001234567") is None

    @pytest.mark.asyncio
    async def test_empty_normalized_phone_returns_none(self) -> None:
        # Убивает мутанта: `if not target: return None` -> guard removed.
        # Телефон без цифр нормализуется в "" — сразу None, не лезем в БД.
        sentinel = SimpleNamespace(max_user_id=999, phone_normalized="x")
        assert await users.find_by_phone(_PhoneSession([sentinel]), "абвгд") is None


class TestErasePdnWrapper:
    @pytest.mark.asyncio
    async def test_returns_false_when_user_absent(self) -> None:
        # Убивает мутанта: erase_pdn `return closed_ids is not None` -> `return True`.
        # erase_pdn_detailed вернул None (жителя нет) ⇒ wrapper отдаёт False.
        with patch.object(users, "erase_pdn_detailed", AsyncMock(return_value=None)):
            assert await users.erase_pdn(object(), 123) is False

    @pytest.mark.asyncio
    async def test_returns_true_when_user_erased(self) -> None:
        # Парная проверка: пустой список закрытых обращений — это всё равно
        # успех (житель найден и обработан) ⇒ True.
        with patch.object(users, "erase_pdn_detailed", AsyncMock(return_value=[])):
            assert await users.erase_pdn(object(), 123) is True


# ══════════════════════════════════════════════════════════════════════
# utils/url_defang.py
# ══════════════════════════════════════════════════════════════════════


class TestSchemeDefangAnchor:
    """scheme-defang использует `\\b(https?)://`. Эта проверка пинит границу,
    задаваемую `\\b`-anchor'ом (выживший мутант — удаление `\\b`).

    ВНИМАНИЕ (документированная граница, не идеал): из-за `\\b` ссылка,
    приклеенная к слову без пробела (`срочноhttps://phish.ru`), scheme-
    defang'ом НЕ разрывается, и bare-domain шаг тоже пропускает её из-за
    `(?<!//)`. Это латентный пробел anti-clickjacking — задокументирован
    в findings; здесь тест фиксирует текущий контракт, чтобы изменение
    `\\b` было осознанным."""

    def test_scheme_with_space_is_defanged(self) -> None:
        out = defang_url_in_text("Перейдите на https://attacker.com")
        assert f"https{_ZWSP}://attacker.com" in out

    def test_scheme_glued_to_word_keeps_anchor_behavior(self) -> None:
        # Убивает мутанта: `\b(https?)://` -> `(https?)://`.
        # При наличии `\b` приклеенная к слову схема (`aaahttps://`) НЕ
        # получает ZWSP между схемой и `://` (текущее поведение). Мутант
        # без `\b` вставил бы ZWSP — тест бы упал.
        out = defang_url_in_text("aaahttps://x.ru")
        assert _ZWSP not in out
