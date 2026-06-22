"""
Binance MCP Server
==================
Servidor MCP que le da a Claude acceso a la REST API de Binance (Spot).

Seguridad por defecto:
- Los retiros (withdrawals) estan DESACTIVADOS a nivel de codigo (ENABLE_WITHDRAWALS=false).
- Recomendado: crear la API key en Binance SIN permiso de retiro y con IP whitelist.
- Usa testnet primero (BINANCE_TESTNET=true) antes de tocar fondos reales.

Variables de entorno requeridas:
  BINANCE_API_KEY      -> tu API key
  BINANCE_API_SECRET   -> tu secret key (HMAC)
Opcionales:
  BINANCE_TESTNET      -> "true" para usar el testnet de spot (default: true)
  ENABLE_WITHDRAWALS   -> "true" para habilitar retiros (default: false). NO lo actives sin entender el riesgo.
  BINANCE_RECV_WINDOW  -> ventana de validez en ms (default: 5000)
"""

import hashlib
import hmac
import os
import time
import urllib.parse
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# --------------------------------------------------------------------------- #
# Configuracion
# --------------------------------------------------------------------------- #
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
TESTNET = os.environ.get("BINANCE_TESTNET", "true").lower() == "true"
ENABLE_WITHDRAWALS = os.environ.get("ENABLE_WITHDRAWALS", "false").lower() == "true"
RECV_WINDOW = int(os.environ.get("BINANCE_RECV_WINDOW", "5000"))

# El testnet de spot usa un host distinto. Datos publicos van por data-api.binance.vision.
BASE_URL = "https://testnet.binance.vision" if TESTNET else "https://api.binance.com"

mcp = FastMCP("binance")


# --------------------------------------------------------------------------- #
# Helpers de firma y request
# --------------------------------------------------------------------------- #
def _sign(params: dict[str, Any]) -> str:
    """Genera la firma HMAC SHA256 sobre el query string (totalParams)."""
    query = urllib.parse.urlencode(params)
    return hmac.new(
        API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def _request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    signed: bool = False,
) -> Any:
    """Hace un request a Binance. Si signed=True, agrega timestamp + firma."""
    params = dict(params or {})
    headers = {}

    if signed:
        if not API_KEY or not API_SECRET:
            return {"error": "Faltan BINANCE_API_KEY / BINANCE_API_SECRET en el entorno."}
        headers["X-MBX-APIKEY"] = API_KEY
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = RECV_WINDOW
        params["signature"] = _sign(params)
    elif API_KEY:
        # Algunos endpoints publicos dan mas info con la key presente.
        headers["X-MBX-APIKEY"] = API_KEY

    url = f"{BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.request(method, url, params=params, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {"status_code": resp.status_code, "text": resp.text}
        if resp.status_code >= 400:
            return {"error": True, "status_code": resp.status_code, "binance": data}
        return data


# --------------------------------------------------------------------------- #
# Tools PUBLICAS (no requieren firma)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_price(symbol: str) -> dict:
    """Precio actual de un simbolo. Ej: symbol='BTCUSDT'."""
    return await _request("GET", "/api/v3/ticker/price", {"symbol": symbol.upper()})


@mcp.tool()
async def get_24h_stats(symbol: str) -> dict:
    """Estadisticas de 24h de un simbolo (volumen, cambio %, high/low). Ej: 'ETHUSDT'."""
    return await _request("GET", "/api/v3/ticker/24hr", {"symbol": symbol.upper()})


@mcp.tool()
async def get_klines(symbol: str, interval: str = "1h", limit: int = 100) -> Any:
    """
    Velas (candlesticks) de un simbolo.
    interval: 1m,5m,15m,1h,4h,1d,1w, etc. limit: max 1000.
    """
    return await _request(
        "GET",
        "/api/v3/klines",
        {"symbol": symbol.upper(), "interval": interval, "limit": min(limit, 1000)},
    )


