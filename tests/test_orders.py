import asyncio
from decimal import Decimal

import httpx
import pytest

import server


@pytest.mark.parametrize("value", ["0.1", 0.1])
def test_decimal_conversion_preserves_precision(value):
    assert server._to_decimal("quantity", value) == Decimal("0.1")


@pytest.mark.parametrize("value", ["1e-5", "-1", "0", "abc", True, float("nan"), float("inf")])
def test_decimal_conversion_rejects_invalid_values(value):
    with pytest.raises(server.ToolError) as exc:
        server._to_decimal("quantity", value)
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("quantity,price,filter_name", [
    ("1", "10.001", "tickSize"),
    ("0.000011", "100000", "stepSize"),
    ("0.001", "1000", "minNotional"),
])
def test_limit_filter_rejections(symbol_info, quantity, price, filter_name):
    with pytest.raises(server.ToolError) as exc:
        server._validate_order(symbol_info, "LIMIT", Decimal(quantity), Decimal(price))
    assert exc.value.code == "VALIDATION_ERROR"
    assert filter_name in exc.value.message


def test_zero_lot_step_is_unresolved(symbol_info):
    symbol_info["filters"][1]["stepSize"] = "0.00000000"
    with pytest.raises(server.ToolError) as exc:
        server._validate_order(symbol_info, "LIMIT", Decimal("1"), Decimal("10"))
    assert exc.value.code == "VALIDATION_UNRESOLVED"


@pytest.mark.parametrize("verified,price,status", [
    (False, "1", "incomplete"), (True, "10", "complete"),
    (True, "10000000", "complete"),
])
def test_market_reference_and_optional_maximum(symbol_info, verified, price, status):
    reference = server.MarketReference(Decimal(price), "fixture", verified, "unverified price")
    result = server._validate_order(symbol_info, "MARKET", Decimal("1"), None, {"NOTIONAL": reference})
    assert result["status"] == status
    assert result["note"] == server.VALIDATION_NOTE
    if not verified:
        assert any(item["filter"] == "NOTIONAL" for item in result["not_checked"])
        assert "NOTIONAL" not in result["checked"]
    else:
        assert "NOTIONAL" in result["checked"]
    assert "MARKET_LOT_SIZE" in result["checked"]


def test_verified_market_reference_rejects_low_notional(symbol_info):
    reference = server.MarketReference(Decimal("1"), "fixture", True)
    with pytest.raises(server.ToolError) as exc:
        server._validate_order(symbol_info, "MARKET", Decimal("1"), None, {"NOTIONAL": reference})
    assert exc.value.code == "VALIDATION_ERROR"
    assert "minNotional" in exc.value.message


def test_zero_min_price_disables_lower_bound(symbol_info):
    # PRICE_FILTER documents zero as disabling this bound.
    assert symbol_info["filters"][0]["minPrice"] == "0"
    result = server._validate_order(symbol_info, "LIMIT", Decimal("500"), Decimal("0.01"))
    assert result["status"] == "complete"
    assert "PRICE_FILTER" in result["checked"]
    assert result["note"] == server.VALIDATION_NOTE


@pytest.mark.parametrize("order_type", ["MARKET", "LIMIT"])
def test_order_precision_and_default_time_in_force(trading, order_http, order_type):
    requests = order_http()
    kwargs = {"price": "10000.00"} if order_type == "LIMIT" else {}
    result = asyncio.run(server.place_order("btcusdt", "BUY", order_type, "0.00100000", **kwargs))
    assert result["ok"] is True
    assert result["data"]["validation"]["note"] == server.VALIDATION_NOTE
    posts = [r for r in requests if r.method == "POST"]
    assert len(posts) == 1
    request = posts[0]
    assert request.url.path == "/api/v3/order"
    assert request.url.params["quantity"] == "0.00100000"
    assert "quantity=0.00100000" in str(request.url)
    assert request.url.params["symbol"] == "BTCUSDT"
    assert request.url.params["newClientOrderId"] == result["data"]["client_order_id"]
    assert result["data"]["order"]["orderId"] == 123
    if order_type == "LIMIT":
        assert request.url.params["timeInForce"] == "GTC"
        assert request.url.params["price"] == "10000.00"
    else:
        assert "timeInForce" not in request.url.params
        assert "price" not in request.url.params


@pytest.mark.parametrize("order_type,kwargs", [
    ("MARKET", {"price": "10000"}), ("LIMIT", {}),
])
def test_order_type_validation_before_http(trading, mock_http, order_type, kwargs):
    requests = mock_http(lambda request: pytest.fail("Invalid order must not reach HTTP"))
    result = asyncio.run(server.place_order("BTCUSDT", "BUY", order_type, "0.001", **kwargs))
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert requests == []


@pytest.mark.parametrize("failure", ["timeout", "503", "non_json"])
def test_uncertain_order_is_not_retried(trading, order_http, failure):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("read timed out", request=request)
        if failure == "503":
            return httpx.Response(503, json={"msg": "Unavailable"})
        return httpx.Response(200, text="not JSON")

    requests = order_http(handler)
    result = asyncio.run(server.place_order("BTCUSDT", "BUY", "LIMIT", "0.001", "10000"))
    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "OUTCOME_UNKNOWN"
    assert error["outcome"] == "unknown"
    assert error["symbol"] == "BTCUSDT"
    assert error["client_order_id"].startswith("mcp-")
    posts = [r for r in requests if r.method == "POST"]
    assert len(posts) == 1
    assert posts[0].url.path == "/api/v3/order"
    assert posts[0].url.params["newClientOrderId"] == error["client_order_id"]
    assert len(requests) == 2  # exchangeInfo, then the single order attempt.


