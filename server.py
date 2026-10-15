"""
Binance MCP Server
==================
Servidor MCP que le da a un agente de IA acceso a la REST API de Binance (Spot).

Seguridad por defecto:
- Solo lectura: BINANCE_ALLOW_TRADING=false bloquea crear y cancelar ordenes.
- Testnet por defecto: BINANCE_TESTNET=true. Red y permiso de operar son independientes:
  pasar a mainnet NO habilita operaciones.
- No hay herramienta de retiro (eliminada en 0.2.0).
- Ninguna respuesta incluye la API key, el secret ni la firma.

Variables de entorno:
  BINANCE_API_KEY        -> API key (solo para consultas privadas)
  BINANCE_API_SECRET     -> secret key HMAC (solo para consultas privadas)
  BINANCE_TESTNET        -> true|false (default: true)
  BINANCE_ALLOW_TRADING  -> true|false (default: false)
  BINANCE_RECV_WINDOW    -> ventana de validez de la firma en ms, 1..60000 (default: 5000)

El servidor NO lee archivos .env: las variables las pasa el cliente MCP (bloque "env").
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import logging
import os
import re
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import StrictFloat, StrictInt, StrictStr

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #
# Datos publicos de mainnet: host oficial de solo datos de mercado (no usa API key).
MAINNET_PUBLIC_URL = "https://data-api.binance.vision"
MAINNET_PRIVATE_URL = "https://api.binance.com"
TESTNET_URL = "https://testnet.binance.vision"
HTTP_TIMEOUT_SECONDS = 15.0

SYMBOL_RE = re.compile(r"^[A-Z0-9\-_.]{1,20}$")
CLIENT_ORDER_ID_RE = re.compile(r"^[.A-Z:/a-z0-9_\-]{1,36}$")
PLAIN_DECIMAL_RE = re.compile(r"^\d+(\.\d+)?$")
KLINE_INTERVALS = {
    "1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h",
    "6h", "8h", "12h", "1d", "3d", "1w", "1M",
}
ORDER_SIDES = {"BUY", "SELL"}
ORDER_TYPES = {"MARKET", "LIMIT"}
TIME_IN_FORCE = {"GTC", "IOC", "FOK"}
LEGACY_ENV = ("ENABLE_WITHDRAWALS",)

VALIDATION_NOTE = (
    "Una validacion local aprobada no garantiza que Binance acepte la orden: "
    "las reglas y el precio de referencia pueden cambiar antes de ejecutarse."
)
TESTNET_WARNING = (
    "Red de pruebas (testnet): precios, saldos y ordenes NO corresponden al mercado real."
)
UNKNOWN_OUTCOME_MESSAGE = (
    "La solicitud se envio pero su resultado es INCIERTO: no se puede afirmar que fallo "
    "ni que se ejecuto. Consulta get_order / get_open_orders antes de volver a intentarlo. "
    "No se reintento automaticamente."
)

_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no"}


# --------------------------------------------------------------------------- #
# Errores
# --------------------------------------------------------------------------- #
class ConfigError(Exception):
    """Configuracion invalida: ninguna herramienta se ejecuta."""


class ToolError(Exception):
    """Error controlado que se devuelve al cliente con un codigo estable."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------- #
# Configuracion (se captura UNA vez por operacion)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Config:
    testnet: bool
    allow_trading: bool
    recv_window: int
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)

    @property
    def network(self) -> str:
        return "testnet" if self.testnet else "mainnet"

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def base_url(self, signed: bool) -> str:
        if self.testnet:
            return TESTNET_URL
        return MAINNET_PRIVATE_URL if signed else MAINNET_PUBLIC_URL


def _parse_bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} debe ser 'true' o 'false'.")


def _load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    raw_window = env.get("BINANCE_RECV_WINDOW", "").strip() or "5000"
    try:
        recv_window = int(raw_window)
    except ValueError:
        raise ConfigError("BINANCE_RECV_WINDOW debe ser un entero en milisegundos.") from None
    if not 1 <= recv_window <= 60000:
        raise ConfigError("BINANCE_RECV_WINDOW debe estar entre 1 y 60000 ms.")
    return Config(
        testnet=_parse_bool(env, "BINANCE_TESTNET", True),
        allow_trading=_parse_bool(env, "BINANCE_ALLOW_TRADING", False),
        recv_window=recv_window,
        api_key=env.get("BINANCE_API_KEY", "").strip(),
        api_secret=env.get("BINANCE_API_SECRET", "").strip(),
    )


