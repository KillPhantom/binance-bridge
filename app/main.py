from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from .binance_client import BinanceClient
from .bracket_worker import BracketWorker
from .config import BinanceAccount, Settings, get_settings
from .db import EventStore
from .logger import configure_logging
from .models import TradingViewSignal
from .risk_engine import RiskEngine


configure_logging()
logger = logging.getLogger(__name__)


@dataclass
class AccountRuntime:
    name: str
    amount_multiplier: Decimal
    settings: Settings
    store: EventStore
    client: BinanceClient
    engine: RiskEngine
    bracket_worker: BracketWorker


def _safe_account_suffix(account_name: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", account_name).strip("._-")
    return suffix or "account"


def _account_sqlite_path(base_path: Path, account_name: str) -> Path:
    suffix = _safe_account_suffix(account_name)
    extension = base_path.suffix or ".db"
    return base_path.with_name(f"{base_path.stem}-{suffix}{extension}")


def _build_account_runtime(
    base_settings: Settings,
    account: BinanceAccount,
    store: EventStore | None = None,
    client: BinanceClient | None = None,
    use_base_store_path: bool = False,
) -> AccountRuntime:
    account_settings = base_settings.for_binance_account(account)
    if not use_base_store_path:
        account_settings = account_settings.model_copy(
            update={
                "sqlite_path": _account_sqlite_path(
                    account_settings.sqlite_path, account.name
                )
            }
        )
    account_store = store or EventStore(account_settings.sqlite_path)
    account_client = client or BinanceClient(account_settings)
    engine = RiskEngine(account_client, account_settings)
    bracket_worker = BracketWorker(
        account_client, account_store, account_settings, engine.lock_for
    )
    return AccountRuntime(
        name=account.name,
        amount_multiplier=account.amount_multiplier,
        settings=account_settings,
        store=account_store,
        client=account_client,
        engine=engine,
        bracket_worker=bracket_worker,
    )


def _build_account_runtimes(
    settings: Settings,
    store: EventStore | None,
    client: BinanceClient | None,
    clients: dict[str, BinanceClient] | None,
) -> list[AccountRuntime]:
    if client is not None and clients is not None:
        raise RuntimeError("pass either client or clients, not both")
    accounts = settings.effective_binance_accounts()
    if client is not None:
        return [
            _build_account_runtime(
                settings, accounts[0], store=store, client=client, use_base_store_path=True
            )
        ]
    return [
        _build_account_runtime(
            settings,
            account,
            store=store if index == 0 and len(accounts) == 1 else None,
            client=clients.get(account.name) if clients else None,
            use_base_store_path=len(accounts) == 1,
        )
        for index, account in enumerate(accounts)
    ]


def create_app(
    settings: Settings | None = None,
    store: EventStore | None = None,
    client: BinanceClient | None = None,
    clients: dict[str, BinanceClient] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if not settings.dry_run:
        if settings.webhook_secret == "change_me":
            raise RuntimeError("WEBHOOK_SECRET must be changed before disabling DRY_RUN")
        for account in settings.effective_binance_accounts():
            if not account.api_key or not account.api_secret:
                raise RuntimeError(
                    f"Binance API credentials are required for account {account.name!r} "
                    "when DRY_RUN=false"
                )
    runtimes = _build_account_runtimes(settings, store, client, clients)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker_tasks = [
            asyncio.create_task(runtime.bracket_worker.run())
            for runtime in runtimes
        ]
        try:
            yield
        finally:
            for runtime in runtimes:
                runtime.bracket_worker.stop()
            await asyncio.gather(*worker_tasks)
            await asyncio.gather(*(runtime.client.close() for runtime in runtimes))

    app = FastAPI(title="TradingView Binance Bridge", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.accounts = runtimes
    app.state.store = runtimes[0].store
    app.state.binance = runtimes[0].client
    app.state.risk_engine = runtimes[0].engine
    app.state.bracket_worker = runtimes[0].bracket_worker

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    async def execute_for_account(
        runtime: AccountRuntime, signal: TradingViewSignal, payload: dict[str, Any]
    ) -> dict[str, Any]:
        account_signal = signal
        account_payload = payload
        if not signal.reduce_only and runtime.amount_multiplier != Decimal("1"):
            adjusted_amount = signal.amount * runtime.amount_multiplier
            account_signal = signal.model_copy(update={"amount": adjusted_amount})
            account_payload = dict(payload)
            account_payload["amount"] = str(adjusted_amount)

        logger.info(
            "webhook account=%s event_id=%s symbol=%s side=%s reduce_only=%s price=%s amount=%s multiplier=%s",
            runtime.name,
            account_signal.event_id,
            account_signal.symbol,
            account_signal.side,
            account_signal.reduce_only,
            account_signal.price,
            account_signal.amount,
            runtime.amount_multiplier,
        )

        claim = runtime.store.claim(account_signal, account_payload)
        if claim == "duplicate_success":
            return {
                "account": runtime.name,
                "status": "success",
                "duplicate": True,
                "summary": "event already executed",
            }
        if claim == "processing":
            return {
                "account": runtime.name,
                "status": "conflict",
                "summary": "event is already processing",
            }
        if claim == "failed_needs_retry":
            return {
                "account": runtime.name,
                "status": "conflict",
                "summary": "failed event requires retry=true",
            }

        try:
            def finalize_execution(result):
                if runtime.settings.dry_run:
                    return
                if account_signal.reduce_only:
                    runtime.store.deactivate_active_brackets(
                        account_signal.symbol, "manual_reduce"
                    )
                    return
                order_response = (
                    result.binance_responses[-1] if result.binance_responses else None
                )
                if order_response is not None:
                    order_id = order_response.get("orderId")
                    if order_id is None:
                        raise RuntimeError("Binance opening order response has no orderId")
                    if result.entry_fill_price is None:
                        raise RuntimeError(
                            "Binance opening order response has no fill price"
                        )
                    stop_loss_price = (
                        result.entry_fill_price
                        + account_signal.stop_loss_price
                        - account_signal.price
                    )
                    take_profit_price = (
                        result.entry_fill_price
                        + account_signal.take_profit_price
                        - account_signal.price
                    )
                    if stop_loss_price <= 0 or take_profit_price <= 0:
                        raise RuntimeError(
                            "market-adjusted protection price must be positive"
                        )
                    runtime.store.create_bracket(
                        account_signal.event_id,
                        account_signal.symbol,
                        int(order_id),
                        account_signal.side.upper(),
                        str(stop_loss_price),
                        str(take_profit_price),
                    )

            result = await runtime.engine.handle_signal(account_signal, finalize_execution)
            runtime.store.mark_success(
                account_signal.event_id,
                result.position_before,
                result.position_after,
                result.binance_responses,
            )
            return {
                "account": runtime.name,
                "status": "success",
                "duplicate": False,
                "summary": result.summary,
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            runtime.store.mark_failed(account_signal.event_id, error)
            logger.exception(
                "webhook execution failed account=%s event_id=%s symbol=%s",
                runtime.name,
                account_signal.event_id,
                account_signal.symbol,
            )
            return {
                "account": runtime.name,
                "status": "failed",
                "summary": "execution failed",
                "error": error,
            }

    @app.post("/webhook/tradingview")
    async def tradingview_webhook(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
            signal = TradingViewSignal.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=400, detail="invalid webhook payload") from exc
        if not hmac.compare_digest(signal.token, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="invalid token")
        eligible_runtimes = [
            runtime
            for runtime in runtimes
            if signal.symbol in runtime.settings.allowed_symbols
        ]
        if not eligible_runtimes:
            raise HTTPException(status_code=400, detail="unsupported symbol")

        account_results = await asyncio.gather(
            *(
                execute_for_account(runtime, signal, payload)
                for runtime in eligible_runtimes
            )
        )
        conflicts = [
            result for result in account_results if result["status"] == "conflict"
        ]
        failures = [
            result for result in account_results if result["status"] == "failed"
        ]
        if conflicts:
            detail = (
                conflicts[0]["summary"]
                if len(account_results) == 1
                else {"accounts": conflicts}
            )
            raise HTTPException(status_code=409, detail=detail)
        if failures:
            detail = "execution failed" if len(account_results) == 1 else {
                "accounts": failures
            }
            raise HTTPException(status_code=500, detail=detail)
        if len(runtimes) == 1:
            result = account_results[0]
            response = {
                "ok": True,
                "event_id": signal.event_id,
                "status": "success",
                "summary": result["summary"],
            }
            if result["duplicate"]:
                response["duplicate"] = True
            return response
        return {
            "ok": True,
            "event_id": signal.event_id,
            "status": "success",
            "summary": f"forwarded to {len(account_results)} accounts",
            "accounts": account_results,
        }

    return app


app = create_app()
