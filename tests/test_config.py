import pytest

from app.config import Settings


def test_invalid_dry_run_value_fails_closed(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "treu")
    with pytest.raises(ValueError, match="DRY_RUN"):
        Settings.from_env()
