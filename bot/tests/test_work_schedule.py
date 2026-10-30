from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from aemr_bot.services import sla


def test_explicit_schedule_preserves_work_hours_and_calendar():
    schedule = {"version": 1, "timezone": "Asia/Kamchatka", "start": 10,
                "end": 17, "lunch_start": 12, "lunch_end": 13,
                "holidays": ["2026-06-17"]}
    tz = ZoneInfo("Asia/Kamchatka")
    start = datetime(2026, 6, 16, 9, tzinfo=tz)
    end = datetime(2026, 6, 17, 18, tzinfo=tz)
    assert sla.business_seconds_between(start, end, schedule=schedule) == 6 * 3600
    assert sla.is_overdue(start, end, 6, schedule=schedule)


@pytest.mark.parametrize("change", [{"start": True}, {"end": 25}, {"lunch_start": 8},
                                    {"lunch_end": 12}, {"timezone": "bad/zone"},
                                    {"holidays": ["2026-99-01"]}, {"version": 2}])
def test_invalid_schedule_rejected(change):
    from aemr_bot.services import work_schedule
    value = {"version": 1, "timezone": "Asia/Kamchatka", "start": 9,
             "end": 18, "lunch_start": 12, "lunch_end": 13, "holidays": []}
    value.update(change)
    with pytest.raises(ValueError):
        work_schedule.validate(value)


def test_preserves_previously_valid_environment_lunch_at_work_boundary():
    from aemr_bot.services import work_schedule
    value = dict(work_schedule.environment(), start=9, lunch_start=9, lunch_end=10, end=18)
    work_schedule.validate(value)


def test_schema_uses_schedule_validation():
    from aemr_bot.services import settings_store, work_schedule
    assert settings_store.validate(work_schedule.KEY, work_schedule.environment())[0]
    assert not settings_store.validate(work_schedule.KEY, dict(work_schedule.environment(), start=True))[0]