# --------------------------------------------------------------------------- #
# Saneamiento de secretos (respuestas, logs y stderr)
# --------------------------------------------------------------------------- #
_SIGNATURE_RE = re.compile(r"(signature=)[^&\s'\"]+", re.IGNORECASE)
_APIKEY_HEADER_RE = re.compile(r"(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+", re.IGNORECASE)


def _redact(text: str) -> str:
    text = _SIGNATURE_RE.sub(r"\1<redacted>", text)
    text = _APIKEY_HEADER_RE.sub(r"\1<redacted>", text)
    for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
        secret = os.environ.get(name, "").strip()
        if len(secret) >= 4:
            text = text.replace(secret, "<redacted>")
    return text


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        record.msg = _redact(message)
        record.args = None
        return True


def _install_redaction() -> None:
    # httpx registra URLs completas (con la firma) en INFO: se silencia.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    flt = _RedactingFilter()
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _RedactingFilter) for f in handler.filters):
            handler.addFilter(flt)

    def _excepthook(exc_type, exc, tb):  # pragma: no cover - solo en caidas del proceso
        import traceback

        sys.stderr.write(_redact("".join(traceback.format_exception(exc_type, exc, tb))))

    sys.excepthook = _excepthook


logger = logging.getLogger("binance_mcp")


# --------------------------------------------------------------------------- #
# Validaciones de parametros
# --------------------------------------------------------------------------- #
def _norm_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise ToolError("VALIDATION_ERROR", "symbol debe ser texto, por ejemplo 'BTCUSDT'.")
    value = symbol.strip().upper()
    if not SYMBOL_RE.match(value):
        raise ToolError("VALIDATION_ERROR", "symbol invalido: usa el formato de Binance, por ejemplo 'BTCUSDT'.")
    return value