def test_cancel_timeout_preserves_original_identifiers(trading, mock_http):
    def handler(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    requests = mock_http(handler)
    result = asyncio.run(server.cancel_order("BTCUSDT", 456))
    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == "OUTCOME_UNKNOWN"
    assert error["outcome"] == "unknown"
    assert (error["symbol"], error["order_id"]) == ("BTCUSDT", 456)
    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert requests[0].url.params["orderId"] == "456"


def test_connection_failure_means_order_not_sent(trading, order_http):
    def handler(request):
        raise httpx.ConnectError("connection failed", request=request)

    requests = order_http(handler)
    result = asyncio.run(server.place_order("BTCUSDT", "BUY", "LIMIT", "0.001", "10000"))
    assert result["ok"] is False
    assert result["error"]["code"] == "NETWORK_ERROR"
    assert result["error"]["sent"] is False
    assert len([r for r in requests if r.method == "POST"]) == 1


@pytest.mark.parametrize("kwargs", [{}, {"order_id": 123, "client_order_id": "mcp-example"}])
def test_get_order_requires_exactly_one_identifier(mock_http, kwargs):
    requests = mock_http(lambda request: pytest.fail("Invalid identifiers must block HTTP"))
    result = asyncio.run(server.get_order("BTCUSDT", **kwargs))
    assert result["ok"] is False
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert requests == []


@pytest.mark.parametrize("kwargs,param,value", [
    ({"order_id": 123}, "orderId", "123"),
    ({"client_order_id": "mcp-example"}, "origClientOrderId", "mcp-example"),
])
def test_get_order_sends_selected_identifier(credentials, mock_http, kwargs, param, value):
    payload = {"orderId": 123, "status": "FILLED"}
    requests = mock_http(lambda request: httpx.Response(200, json=payload))
    result = asyncio.run(server.get_order("btcusdt", **kwargs))
    assert result["ok"] is True
    assert result["data"] == payload
    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.url.path) == ("GET", "/api/v3/order")
    assert request.url.params[param] == value
    assert ("origClientOrderId" if param == "orderId" else "orderId") not in request.url.params
    assert request.url.params["symbol"] == "BTCUSDT"
    assert "signature" in request.url.params


def _cfg():
    return server._load_config({"BINANCE_TESTNET": "true"})


@pytest.mark.parametrize("routes,verified,source,price", [
    # referencePrice no nulo: se usa tal cual.
    ({"/api/v3/referencePrice": (200, {"referencePrice": "100.5"})}, True, "referencePrice", "100.5"),
    # -2043 (sin referencia) y avgPrice con la ventana exigida por el filtro.
    ({"/api/v3/referencePrice": (400, {"code": -2043, "msg": "no ref"}),
      "/api/v3/avgPrice": (200, {"mins": 5, "price": "99.1"})}, True, "avgPrice 5m", "99.1"),
    # Ventana distinta a avgPriceMins: no se puede comprobar.
    ({"/api/v3/referencePrice": (200, {"referencePrice": None}),
      "/api/v3/avgPrice": (200, {"mins": 3, "price": "99.1"})}, False, "avgPrice", None),
    # Endpoint inexistente en la red: validacion incompleta, no se asume nada.
    ({"/api/v3/referencePrice": (404, {"code": -1, "msg": "not found"})}, False, "referencePrice", None),
])
def test_market_reference_resolution(mock_http, routes, verified, source, price):
    def handler(request):
        status, body = routes[request.url.path]
        return httpx.Response(status, json=body)

    mock_http(handler)
    ref = asyncio.run(server._resolve_market_reference(_cfg(), "BTCUSDT", 5))
    assert (ref.verified, ref.source) == (verified, source)
    assert ref.price == (Decimal(price) if price else None)


def test_market_reference_zero_window_uses_last_price(mock_http):
    def handler(request):
        if request.url.path == "/api/v3/referencePrice":
            return httpx.Response(400, json={"code": -2043, "msg": "no ref"})
        assert request.url.path == "/api/v3/ticker/price"
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "77.7"})

    mock_http(handler)
    ref = asyncio.run(server._resolve_market_reference(_cfg(), "BTCUSDT", 0))
    assert (ref.verified, ref.source, ref.price) == (True, "lastPrice", Decimal("77.7"))


def test_binance_internal_timeout_on_order_is_unknown(trading, order_http):
    order_http(lambda request: httpx.Response(400, json={"code": -1007, "msg": "Timeout waiting for response"}))
    result = asyncio.run(server.place_order("BTCUSDT", "BUY", "LIMIT", "0.001", "10000"))
    assert result["error"]["code"] == "OUTCOME_UNKNOWN"
    assert result["error"]["client_order_id"].startswith("mcp-")


@pytest.mark.parametrize("tool,args", [
    ("place_order", {"symbol": "BTCUSDT", "side": "BUY", "quantity": True}),
    ("place_order", {"symbol": "BTCUSDT", "side": "BUY", "order_type": "LIMIT", "quantity": "1", "price": True}),
    ("cancel_order", {"symbol": "BTCUSDT", "order_id": True}),
    ("get_order", {"symbol": "BTCUSDT", "order_id": True}),
    ("get_klines", {"symbol": "BTCUSDT", "limit": True}),
])
def test_mcp_arguments_reject_booleans(tool, args):
    # Revision de Codex: Pydantic en modo laxo convertia true -> 1 (una orden de 1 BTC).
    from pydantic import ValidationError
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    with pytest.raises(ValidationError):
        func_metadata(getattr(server, tool)).arg_model.model_validate(args)


def test_empty_time_in_force_is_rejected_not_defaulted(trading, mock_http):
    requests = mock_http(lambda request: pytest.fail("Invalid order must not reach HTTP"))
    result = asyncio.run(server.place_order("BTCUSDT", "BUY", "LIMIT", "0.001", "10000", ""))
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert requests == []
