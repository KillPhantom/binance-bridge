import pytest
from decimal import Decimal

from app.config import Settings


def test_algo_orders_use_contract_price_by_default():
    assert Settings().algo_working_type == "CONTRACT_PRICE"


def test_invalid_dry_run_value_fails_closed(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "treu")
    with pytest.raises(ValueError, match="DRY_RUN"):
        Settings.from_env()


def test_named_binance_accounts_are_read_from_env(monkeypatch):
    monkeypatch.setenv("BINANCE_ACCOUNT_NAMES", "primary, copy-trader")
    monkeypatch.setenv("BINANCE_PRIMARY_API_KEY", "key-1")
    monkeypatch.setenv("BINANCE_PRIMARY_API_SECRET", "secret-1")
    monkeypatch.setenv(
        "BINANCE_PRIMARY_MANUAL_TOKEN",
        "primary-manual-token-1234567890-ab",
    )
    monkeypatch.setenv("BINANCE_PRIMARY_ALLOWED_SYMBOLS", "ETHUSDT,SOLUSDT")
    monkeypatch.setenv("BINANCE_PRIMARY_AMOUNT_MULTIPLIER", "1")
    monkeypatch.setenv("BINANCE_COPY_TRADER_API_KEY", "key-2")
    monkeypatch.setenv("BINANCE_COPY_TRADER_API_SECRET", "secret-2")
    monkeypatch.setenv("BINANCE_COPY_TRADER_ALLOWED_SYMBOLS", "ETHUSDT")
    monkeypatch.setenv("BINANCE_COPY_TRADER_AMOUNT_MULTIPLIER", "10")
    monkeypatch.setenv("PROTECTED_RECONCILE_INTERVAL", "30")

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
    assert (
        settings.binance_accounts[0].manual_token
        == "primary-manual-token-1234567890-ab"
    )
    assert settings.binance_accounts[1].allowed_symbols == frozenset({"ETHUSDT"})
    assert settings.binance_accounts[0].amount_multiplier == Decimal("1")
    assert settings.binance_accounts[1].amount_multiplier == Decimal("10")
    assert settings.protected_reconcile_interval == 30


def test_short_manual_token_is_rejected(monkeypatch):
    monkeypatch.delenv("BINANCE_ACCOUNT_NAMES", raising=False)
    monkeypatch.setenv("BINANCE_MANUAL_TOKEN", "too-short")
    with pytest.raises(ValueError, match="at least 32"):
        Settings.from_env().effective_binance_accounts()