def _choice(name: str, value: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value.strip().upper() not in allowed:
        raise ToolError("VALIDATION_ERROR", f"{name} debe ser uno de: {', '.join(sorted(allowed))}.")
    return value.strip().upper()


def _int_range(name: str, value: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ToolError("VALIDATION_ERROR", f"{name} debe ser un entero entre {low} y {high}.")
    return value


def _to_decimal(name: str, value: str | int | float) -> Decimal:
    """Convierte sin perder precision. Preferido: string decimal ('0.001').

    Los float se aceptan por compatibilidad via repr() (nunca Decimal(float)), pero un
    float JSON puede llegar ya redondeado por el cliente: para cantidades exactas usar texto.
    """
    if isinstance(value, bool):
        raise ToolError("VALIDATION_ERROR", f"{name} debe ser un numero decimal, no booleano.")
    if isinstance(value, str):
        text = value.strip()
        if not PLAIN_DECIMAL_RE.match(text):
            raise ToolError(
                "VALIDATION_ERROR",
                f"{name} debe ser un decimal positivo en texto plano, por ejemplo '0.001' "
                "(sin signo, exponente ni separador de miles).",
            )
        number = Decimal(text)
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        try:
            number = Decimal(repr(value))
        except InvalidOperation:
            raise ToolError("VALIDATION_ERROR", f"{name} no es un numero valido.") from None
    else:
        raise ToolError("VALIDATION_ERROR", f"{name} debe ser un decimal en texto, por ejemplo '0.001'.")
    if not number.is_finite() or number <= 0:
        raise ToolError("VALIDATION_ERROR", f"{name} debe ser un numero finito mayor que cero.")
    return number


def _fmt(number: Decimal) -> str:
    return format(number, "f")


def _require_trading(cfg: Config) -> None:
    if not cfg.allow_trading:
        raise ToolError(
            "TRADING_DISABLED",
            "Operaciones que modifican la cuenta bloqueadas: BINANCE_ALLOW_TRADING=false "
            f"(red activa: {cfg.network}). Solo el usuario puede habilitarlo en la configuracion "
            "del servidor; cambiar de red no lo habilita.",
        )


def _require_credentials(cfg: Config) -> None:
    if not cfg.has_credentials:
        raise ToolError(
            "MISSING_CREDENTIALS",
            "Esta consulta es privada y faltan BINANCE_API_KEY y/o BINANCE_API_SECRET en el "
            "entorno del servidor MCP. Configuralas en el bloque 'env' del cliente; no las "
            "compartas por chat.",
        )


# --------------------------------------------------------------------------- #
# Transporte HTTP
# --------------------------------------------------------------------------- #
def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)


def _sign(secret: str, payload: str) -> str:
    """Firma HMAC SHA256 sobre el query string exacto que se envia."""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _unknown(context: dict[str, Any], reason: str) -> ToolError:
    return ToolError("OUTCOME_UNKNOWN", UNKNOWN_OUTCOME_MESSAGE, outcome="unknown", reason=reason, **context)


async def _send(
    cfg: Config,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    signed: bool = False,
    mutating: bool = False,
    context: dict[str, Any] | None = None,
) -> Any:
    """Unico punto de salida hacia Binance. Nunca reintenta."""
    method = method.upper()
    context = dict(context or {})
    # Bloqueo centralizado: cualquier metodo que no sea GET modifica la cuenta.
    if method != "GET" or mutating:
        _require_trading(cfg)
    if signed:
        _require_credentials(cfg)

    query_params = dict(params or {})
    headers: dict[str, str] = {}
    if signed:
        query_params["timestamp"] = int(time.time() * 1000)
        query_params["recvWindow"] = cfg.recv_window
        query = urllib.parse.urlencode(query_params)
        query += "&signature=" + _sign(cfg.api_secret, query)
        headers["X-MBX-APIKEY"] = cfg.api_key
    else:
        query = urllib.parse.urlencode(query_params)
    url = cfg.base_url(signed) + path + (f"?{query}" if query else "")

    try:
        async with _make_client() as client:
            response = await client.request(method, url, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # La conexion no llego a establecerse: la solicitud no se envio.
        raise ToolError(
            "NETWORK_ERROR",
            f"No se pudo conectar con Binance ({type(exc).__name__}). La solicitud no se envio.",
            sent=False,
            **context,
        ) from None
    except httpx.TimeoutException as exc:
        if mutating:
            raise _unknown(context, f"timeout ({type(exc).__name__})") from None
        raise ToolError("TIMEOUT", f"Binance no respondio a tiempo ({type(exc).__name__}).") from None
    except httpx.HTTPError as exc:
        if mutating:
            raise _unknown(context, f"error de red tras enviar ({type(exc).__name__})") from None
        raise ToolError("NETWORK_ERROR", f"Error de red ({type(exc).__name__}).") from None

    status = response.status_code
    if status in (418, 429):
        retry_after = response.headers.get("Retry-After")
        raise ToolError(
            "IP_BANNED" if status == 418 else "RATE_LIMITED",
            "Binance bloqueo la IP por exceso de solicitudes."
            if status == 418
            else "Limite de solicitudes de Binance alcanzado. Espera antes de reintentar.",
            http_status=status,
            retry_after_seconds=int(retry_after) if retry_after and retry_after.isdigit() else None,
            **({"outcome": "rejected"} if mutating else {}),
            **context,
        )

    try:
        data = response.json()
    except ValueError:
        if mutating:
            raise _unknown(context, f"respuesta ilegible (HTTP {status})") from None
        raise ToolError("INVALID_RESPONSE", f"Binance devolvio una respuesta no JSON (HTTP {status}).", http_status=status) from None

    if status >= 500:
        if mutating:
            raise _unknown(context, f"error del servidor de Binance (HTTP {status})")
        raise ToolError("BINANCE_SERVER_ERROR", f"Error interno de Binance (HTTP {status}).", http_status=status)

    if status >= 400:
        code = data.get("code") if isinstance(data, dict) else None
        msg = data.get("msg") if isinstance(data, dict) else None
        if mutating and code == -1007:
            # Timeout interno de Binance: el estado de ejecucion es desconocido.
            raise _unknown(context, "Binance reporto timeout interno (-1007)")
        extra: dict[str, Any] = {"http_status": status, "binance_code": code}
        if code == -1021:
            extra["hint"] = "Reloj desincronizado o recvWindow corto: ejecuta diagnose_connection."
        if mutating:
            extra["outcome"] = "rejected"
        raise ToolError("BINANCE_ERROR", _redact(str(msg or f"HTTP {status}")), **extra, **context)

    return data


# --------------------------------------------------------------------------- #
# Validacion de ordenes contra exchangeInfo
# --------------------------------------------------------------------------- #
def _dec(f: dict[str, Any], key: str) -> Decimal:
    try:
        return Decimal(str(f[key]))
    except (KeyError, InvalidOperation):
        raise ToolError("VALIDATION_UNRESOLVED", f"El filtro {f.get('filterType')} no trae {key} valido; la orden se bloquea.") from None


def _check_price_filter(f: dict[str, Any], price: Decimal) -> None:
    # Documentado: cualquier campo en 0 deshabilita esa regla del PRICE_FILTER.
    min_p, max_p, tick = _dec(f, "minPrice"), _dec(f, "maxPrice"), _dec(f, "tickSize")
    if min_p != 0 and price < min_p:
        raise ToolError("VALIDATION_ERROR", f"price {_fmt(price)} es menor que minPrice {_fmt(min_p)}.")
    if max_p != 0 and price > max_p:
        raise ToolError("VALIDATION_ERROR", f"price {_fmt(price)} es mayor que maxPrice {_fmt(max_p)}.")
    if tick != 0 and price % tick != 0:
        raise ToolError("VALIDATION_ERROR", f"price {_fmt(price)} no es multiplo de tickSize {_fmt(tick)}. No se redondea: envia un precio valido.")


def _check_lot(f: dict[str, Any], qty: Decimal) -> None:
    name = f.get("filterType")
    min_q, max_q, step = _dec(f, "minQty"), _dec(f, "maxQty"), _dec(f, "stepSize")
    # Binance no documenta que 0 deshabilite reglas de LOT_SIZE/MARKET_LOT_SIZE: no se asume.
    if step == 0 or max_q == 0:
        raise ToolError(
            "VALIDATION_UNRESOLVED",
            f"{name} trae stepSize o maxQty en 0 y Binance no documenta ese caso. "
            "La orden se bloquea para no suponer su significado.",
        )
    if qty < min_q:
        raise ToolError("VALIDATION_ERROR", f"quantity {_fmt(qty)} es menor que {name}.minQty {_fmt(min_q)}.")
    if qty > max_q:
        raise ToolError("VALIDATION_ERROR", f"quantity {_fmt(qty)} es mayor que {name}.maxQty {_fmt(max_q)}.")
    if qty % step != 0:
        raise ToolError("VALIDATION_ERROR", f"quantity {_fmt(qty)} no es multiplo de {name}.stepSize {_fmt(step)}. No se redondea: envia una cantidad valida.")


@dataclass
class MarketReference:
    """Precio de referencia de una orden MARKET para filtros de notional."""

    price: Decimal | None
    source: str
    verified: bool
    reason: str = ""


def _notional_bounds(f: dict[str, Any], is_market: bool) -> tuple[Decimal | None, Decimal | None]:
    """Devuelve (minimo, maximo) aplicables segun el tipo de filtro y de orden."""
    if f["filterType"] == "NOTIONAL":
        apply_min = (not is_market) or bool(f.get("applyMinToMarket"))
        apply_max = (not is_market) or bool(f.get("applyMaxToMarket"))
        lo = _dec(f, "minNotional") if apply_min else None
        hi = _dec(f, "maxNotional") if apply_max else None
        if hi is not None and hi == 0:
            raise ToolError("VALIDATION_UNRESOLVED", "NOTIONAL.maxNotional es 0 y Binance no documenta ese caso; la orden se bloquea.")
        return lo, hi
    apply_min = (not is_market) or bool(f.get("applyToMarket"))
    return (_dec(f, "minNotional") if apply_min else None), None


def _validate_order(
    symbol_info: dict[str, Any],
    order_type: str,
    quantity: Decimal,
    price: Decimal | None,
    references: dict[str, MarketReference] | None = None,
) -> dict[str, Any]:
    """Valida contra las reglas del simbolo. Lanza ToolError si algo falla. Nunca ajusta valores.

    references: para MARKET, precio de referencia por filterType de notional.
    """
    references = references or {}
    checked: list[str] = []
    not_checked: list[dict[str, str]] = []

    if symbol_info.get("status") != "TRADING":
        raise ToolError("VALIDATION_ERROR", f"El simbolo no esta en estado TRADING (estado: {symbol_info.get('status')}).")
    checked.append("status")
    if symbol_info.get("isSpotTradingAllowed") is False:
        raise ToolError("VALIDATION_ERROR", "El simbolo no permite trading spot.")
    order_types = symbol_info.get("orderTypes")
    if isinstance(order_types, list):
        if order_type not in order_types:
            raise ToolError("VALIDATION_ERROR", f"El simbolo no admite ordenes {order_type}.")
        checked.append("orderTypes")

    filters = {f.get("filterType"): f for f in symbol_info.get("filters", []) if isinstance(f, dict)}
    is_market = order_type == "MARKET"

    if not is_market and "PRICE_FILTER" in filters:
        _check_price_filter(filters["PRICE_FILTER"], price)  # type: ignore[arg-type]
        checked.append("PRICE_FILTER")
    if "LOT_SIZE" in filters:
        _check_lot(filters["LOT_SIZE"], quantity)
        checked.append("LOT_SIZE")
    if is_market and "MARKET_LOT_SIZE" in filters:
        _check_lot(filters["MARKET_LOT_SIZE"], quantity)
        checked.append("MARKET_LOT_SIZE")

    for name in ("NOTIONAL", "MIN_NOTIONAL"):
        if name not in filters:
            continue
        lo, hi = _notional_bounds(filters[name], is_market)
        if lo is None and hi is None:
            checked.append(f"{name} (no aplica a MARKET)")
            continue
        if is_market:
            ref = references.get(name)
            if ref is None or not ref.verified or ref.price is None:
                not_checked.append({
                    "filter": name,
                    "reason": (ref.reason if ref else "sin precio de referencia")
                    + ". No se pudo comprobar el precio que Binance usara para el notional.",
                })
                continue
            notional = quantity * ref.price
            basis = f"precio de referencia {_fmt(ref.price)} ({ref.source})"
        else:
            notional = quantity * price  # type: ignore[operator]
            basis = "price x quantity"
        if lo is not None and notional < lo:
            raise ToolError("VALIDATION_ERROR", f"Valor de la orden {_fmt(notional)} ({basis}) menor que {name}.minNotional {_fmt(lo)}.")
        if hi is not None and notional > hi:
            raise ToolError("VALIDATION_ERROR", f"Valor de la orden {_fmt(notional)} ({basis}) mayor que {name}.maxNotional {_fmt(hi)}.")
        checked.append(name)

    handled = {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "NOTIONAL", "MIN_NOTIONAL"}
    for name in filters:
        if name in handled or (name == "PRICE_FILTER" and is_market):
            continue
        not_checked.append({"filter": str(name), "reason": "filtro no validado localmente"})
    if is_market and "PRICE_FILTER" in filters:
        not_checked.append({"filter": "PRICE_FILTER", "reason": "una orden MARKET no envia price"})

    incomplete = any(item["filter"] in ("NOTIONAL", "MIN_NOTIONAL") for item in not_checked)
    return {
        "status": "incomplete" if incomplete else "complete",
        "checked": checked,
        "not_checked": not_checked,
        "note": VALIDATION_NOTE,
    }


async def _resolve_market_reference(cfg: Config, symbol: str, avg_price_mins: Any) -> MarketReference:
    """Busca el precio que Binance documenta para el notional de MARKET, en su orden:
    1) referencePrice no nulo; 2) promedio de avgPriceMins minutos; 3) si avgPriceMins == 0, ultimo precio.
    Si algo no puede comprobarse, devuelve verified=False (validacion incompleta).
    """
    try:
        ref = await _send(cfg, "GET", "/api/v3/referencePrice", {"symbol": symbol})
        if isinstance(ref, dict) and ref.get("referencePrice") is not None:
            return MarketReference(Decimal(str(ref["referencePrice"])), "referencePrice", True)
    except ToolError as exc:
        if exc.extra.get("binance_code") != -2043:  # -2043 = el simbolo no tiene precio de referencia
            return MarketReference(None, "referencePrice", False, f"no se pudo consultar referencePrice ({exc.code})")
    except InvalidOperation:
        return MarketReference(None, "referencePrice", False, "referencePrice ilegible")

    try:
        mins = int(avg_price_mins)
    except (TypeError, ValueError):
        return MarketReference(None, "avgPrice", False, "el filtro no trae avgPriceMins valido")
    try:
        if mins == 0:
            last = await _send(cfg, "GET", "/api/v3/ticker/price", {"symbol": symbol})
            return MarketReference(Decimal(str(last["price"])), "lastPrice", True)
        avg = await _send(cfg, "GET", "/api/v3/avgPrice", {"symbol": symbol})
        if int(avg.get("mins", -1)) != mins:
            return MarketReference(
                None, "avgPrice", False,
                f"avgPrice usa {avg.get('mins')} min y el filtro exige {mins} min",
            )
        return MarketReference(Decimal(str(avg["price"])), f"avgPrice {mins}m", True)
    except ToolError as exc:
        return MarketReference(None, "avgPrice", False, f"no se pudo consultar el precio ({exc.code})")
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        return MarketReference(None, "avgPrice", False, "respuesta de precio ilegible")


async def _symbol_info(cfg: Config, symbol: str) -> dict[str, Any]:
    info = await _send(cfg, "GET", "/api/v3/exchangeInfo", {"symbol": symbol})
    symbols = info.get("symbols") if isinstance(info, dict) else None
    if not symbols or not isinstance(symbols[0], dict) or symbols[0].get("symbol") != symbol:
        raise ToolError("INVALID_RESPONSE", "exchangeInfo no devolvio las reglas del simbolo; no se envia la orden.")
    return symbols[0]


# --------------------------------------------------------------------------- #
# Ejecucion comun de herramientas: sobre de respuesta y errores consistentes
# --------------------------------------------------------------------------- #
def _envelope(cfg: Config | None, ok: bool, payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "network": cfg.network if cfg else None}
    if cfg is not None:
        out["trading_enabled"] = cfg.allow_trading
        if cfg.testnet:
            out["warning"] = TESTNET_WARNING
    out.update(payload)
    return out


async def _run(impl: Callable[..., Awaitable[Any]], *args: Any) -> dict[str, Any]:
    try:
        cfg = _load_config()
    except ConfigError as exc:
        return _envelope(None, False, {"error": {"code": "CONFIG_ERROR", "message": str(exc)}})
    try:
        data = await impl(cfg, *args)
        return _envelope(cfg, True, {"data": data})
    except ToolError as exc:
        error = {"code": exc.code, "message": _redact(exc.message)}
        error.update({k: v for k, v in exc.extra.items() if v is not None})
        return _envelope(cfg, False, {"error": error})
    except Exception as exc:  # noqa: BLE001 - nunca devolver excepciones crudas (pueden traer URLs firmadas)
        logger.error("Error interno en %s: %s", getattr(impl, "__name__", "tool"), type(exc).__name__)
        return _envelope(cfg, False, {"error": {"code": "INTERNAL_ERROR", "message": f"Error interno ({type(exc).__name__})."}})


# --------------------------------------------------------------------------- #
# Implementaciones (reciben la config capturada al inicio de la operacion)
# --------------------------------------------------------------------------- #
async def _get_price(cfg: Config, symbol: str) -> Any:
    return await _send(cfg, "GET", "/api/v3/ticker/price", {"symbol": _norm_symbol(symbol)})


async def _get_24h_stats(cfg: Config, symbol: str) -> Any:
    return await _send(cfg, "GET", "/api/v3/ticker/24hr", {"symbol": _norm_symbol(symbol)})


async def _get_klines(cfg: Config, symbol: str, interval: str, limit: int) -> Any:
    if interval not in KLINE_INTERVALS:
        raise ToolError("VALIDATION_ERROR", f"interval invalido. Valores admitidos: {', '.join(sorted(KLINE_INTERVALS))}.")
    params = {"symbol": _norm_symbol(symbol), "interval": interval, "limit": _int_range("limit", limit, 1, 1000)}
    return await _send(cfg, "GET", "/api/v3/klines", params)


async def _get_order_book(cfg: Config, symbol: str, limit: int) -> Any:
    params = {"symbol": _norm_symbol(symbol), "limit": _int_range("limit", limit, 1, 5000)}
    return await _send(cfg, "GET", "/api/v3/depth", params)


async def _get_account(cfg: Config) -> Any:
    return await _send(cfg, "GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)


async def _get_open_orders(cfg: Config, symbol: str | None) -> Any:
    params = {"symbol": _norm_symbol(symbol)} if symbol else {}
    return await _send(cfg, "GET", "/api/v3/openOrders", params, signed=True)


async def _get_my_trades(cfg: Config, symbol: str, limit: int) -> Any:
    params = {"symbol": _norm_symbol(symbol), "limit": _int_range("limit", limit, 1, 1000)}
    trades = await _send(cfg, "GET", "/api/v3/myTrades", params, signed=True)
    return {
        "trades": trades,
        "note": f"Historial parcial: como maximo {params['limit']} operaciones recientes. "
        "No sirve para calcular rentabilidad total.",
    }


async def _get_order(cfg: Config, symbol: str, order_id: int | None, client_order_id: str | None) -> Any:
    sym = _norm_symbol(symbol)
    if (order_id is None) == (client_order_id is None):
        raise ToolError("VALIDATION_ERROR", "Indica exactamente uno: order_id o client_order_id.")
    params: dict[str, Any] = {"symbol": sym}
    if order_id is not None:
        params["orderId"] = _int_range("order_id", order_id, 1, 2**63 - 1)
    else:
        if not isinstance(client_order_id, str) or not CLIENT_ORDER_ID_RE.match(client_order_id):
            raise ToolError("VALIDATION_ERROR", "client_order_id invalido (1-36 caracteres: letras, numeros, . : / _ -).")
        params["origClientOrderId"] = client_order_id
    return await _send(cfg, "GET", "/api/v3/order", params, signed=True)


async def _place_order(
    cfg: Config,
    symbol: str,
    side: str,
    order_type: str,
    quantity: str | int | float | None,
    price: str | int | float | None,
    time_in_force: str | None,
) -> Any:
    _require_trading(cfg)  # antes de cualquier validacion o llamada de red
    _require_credentials(cfg)
    sym = _norm_symbol(symbol)
    side_v = _choice("side", side, ORDER_SIDES)
    type_v = _choice("order_type", order_type, ORDER_TYPES)
    if quantity is None:
        raise ToolError("VALIDATION_ERROR", "quantity es obligatoria (cantidad del activo base).")
    qty = _to_decimal("quantity", quantity)

    params: dict[str, Any] = {"symbol": sym, "side": side_v, "type": type_v}
    price_v: Decimal | None = None
    if type_v == "MARKET":
        if price is not None:
            raise ToolError("VALIDATION_ERROR", "Las ordenes MARKET no llevan price.")
        if time_in_force is not None:
            raise ToolError("VALIDATION_ERROR", "Las ordenes MARKET no llevan time_in_force.")
    else:
        if price is None:
            raise ToolError("VALIDATION_ERROR", "Las ordenes LIMIT requieren price.")
        price_v = _to_decimal("price", price)
        tif = "GTC" if time_in_force is None else time_in_force
        params["timeInForce"] = _choice("time_in_force", tif, TIME_IN_FORCE)
        params["price"] = _fmt(price_v)
    params["quantity"] = _fmt(qty)

    info = await _symbol_info(cfg, sym)
    references: dict[str, MarketReference] = {}
    if type_v == "MARKET":
        for f in info.get("filters", []):
            if not isinstance(f, dict) or f.get("filterType") not in ("NOTIONAL", "MIN_NOTIONAL"):
                continue
            lo, hi = _notional_bounds(f, True)
            if lo is not None or hi is not None:
                references[f["filterType"]] = await _resolve_market_reference(cfg, sym, f.get("avgPriceMins"))
    validation = _validate_order(info, type_v, qty, price_v, references)

    # Se genera ANTES de enviar: permite consultar la orden si el resultado queda incierto.
    client_order_id = "mcp-" + uuid.uuid4().hex[:28]
    params["newClientOrderId"] = client_order_id
    params["newOrderRespType"] = "RESULT"
    order = await _send(
        cfg, "POST", "/api/v3/order", params, signed=True, mutating=True,
        context={"symbol": sym, "client_order_id": client_order_id},
    )
    return {"order": order, "client_order_id": client_order_id, "validation": validation}


async def _cancel_order(cfg: Config, symbol: str, order_id: int) -> Any:
    _require_trading(cfg)
    _require_credentials(cfg)
    sym = _norm_symbol(symbol)
    oid = _int_range("order_id", order_id, 1, 2**63 - 1)
    return await _send(
        cfg, "DELETE", "/api/v3/order", {"symbol": sym, "orderId": oid}, signed=True, mutating=True,
        context={"symbol": sym, "order_id": oid},
    )


async def _cancel_all_orders(cfg: Config, symbol: str) -> Any:
    _require_trading(cfg)
    _require_credentials(cfg)
    sym = _norm_symbol(symbol)
    return await _send(
        cfg, "DELETE", "/api/v3/openOrders", {"symbol": sym}, signed=True, mutating=True,
        context={"symbol": sym},
    )


async def _diagnose_connection(cfg: Config) -> Any:
    report: dict[str, Any] = {
        "network": cfg.network,
        "public_base_url": cfg.base_url(signed=False),
        "private_base_url": cfg.base_url(signed=True),
        "trading_enabled": cfg.allow_trading,
        "credentials_configured": cfg.has_credentials,
        "recv_window_ms": cfg.recv_window,
        "legacy_env_ignored": [name for name in LEGACY_ENV if os.environ.get(name)],
    }
    started = time.time() * 1000
    try:
        server = await _send(cfg, "GET", "/api/v3/time")
        finished = time.time() * 1000
        offset = int(server["serverTime"] - (started + finished) / 2)
        round_trip = int(finished - started)
        # Binance rechaza si timestamp > serverTime + 1000 o si serverTime - timestamp > recvWindow.
        if -offset > 1000:
            clock = "reloj local adelantado mas de 1000 ms: las consultas firmadas fallaran (-1021)"
        elif offset + round_trip > cfg.recv_window:
            clock = "reloj local atrasado respecto a recvWindow: las consultas firmadas pueden fallar (-1021)"
        else:
            clock = "ok"
        report["public_api"] = {
            "status": "ok",
            "round_trip_ms": round_trip,
            "server_time_ms": server["serverTime"],
            "clock_offset_ms": offset,
            "clock_status": clock,
        }
    except ToolError as exc:
        report["public_api"] = {"status": "error", "code": exc.code, "message": _redact(exc.message)}
    except (KeyError, TypeError, ValueError):
        report["public_api"] = {"status": "error", "code": "INVALID_RESPONSE", "message": "Respuesta de /api/v3/time ilegible."}

    if not cfg.has_credentials:
        report["private_api"] = {"status": "not_configured", "message": "Sin credenciales: solo consultas publicas."}
    else:
        try:
            await _send(cfg, "GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
            report["private_api"] = {"status": "ok", "message": "Autenticacion aceptada (consulta de solo lectura)."}
        except ToolError as exc:
            report["private_api"] = {
                "status": "error",
                "code": exc.code,
                "binance_code": exc.extra.get("binance_code"),
                "message": _redact(exc.message),
            }
    return report


# --------------------------------------------------------------------------- #
# Herramientas MCP
# --------------------------------------------------------------------------- #
mcp = FastMCP("binance")
_install_redaction()


@mcp.tool()
async def get_price(symbol: str) -> dict:
    """Precio actual de un simbolo. Ej: symbol='BTCUSDT'. Publica, sin credenciales.
    Revisa 'network' en la respuesta: en testnet el precio NO es del mercado real."""
    return await _run(_get_price, symbol)


@mcp.tool()
async def get_24h_stats(symbol: str) -> dict:
    """Estadisticas de 24h de un simbolo (volumen, cambio %, max/min). Ej: 'ETHUSDT'. Publica."""
    return await _run(_get_24h_stats, symbol)


@mcp.tool()
async def get_klines(symbol: str, interval: str = "1h", limit: StrictInt = 100) -> dict:
    """Velas (candlesticks). interval: 1s,1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w,1M.
    limit: 1..1000. Publica."""
    return await _run(_get_klines, symbol, interval, limit)


@mcp.tool()
async def get_order_book(symbol: str, limit: StrictInt = 20) -> dict:
    """Libro de ordenes (bids/asks). limit: 1..5000. Publica."""
    return await _run(_get_order_book, symbol, limit)


@mcp.tool()
async def get_account() -> dict:
    """Saldos (sin los que estan en cero) y permisos de la cuenta spot. Privada, solo lectura."""
    return await _run(_get_account)


@mcp.tool()
async def get_open_orders(symbol: str | None = None) -> dict:
    """Ordenes abiertas. Si pasas symbol, filtra por ese par. Privada, solo lectura."""
    return await _run(_get_open_orders, symbol)


@mcp.tool()
async def get_my_trades(symbol: str, limit: StrictInt = 50) -> dict:
    """Ultimos trades propios de un simbolo (limit 1..1000). Historial parcial: no sirve
    para calcular rentabilidad total. Privada, solo lectura."""
    return await _run(_get_my_trades, symbol, limit)


@mcp.tool()
async def get_order(
    symbol: str, order_id: StrictInt | None = None, client_order_id: StrictStr | None = None
) -> dict:
    """Consulta una orden por simbolo y exactamente UNO de: order_id o client_order_id.
    Util para resolver un resultado incierto de place_order. Privada, solo lectura."""
    return await _run(_get_order, symbol, order_id, client_order_id)


@mcp.tool()
async def diagnose_connection() -> dict:
    """Diagnostico: red activa, si el trading esta habilitado, si hay credenciales (nunca sus
    valores), conectividad, diferencia de hora con Binance y autenticacion de solo lectura.
    No coloca ordenes."""
    return await _run(_diagnose_connection)


@mcp.tool()
async def place_order(
    symbol: str,
    side: str,
    order_type: str = "MARKET",
    quantity: StrictStr | StrictInt | StrictFloat | None = None,
    price: StrictStr | StrictInt | StrictFloat | None = None,
    time_in_force: StrictStr | None = None,
) -> dict:
    """
    Crea una orden. BLOQUEADA salvo BINANCE_ALLOW_TRADING=true.
      symbol: ej 'BTCUSDT'; side: 'BUY' o 'SELL'; order_type: 'MARKET' o 'LIMIT'
      quantity: cantidad del activo base, preferentemente como texto ('0.001')
      price: obligatorio solo en LIMIT, preferentemente como texto
      time_in_force: solo LIMIT (GTC por defecto, IOC, FOK); omitir en MARKET
    Valida contra exchangeInfo sin redondear. Si el resultado es incierto (timeout), NO
    reintenta: devuelve OUTCOME_UNKNOWN y el client_order_id para consultarla con get_order.
    """
    return await _run(_place_order, symbol, side, order_type, quantity, price, time_in_force)


@mcp.tool()
async def cancel_order(symbol: str, order_id: StrictInt) -> dict:
    """Cancela una orden por orderId. BLOQUEADA salvo BINANCE_ALLOW_TRADING=true."""
    return await _run(_cancel_order, symbol, order_id)


@mcp.tool()
async def cancel_all_orders(symbol: str) -> dict:
    """Cancela TODAS las ordenes abiertas de un simbolo. BLOQUEADA salvo BINANCE_ALLOW_TRADING=true."""
    return await _run(_cancel_all_orders, symbol)


if __name__ == "__main__":
    _install_redaction()
    mcp.run()
