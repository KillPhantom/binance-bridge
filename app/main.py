from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from .binance_client import BinanceClient
from .config import Settings, get_settings
from .db import EventStore
from .logger import configure_logging
from .models import TradingViewSignal
from .risk_engine import RiskEngine


configure_logging()
logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    store: EventStore | None = None,
    client: BinanceClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if not settings.dry_run:
        if settings.webhook_secret == "change_me":
            raise RuntimeError("WEBHOOK_SECRET must be changed before disabling DRY_RUN")
        if not settings.binance_api_key or not settings.binance_api_secret:
            raise RuntimeError("Binance API credentials are required when DRY_RUN=false")
    store = store or EventStore(settings.sqlite_path)
    client = client or BinanceClient(settings)
    engine = RiskEngine(client, settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await client.close()

    app = FastAPI(title="TradingView Binance Bridge", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.binance = client
    app.state.risk_engine = engine

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/webhook/tradingview")
    async def tradingview_webhook(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
            signal = TradingViewSignal.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=400, detail="invalid webhook payload") from exc
        if not hmac.compare_digest(signal.token, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="invalid token")
        if signal.symbol not in settings.allowed_symbols:
            raise HTTPException(status_code=400, detail="unsupported symbol")

        claim = store.claim(signal, payload)
        if claim == "duplicate_success":
            return {"ok": True, "event_id": signal.event_id, "status": "success", "duplicate": True,
                    "summary": "event already executed"}
        if claim == "processing":
            raise HTTPException(status_code=409, detail="event is already processing")
        if claim == "failed_needs_retry":
            raise HTTPException(status_code=409, detail="failed event requires retry=true")

        try:
            result = await engine.handle_signal(signal)
            store.mark_success(
                signal.event_id,
                result.position_before,
                result.position_after,
                result.binance_responses,
            )
            return {"ok": True, "event_id": signal.event_id, "status": "success",
                    "summary": result.summary}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            store.mark_failed(signal.event_id, error)
            logger.exception("webhook execution failed event_id=%s symbol=%s", signal.event_id, signal.symbol)
            raise HTTPException(status_code=500, detail="execution failed") from exc

    return app


app = create_app()
