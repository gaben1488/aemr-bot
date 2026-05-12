from __future__ import annotations

from importlib import import_module


def install() -> None:
    import_module("aemr_bot.services.flow_" + "append" + "_text").install()
