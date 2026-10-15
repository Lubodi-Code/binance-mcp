import asyncio
import json

import httpx
import pytest

import server


@pytest.mark.parametrize("allow", [None, "false"])
@pytest.mark.parametrize("testnet", ["true", "false"])
@pytest.mark.parametrize("tool", ["place_order", "cancel_order", "cancel_all_orders"])
def test_trading_disabled_before_any_http(monkeypatch, mock_http, credentials, allow, testnet, tool):
    monkeypatch.setenv("BINANCE_TESTNET", testnet)
    if allow is not None:
        monkeypatch.setenv("BINANCE_ALLOW_TRADING", allow)
    requests = mock_http(lambda request: pytest.fail("Trading must be blocked before HTTP"))
    args = {
        "place_order": ("BTCUSDT", "BUY"),
        "cancel_order": ("BTCUSDT", 123),
        "cancel_all_orders": ("BTCUSDT",),
    }
    result = asyncio.run(getattr(server, tool)(*args[tool]))
    assert result["ok"] is False
    assert result["error"]["code"] == "TRADING_DISABLED"
    assert result["trading_enabled"] is False
    assert result["network"] == ("testnet" if testnet == "true" else "mainnet")
    assert requests == []


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_send_central_trading_guard(mock_http, method):
    requests = mock_http(lambda request: pytest.fail("Central guard must block HTTP"))
    cfg = server._load_config({"BINANCE_ALLOW_TRADING": "false"})
    with pytest.raises(server.ToolError) as exc:
        asyncio.run(server._send(cfg, method, "/api/v3/order"))
    assert exc.value.code == "TRADING_DISABLED"
    assert requests == []


def test_config_defaults_and_explicit_values():
    cfg = server._load_config({})
    assert (cfg.testnet, cfg.allow_trading, cfg.recv_window, cfg.network) == (True, False, 5000, "testnet")
    assert (cfg.api_key, cfg.api_secret) == ("", "")
    cfg = server._load_config({
        "BINANCE_TESTNET": "false", "BINANCE_ALLOW_TRADING": "true",
        "BINANCE_RECV_WINDOW": "60000", "BINANCE_API_KEY": " key ", "BINANCE_API_SECRET": " secret ",
    })
    assert (cfg.testnet, cfg.allow_trading, cfg.recv_window, cfg.network) == (False, True, 60000, "mainnet")
    assert (cfg.api_key, cfg.api_secret) == ("key", "secret")


@pytest.mark.parametrize("name,value", [
    ("BINANCE_TESTNET", "maybe"), ("BINANCE_ALLOW_TRADING", "maybe"),
    ("BINANCE_RECV_WINDOW", "70000"), ("BINANCE_RECV_WINDOW", "0"),
    ("BINANCE_RECV_WINDOW", "abc"),
])
def test_invalid_config_blocks_execution(monkeypatch, mock_http, name, value):
    with pytest.raises(server.ConfigError):
        server._load_config({name: value})
    monkeypatch.setenv(name, value)
    requests = mock_http(lambda request: pytest.fail("Invalid configuration must block HTTP"))
    result = asyncio.run(server.get_price("BTCUSDT"))
    assert result["ok"] is False
    assert result["error"]["code"] == "CONFIG_ERROR"
    assert requests == []


@pytest.mark.parametrize("testnet,host", [
    ("false", "data-api.binance.vision"), ("true", "testnet.binance.vision"),
])
def test_public_price_without_credentials(monkeypatch, mock_http, testnet, host):
    monkeypatch.setenv("BINANCE_TESTNET", testnet)
    payload = {"symbol": "BTCUSDT", "price": "10000.00"}
    requests = mock_http(lambda request: httpx.Response(200, json=payload))
    result = asyncio.run(server.get_price("btcusdt"))
    assert result["ok"] is True
    assert result["data"] == payload
    assert result["network"] == ("testnet" if testnet == "true" else "mainnet")
    assert result["trading_enabled"] is False
    if testnet == "true":
        assert result["warning"]
    else:
        assert "warning" not in result
    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.url.host, request.url.path) == ("GET", host, "/api/v3/ticker/price")
    assert request.url.params["symbol"] == "BTCUSDT"
    assert "X-MBX-APIKEY" not in request.headers
    assert "signature" not in request.url.params


@pytest.mark.parametrize("missing", ["both", "key", "secret"])
def test_private_query_requires_credentials(monkeypatch, mock_http, credentials, missing):
    if missing in ("both", "key"):
        monkeypatch.delenv("BINANCE_API_KEY")
    if missing in ("both", "secret"):
        monkeypatch.delenv("BINANCE_API_SECRET")
    requests = mock_http(lambda request: pytest.fail("Missing credentials must block HTTP"))
    result = asyncio.run(server.get_account())
    assert result["ok"] is False
    assert result["error"]["code"] == "MISSING_CREDENTIALS"
    assert requests == []


@pytest.mark.parametrize("tool", ["get_account", "diagnose_connection"])
@pytest.mark.parametrize("failure,code", [
    ("binance", "BINANCE_ERROR"), ("timeout", "TIMEOUT"), ("generic", "INTERNAL_ERROR"),
])
def test_error_responses_never_expose_secrets(mock_http, credentials, tool, failure, code):
    key, secret = credentials

    def handler(request):
        if request.url.path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1700000000000})
        if failure == "binance":
            return httpx.Response(400, json={"code": -2015, "msg": f"Invalid API key {key}; secret {secret}"})
        message = f"{key} {secret} {request.url}"
        if failure == "timeout":
            raise httpx.ReadTimeout(message, request=request)
        raise RuntimeError(message)

    requests = mock_http(handler)
    result = asyncio.run(getattr(server, tool)())
    serialized = json.dumps(result)
    assert key not in serialized
    assert secret not in serialized
    assert "signature=" not in serialized
    if tool == "diagnose_connection" and failure != "generic":
        assert result["ok"] is True
        assert result["data"]["private_api"]["status"] == "error"
        assert result["data"]["private_api"]["code"] == code
    else:
        assert result["ok"] is False
        assert result["error"]["code"] == code
    private = [r for r in requests if r.url.path == "/api/v3/account"]
    assert len(private) == 1
    assert private[0].headers["X-MBX-APIKEY"] == key
    assert "signature" in private[0].url.params


@pytest.mark.parametrize("status,payload,headers,code,extra", [
    (429, {}, {"Retry-After": "7"}, "RATE_LIMITED", {"retry_after_seconds": 7}),
    (400, {"code": -1121, "msg": "Invalid symbol."}, {}, "BINANCE_ERROR", {"binance_code": -1121}),
])
def test_http_errors(mock_http, status, payload, headers, code, extra):
    requests = mock_http(lambda request: httpx.Response(status, json=payload, headers=headers))
    result = asyncio.run(server.get_price("BTCUSDT"))
    assert result["ok"] is False
    assert result["error"]["code"] == code
    for name, value in extra.items():
        assert result["error"][name] == value
    assert len(requests) == 1
