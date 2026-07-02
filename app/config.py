from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import re
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator


load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    import os

    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, not {value!r}")


def _env_list(name: str) -> tuple[str, ...]:
    import os

    value = os.getenv(name, "")
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _account_env_prefix(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _parse_symbols(value: str) -> frozenset[str]:
    return frozenset(x.strip().upper() for x in value.split(",") if x.strip())


class BinanceAccount(BaseModel):
    name: str = Field(min_length=1)
    api_key: str = ""
    api_secret: str = ""
    allowed_symbols: frozenset[str] | None = None
    amount_multiplier: Decimal = Field(default=Decimal("1"), gt=0)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("account name is required")
        return normalized


class Settings(BaseModel):
    webhook_secret: str = Field(default="change_me")
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_accounts: tuple[BinanceAccount, ...] = ()
    binance_base_url: str = "https://fapi.binance.com"
    allowed_symbols: frozenset[str] = frozenset({"BTCUSDT", "ETHUSDT"})
    allow_add: bool = True
    dry_run: bool = True
    recv_window: int = Field(default=5000, ge=1, le=60000)
    position_poll_seconds: float = Field(default=5, gt=0)
    position_poll_interval: float = Field(default=0.5, gt=0)
    bracket_poll_interval: float = Field(default=1.0, gt=0)
    entry_order_timeout_seconds: float = Field(default=1800.0, gt=0)
    algo_working_type: Literal["MARK_PRICE", "CONTRACT_PRICE"] = "MARK_PRICE"
    algo_price_protect: bool = False
    sqlite_path: Path = Path("./bridge.db")

    @field_validator("allowed_symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: object) -> object:
        if isinstance(value, str):
            return _parse_symbols(value)
        return value

    def effective_binance_accounts(self) -> tuple[BinanceAccount, ...]:
        if self.binance_accounts:
            return self.binance_accounts
        return (
            BinanceAccount(
                name="default",
                api_key=self.binance_api_key,
                api_secret=self.binance_api_secret,
            ),
        )

    def for_binance_account(self, account: BinanceAccount) -> "Settings":
        return self.model_copy(
            update={
                "binance_api_key": account.api_key,
                "binance_api_secret": account.api_secret,
                "allowed_symbols": account.allowed_symbols or self.allowed_symbols,
            }
        )

    @classmethod
    def from_env(cls) -> "Settings":
        import os

        account_names = _env_list("BINANCE_ACCOUNT_NAMES")
        accounts = tuple(
            BinanceAccount(
                name=name,
                api_key=os.getenv(f"BINANCE_{_account_env_prefix(name)}_API_KEY", ""),
                api_secret=os.getenv(
                    f"BINANCE_{_account_env_prefix(name)}_API_SECRET", ""
                ),
                allowed_symbols=(
                    _parse_symbols(symbols)
                    if (
                        symbols := os.getenv(
                            f"BINANCE_{_account_env_prefix(name)}_ALLOWED_SYMBOLS"
                        )
                    )
                    else None
                ),
                amount_multiplier=os.getenv(
                    f"BINANCE_{_account_env_prefix(name)}_AMOUNT_MULTIPLIER", "1"
                ),
            )
            for name in account_names
        )
        return cls(
            webhook_secret=os.getenv("WEBHOOK_SECRET", "change_me"),
            binance_api_key=os.getenv("BINANCE_API_KEY", ""),
            binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
            binance_accounts=accounts,
            binance_base_url=os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com"),
            allowed_symbols=os.getenv("ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT"),
            allow_add=_env_bool("ALLOW_ADD", True),
            dry_run=_env_bool("DRY_RUN", True),
            recv_window=os.getenv("RECV_WINDOW", "5000"),
            position_poll_seconds=os.getenv("POSITION_POLL_SECONDS", "5"),
            position_poll_interval=os.getenv("POSITION_POLL_INTERVAL", "0.5"),
            bracket_poll_interval=os.getenv("BRACKET_POLL_INTERVAL", "1"),
            entry_order_timeout_seconds=os.getenv(
                "ENTRY_ORDER_TIMEOUT_SECONDS", "1800"
            ),
            algo_working_type=os.getenv("ALGO_WORKING_TYPE", "MARK_PRICE"),
            algo_price_protect=_env_bool("ALGO_PRICE_PROTECT", False),
            sqlite_path=Path(os.getenv("SQLITE_PATH", "./bridge.db")),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
