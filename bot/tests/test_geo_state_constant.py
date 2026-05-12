from aemr_bot.db.models import DialogState


def test_geo_state_constant() -> None:
    assert DialogState.AWAITING_GEO_CONFIRM.value == "awaiting_geo_confirm"
