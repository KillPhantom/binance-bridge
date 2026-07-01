import pytest

from app.config import Settings


def test_invalid_dry_run_value_fails_closed(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "treu")
    with pytest.raises(ValueError, match="DRY_RUN"):
        Settings.from_env()


def test_named_binance_accounts_are_read_from_env(monkeypatch):
    monkeypatch.setenv("BINANCE_ACCOUNT_NAMES", "primary, copy-trader")
    monkeypatch.setenv("BINANCE_PRIMARY_API_KEY", "key-1")
    monkeypatch.setenv("BINANCE_PRIMARY_API_SECRET", "secret-1")
    monkeypatch.setenv("BINANCE_PRIMARY_ALLOWED_SYMBOLS", "ETHUSDT,SOLUSDT")
    monkeypatch.setenv("BINANCE_COPY_TRADER_API_KEY", "key-2")
    monkeypatch.setenv("BINANCE_COPY_TRADER_API_SECRET", "secret-2")
    monkeypatch.setenv("BINANCE_COPY_TRADER_ALLOWED_SYMBOLS", "ETHUSDT")

    settings = Settings.from_env()

    assert [account.name for account in settings.binance_accounts] == [
        "primary",
        "copy-trader",
    ]
    assert [account.api_key for account in settings.binance_accounts] == [
        "key-1",
        "key-2",
    ]
    assert settings.binance_accounts[0].allowed_symbols == frozenset(
        {"ETHUSDT", "SOLUSDT"}
    )
    assert settings.binance_accounts[1].allowed_symbols == frozenset({"ETHUSDT"})
