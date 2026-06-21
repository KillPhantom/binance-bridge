from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    default_notional_usdt: float = Field(default=80, gt=0)
    allow_add: bool = True
    dry_run: bool = True
    recv_window: int = Field(default=5000, ge=1, le=60000)
    position_poll_seconds: float = Field(default=5, gt=0)
    position_poll_interval: float = Field(default=0.5, gt=0)
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
            default_notional_usdt=os.getenv("DEFAULT_NOTIONAL_USDT", "80"),
            allow_add=_env_bool("ALLOW_ADD", True),
            dry_run=_env_bool("DRY_RUN", True),
            recv_window=os.getenv("RECV_WINDOW", "5000"),
            position_poll_seconds=os.getenv("POSITION_POLL_SECONDS", "5"),
            position_poll_interval=os.getenv("POSITION_POLL_INTERVAL", "0.5"),
            sqlite_path=Path(os.getenv("SQLITE_PATH", "./bridge.db")),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