@mcp.tool()
async def get_order_book(symbol: str, limit: int = 20) -> dict:
    """Libro de ordenes (bids/asks) de un simbolo. limit: 5,10,20,50,100,500,1000."""
    return await _request(
        "GET", "/api/v3/depth", {"symbol": symbol.upper(), "limit": limit}
    )


# --------------------------------------------------------------------------- #
# Tools de CUENTA (firmadas, solo lectura)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def get_account() -> dict:
    """Balances y permisos de tu cuenta spot. Requiere firma."""
    return await _request("GET", "/api/v3/account", signed=True)


@mcp.tool()
async def get_open_orders(symbol: str | None = None) -> Any:
    """Ordenes abiertas. Si pasas symbol, filtra por ese par."""
    params = {"symbol": symbol.upper()} if symbol else {}
    return await _request("GET", "/api/v3/openOrders", params, signed=True)


@mcp.tool()
async def get_my_trades(symbol: str, limit: int = 50) -> Any:
    """Historial de trades tuyos para un simbolo. Requiere firma."""
    return await _request(
        "GET",
        "/api/v3/myTrades",
        {"symbol": symbol.upper(), "limit": min(limit, 1000)},
        signed=True,
    )


# --------------------------------------------------------------------------- #
# Tools de TRADING (firmadas, MODIFICAN la cuenta)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def place_order(
    symbol: str,
    side: str,
    order_type: str = "MARKET",
    quantity: float | None = None,
    price: float | None = None,
    time_in_force: str = "GTC",
) -> dict:
    """
    Crea una orden de compra/venta.
      symbol: ej 'BTCUSDT'
      side: 'BUY' o 'SELL'
      order_type: 'MARKET' o 'LIMIT'
      quantity: cantidad del activo base
      price: requerido solo para LIMIT
      time_in_force: GTC / IOC / FOK (solo LIMIT)
    """
    params: dict[str, Any] = {
        "symbol": symbol.upper(),
        "side": side.upper(),
        "type": order_type.upper(),
    }
    if quantity is not None:
        params["quantity"] = quantity
    if order_type.upper() == "LIMIT":
        if price is None:
            return {"error": "Las ordenes LIMIT requieren price."}
        params["price"] = price
        params["timeInForce"] = time_in_force.upper()
    return await _request("POST", "/api/v3/order", params, signed=True)


@mcp.tool()
async def cancel_order(symbol: str, order_id: int) -> dict:
    """Cancela una orden por su orderId en el simbolo dado."""
    return await _request(
        "DELETE",
        "/api/v3/order",
        {"symbol": symbol.upper(), "orderId": order_id},
        signed=True,
    )


@mcp.tool()
async def cancel_all_orders(symbol: str) -> Any:
    """Cancela TODAS las ordenes abiertas de un simbolo."""
    return await _request(
        "DELETE", "/api/v3/openOrders", {"symbol": symbol.upper()}, signed=True
    )


# --------------------------------------------------------------------------- #
# RETIROS (desactivados por defecto -- el dinero que puede salir de tu cuenta)
# --------------------------------------------------------------------------- #
@mcp.tool()
async def withdraw(coin: str, address: str, amount: float, network: str | None = None) -> dict:
    """
    Retira fondos a una direccion externa.
    DESACTIVADO por defecto. Para usarlo: ENABLE_WITHDRAWALS=true y la API key
    debe tener permiso de retiro. Entende el riesgo antes de activarlo.
    """
    if not ENABLE_WITHDRAWALS:
        return {
            "error": "Retiros desactivados. Pone ENABLE_WITHDRAWALS=true para habilitar. "
            "Recomendacion: mejor no le des permiso de retiro a una API key usada por un LLM."
        }
    params: dict[str, Any] = {"coin": coin.upper(), "address": address, "amount": amount}
    if network:
        params["network"] = network
    return await _request("POST", "/sapi/v1/capital/withdraw/apply", params, signed=True)


if __name__ == "__main__":
    mcp.run()
