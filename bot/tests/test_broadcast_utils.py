"""Чистые функции рассылки: задержка ЧС, шаг прогресса, разбор Retry-After.

Аудит покрытия по графу (graphify, 2026-08-08) показал, что
`services/broadcast_utils.py` не вызывается ни одним тестом, хотя решает
три вопроса с ценой ошибки: как быстро уйдёт оповещение о чрезвычайной
ситуации, будет ли оператор видеть движение прогресса и как бот
переживёт ограничение частоты со стороны мессенджера.

Функции чистые, поэтому проверяются напрямую — без событий и моков.
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from aemr_bot.services import broadcast_utils as bu


class TestEmergencyCooldown:
    """Пауза перед отправкой: у ЧС она короткая, у обычной — длинная."""

    def test_emergency_marker_shortens_the_wait(self) -> None:
        """[ЧС] сокращает паузу — оповещение не должно ждать пять минут.

        Пауза существует, чтобы оператор успел отменить ошибочную
        рассылку. Но при реальной чрезвычайной ситуации эти минуты
        стоят дороже права на отмену, поэтому окно сжимается.
        """
        emergency = bu._broadcast_cooldown_seconds("[ЧС] Циклон, не выходите из дома")
        normal = bu._broadcast_cooldown_seconds("Плановое отключение воды в среду")

        assert emergency < normal, "оповещение о ЧС обязано уходить быстрее обычного"
        assert emergency > 0, "мгновенная отправка лишила бы оператора шанса отменить"

    @pytest.mark.parametrize(
        "text",
        ["[ЧС] тревога", "Внимание! [ЧС] цунами", "[чс] в нижнем регистре"],
        ids=["в начале", "в середине", "в нижнем регистре"],
    )
    def test_marker_recognised_anywhere_and_in_any_case(self, text: str) -> None:
        """Метку ищут по всему тексту и без учёта регистра.

        Оператор в спешке напишет её как получится и где придётся;
        если распознавание окажется строгим, оповещение о ЧС уйдёт с
        пятиминутной задержкой — молча, без единого предупреждения.
        """
        assert bu._broadcast_cooldown_seconds(text) < bu._broadcast_cooldown_seconds(
            "обычное объявление"
        ), f"метка ЧС не распознана в тексте: {text!r}"


class TestProgressStep:
    """Шаг обновления карточки прогресса подстраивается под длину рассылки."""

    def test_short_broadcast_updates_more_often(self) -> None:
        """На пяти получателях полоска не должна дёрнуться один раз в конце.

        Оператор смотрит на карточку и решает, идёт ли рассылка. При
        неподвижном прогрессе он нажимает «Отправить» повторно.
        """
        short = bu._compute_progress_step(total=5, rate_delay=0.5)
        long = bu._compute_progress_step(total=1000, rate_delay=0.5)

        assert short < long, "короткая рассылка обязана обновляться чаще длинной"
        assert short > 0, "нулевой шаг — это обновление на каждом сообщении"

    def test_step_never_exceeds_configured_ceiling(self) -> None:
        """Потолок из настроек — верхняя граница, а не пожелание.

        Иначе на очень длинной рассылке карточка замерла бы на минуты, и
        оператор счёл бы бот зависшим.
        """
        from aemr_bot.config import settings as cfg

        for total in (1, 50, 10_000):
            step = bu._compute_progress_step(total=total, rate_delay=2.0)
            assert step <= cfg.broadcast_progress_update_sec, (
                f"шаг {step} превысил потолок при total={total}"
            )


class TestRetryAfter:
    """Разбор Retry-After: точная пауза лучше слепого удвоения."""

    def test_reads_value_from_error_payload(self) -> None:
        """Мессенджер сказал, сколько ждать — слушаем его, а не гадаем.

        Без этого бот уходит в удвоение задержки и растягивает рассылку
        (в том числе экстренную) на дольше, чем требует сам сервис.
        """
        exc = Exception("429")
        exc.raw = {"retry_after": "3.5"}  # type: ignore[attr-defined]
        assert bu._extract_retry_after(exc) == 3.5

    @pytest.mark.parametrize(
        "raw",
        [None, {}, {"retry_after": "не число"}, {"other": 5}, "строка вместо словаря"],
        ids=["нет поля", "пусто", "мусор", "другой ключ", "не словарь"],
    )
    def test_returns_none_instead_of_raising(self, raw) -> None:
        """Любой неожиданный ответ — None, а не исключение.

        Функция разбирает чужой формат ошибки; если она сама упадёт,
        рассылка прервётся из-за неудачной попытки понять, когда её
        продолжить. Вызывающий по None уходит в обычную отсрочку.
        """
        exc = Exception("429")
        if raw is not None:
            exc.raw = raw  # type: ignore[attr-defined]
        assert bu._extract_retry_after(exc) is None


class TestFinalText:
    """Итог рассылки: цифры на месте, отмена отличима от завершения."""

    def test_done_text_reports_all_numbers(self) -> None:
        text = bu._build_final_text(
            broadcast_id=42, total=100, delivered=97, failed=3, cancelled=False
        )
        assert "42" in text and "97" in text and "100" in text
        assert "3" in text, "число ошибок обязано попасть в итог"

    def test_failed_line_disappears_when_nothing_failed(self) -> None:
        """Без ошибок строку про ошибки не показываем.

        «Не доставлено: 0» заставляет оператора искать проблему там, где
        её нет.
        """
        clean = bu._build_final_text(
            broadcast_id=1, total=10, delivered=10, failed=0, cancelled=False
        )
        assert "0" not in clean.replace("10", "").replace("1", ""), (
            f"в чистом итоге не должно быть нулевых ошибок: {clean}"
        )

    def test_cancelled_differs_from_completed(self) -> None:
        """Отменённая рассылка не выглядит как успешно доставленная.

        Оператор по карточке решает, запускать ли заново; спутать эти
        два исхода — значит либо продублировать оповещение, либо не
        отправить его вовсе.
        """
        cancelled = bu._build_final_text(
            broadcast_id=7, total=50, delivered=20, failed=0, cancelled=True
        )
        done = bu._build_final_text(
            broadcast_id=7, total=50, delivered=50, failed=0, cancelled=False
        )
        assert cancelled != done
        # Бот пишет «остановлена» — слово выбрано сознательно: рассылка
        # была прервана на ходу, часть жителей оповещение уже получила.
        # «Отменена» подразумевало бы, что не ушло ничего.
        assert "становлена" in cancelled, (
            f"прерванная рассылка обязана называться остановленной: {cancelled}"
        )
        assert str(20) in cancelled and str(50) in cancelled, (
            "оператор должен видеть, скольким успели отправить до остановки"
        )


class TestFormatDt:
    def test_renders_in_the_given_timezone(self) -> None:
        """Время показывается в поясе округа, а не сервера.

        Контейнер живёт в UTC; без явного пояса оператор увидел бы
        время, не совпадающее с его часами.
        """
        moment = datetime(2026, 7, 2, 12, 30, tzinfo=UTC)
        rendered = bu._format_dt(moment, ZoneInfo("Asia/Kamchatka"))
        assert "03.07.2026" in rendered, f"ожидалось камчатское время: {rendered}"
        assert "00:30" in rendered

    def test_missing_value_does_not_crash(self) -> None:
        """Пустая дата — прочерк, а не падение карточки.

        Рассылка могла не стартовать, и поле остаётся пустым; карточка
        обязана отрисоваться.
        """
        assert bu._format_dt(None, ZoneInfo("Asia/Kamchatka")).strip() != ""
