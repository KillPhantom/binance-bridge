from __future__ import annotations

from functools import lru_cache
from pathlib import Path
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


class Settings(BaseModel):
    webhook_secret: str = Field(default="change_me")
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_base_url: str = "https://fapi.binance.com"
    allowed_symbols: frozenset[str] = frozenset({"BTCUSDT", "ETHUSDT"})
    allow_add: bool = True
    dry_run: bool = True
    recv_window: int = Field(default=5000, ge=1, le=60000)
    position_poll_seconds: float = Field(default=5, gt=0)
    position_poll_interval: float = Field(default=0.5, gt=0)
    bracket_poll_interval: float = Field(default=1.0, gt=0)
    entry_order_timeout_seconds: float = Field(default=360.0, gt=0)
    algo_working_type: Literal["MARK_PRICE", "CONTRACT_PRICE"] = "MARK_PRICE"
    algo_price_protect: bool = False
    sqlite_path: Path = Path("./bridge.db")

    @field_validator("allowed_symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(x.strip().upper() for x in value.split(",") if x.strip())
        return value

    @classmethod
    def from_env(cls) -> "Settings":
        import os

        return cls(
            webhook_secret=os.getenv("WEBHOOK_SECRET", "change_me"),
            binance_api_key=os.getenv("BINANCE_API_KEY", ""),
            binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
            binance_base_url=os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com"),
            allowed_symbols=os.getenv("ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT"),
            allow_add=_env_bool("ALLOW_ADD", True),
            dry_run=_env_bool("DRY_RUN", True),
            recv_window=os.getenv("RECV_WINDOW", "5000"),
            position_poll_seconds=os.getenv("POSITION_POLL_SECONDS", "5"),
            position_poll_interval=os.getenv("POSITION_POLL_INTERVAL", "0.5"),
            bracket_poll_interval=os.getenv("BRACKET_POLL_INTERVAL", "1"),
            entry_order_timeout_seconds=os.getenv(
                "ENTRY_ORDER_TIMEOUT_SECONDS", "360"
            ),
            algo_working_type=os.getenv("ALGO_WORKING_TYPE", "MARK_PRICE"),
            algo_price_protect=_env_bool("ALGO_PRICE_PROTECT", False),
            sqlite_path=Path(os.getenv("SQLITE_PATH", "./bridge.db")),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
