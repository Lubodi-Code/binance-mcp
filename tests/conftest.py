import os
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("BINANCE_") or name in server.LEGACY_ENV:
            monkeypatch.delenv(name, raising=False)

    def unexpected_request(request):
        pytest.fail(f"Unexpected HTTP request: {request.method} {request.url.path}")

    monkeypatch.setattr(
        server, "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)),
    )


@pytest.fixture
def mock_http(monkeypatch):
    def install(handler):
        requests = []

        def record(request):
            requests.append(request)
            return handler(request)

        monkeypatch.setattr(
            server, "_make_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(record)),
        )
        return requests

    return install


@pytest.fixture
def credentials(monkeypatch):
    key, secret = "KEYFAKE1234567890", "SECRETFAKE0987654321"
    monkeypatch.setenv("BINANCE_API_KEY", key)
    monkeypatch.setenv("BINANCE_API_SECRET", secret)
    return key, secret


@pytest.fixture
def trading(monkeypatch, credentials):
    monkeypatch.setenv("BINANCE_ALLOW_TRADING", "true")


@pytest.fixture
def symbol_info():
    return {
        "symbol": "BTCUSDT", "status": "TRADING",
        "isSpotTradingAllowed": True, "orderTypes": ["MARKET", "LIMIT"],
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0", "maxPrice": "1000000", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
            {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "9000000",
             "applyMinToMarket": True, "applyMaxToMarket": False, "avgPriceMins": 5},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
        ],
    }


@pytest.fixture
def order_http(mock_http, symbol_info):
    def install(order_handler=None):
        def handler(request):
            if request.method == "GET" and request.url.path == "/api/v3/exchangeInfo":
                return httpx.Response(200, json={"symbols": [symbol_info]})
            if request.method == "GET" and request.url.path == "/api/v3/referencePrice":
                return httpx.Response(200, json={"referencePrice": "10000"})
            if request.url.path == "/api/v3/order":
                if order_handler is not None:
                    return order_handler(request)
                return httpx.Response(200, json={"orderId": 123, "status": "NEW"})
            pytest.fail(f"Unexpected route: {request.method} {request.url.path}")

        return mock_http(handler)

    return install
