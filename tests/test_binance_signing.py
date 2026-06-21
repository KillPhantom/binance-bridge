import hashlib
import hmac
from urllib.parse import urlencode

import httpx
import pytest

from app.binance_client import BinanceClient
from app.config import Settings


@pytest.mark.asyncio
async def test_signed_request_uses_hmac_sha256(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("app.binance_client.time.time", lambda: 1700000000.0)
    settings = Settings(
        binance_api_key="key",
        binance_api_secret="secret",
        dry_run=False,
    )
    http = httpx.AsyncClient(
        base_url=settings.binance_base_url, transport=httpx.MockTransport(handler)
    )
    client = BinanceClient(settings, http)
    await client.signed_request("GET", "/fapi/v3/positionRisk", {"symbol": "BTCUSDT"})

    query = urlencode({"symbol": "BTCUSDT", "timestamp": 1700000000000, "recvWindow": 5000})
    expected = hmac.new(b"secret", query.encode(), hashlib.sha256).hexdigest()
    assert seen["request"].headers["X-MBX-APIKEY"] == "key"
    assert seen["request"].url.params["signature"] == expected
    await http.aclose()
