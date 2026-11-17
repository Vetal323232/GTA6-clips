"""Test-only HUD plugin: reports one fixed event. Not a real GTA VI detector."""

version = "test-1"


def detect(path, duration, runner, cfg):
    return [(min(duration - 1, 15.0), "тестовое событие", 0.9)]
