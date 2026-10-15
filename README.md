# Binance MCP Server

> Servidor [MCP (Model Context Protocol)](https://modelcontextprotocol.io) para consultar
> **Binance Spot** desde un agente de IA: precios, saldos y órdenes. **Solo lectura por
> defecto** y **testnet por defecto**.
>
> *English: MCP server for Binance Spot. Read-only and testnet by default; trading must be
> enabled explicitly with `BINANCE_ALLOW_TRADING=true`. Withdrawals were removed in 0.2.0.*

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![MCP](https://img.shields.io/badge/MCP-1.2%2B-purple)
![License](https://img.shields.io/badge/license-MIT-green)

## Seguridad por defecto

| Configuración | Default | Efecto |
|---|---|---|
| `BINANCE_TESTNET` | `true` | Usa `testnet.binance.vision`. Con `false`, Binance real. |
| `BINANCE_ALLOW_TRADING` | `false` | Bloquea **toda** operación que modifique la cuenta (crear y cancelar órdenes). |
| Retiros | — | **No existen** en esta versión. |

- **Red y permiso son independientes.** Pasar a mainnet (`BINANCE_TESTNET=false`) **no**
  habilita operar. Para operar hay que poner además `BINANCE_ALLOW_TRADING=true`.
- El bloqueo está centralizado: además de cada herramienta, el único punto que habla con
  Binance rechaza cualquier método distinto de `GET` si el permiso está desactivado.
- Valores inválidos (`BINANCE_TESTNET=quizas`) devuelven `CONFIG_ERROR`: nunca se cae en
  mainnet por accidente.
- Las consultas públicas no usan ni envían credenciales. Las respuestas, logs y errores
  nunca incluyen la API key, el secret ni la firma.
- Recomendado: API key **sin permiso de retiro**, con **IP whitelist**, y solo lectura si
  no vas a operar.

## Herramientas

| Herramienta | Tipo | Descripción |
|---|---|---|
| `get_price` | pública | Precio actual de un símbolo (`BTCUSDT`) |
| `get_24h_stats` | pública | Estadísticas de 24 h |
| `get_klines` | pública | Velas; `interval` oficial (`1s`…`1M`), `limit` 1..1000 |
| `get_order_book` | pública | Libro de órdenes; `limit` 1..5000 |
| `diagnose_connection` | diagnóstico | Red, permiso de operar, credenciales presentes (sí/no), conectividad, diferencia de hora, autenticación de solo lectura |
| `get_account` | privada | Saldos distintos de cero y permisos de la cuenta |
| `get_open_orders` | privada | Órdenes abiertas (opcionalmente por símbolo) |
| `get_my_trades` | privada | Últimos trades de un símbolo (historial **parcial**) |
| `get_order` | privada | Una orden por símbolo y **exactamente uno** de `order_id` / `client_order_id` |
| `place_order` | operación | `MARKET` o `LIMIT`. Bloqueada salvo `BINANCE_ALLOW_TRADING=true` |
| `cancel_order` | operación | Cancela por `order_id`. Bloqueada salvo permiso |
| `cancel_all_orders` | operación | Cancela todas las de un símbolo. Bloqueada salvo permiso |

### Formato de respuesta

Todas las herramientas devuelven el mismo sobre:

```json
{ "ok": true, "network": "mainnet", "trading_enabled": false, "data": { "symbol": "BTCUSDT", "price": "82026.83000000" } }
{ "ok": false, "network": "testnet", "trading_enabled": false,
  "warning": "Red de pruebas (testnet): precios, saldos y ordenes NO corresponden al mercado real.",
  "error": { "code": "MISSING_CREDENTIALS", "message": "..." } }
```

Códigos de error: `CONFIG_ERROR`, `VALIDATION_ERROR`, `VALIDATION_UNRESOLVED`,
`TRADING_DISABLED`, `MISSING_CREDENTIALS`, `NETWORK_ERROR` (con `sent: false` si no se
llegó a conectar), `TIMEOUT`, `RATE_LIMITED` / `IP_BANNED` (con `retry_after_seconds`),
`INVALID_RESPONSE`, `BINANCE_SERVER_ERROR`, `BINANCE_ERROR` (con `binance_code`),
`OUTCOME_UNKNOWN`, `INTERNAL_ERROR`.

### Órdenes: validación y resultados inciertos

Antes de enviar una orden se consultan las reglas del símbolo (`/api/v3/exchangeInfo`):
estado `TRADING`, tipos admitidos, `PRICE_FILTER`, `LOT_SIZE`, `MARKET_LOT_SIZE`,
`NOTIONAL` (`applyMinToMarket` / `applyMaxToMarket`) y `MIN_NOTIONAL` (`applyToMarket`).

- **Nunca se redondea**: un precio o cantidad fuera de `tickSize`/`stepSize` se rechaza.
- Pasá `quantity` y `price` como **texto** (`"0.001"`). Se aceptan números por
  compatibilidad (se convierten vía su representación más corta, nunca `Decimal(float)`),
  pero un número JSON pudo haber sido redondeado ya por el cliente.
- `time_in_force` solo aplica a `LIMIT` (default `GTC`); en `MARKET` se omite.
- `0` desactiva una regla **solo** en `PRICE_FILTER` (así lo documenta Binance). Si
  `LOT_SIZE`/`MARKET_LOT_SIZE` traen `stepSize` o `maxQty` en `0`, o `NOTIONAL.maxNotional`
  en `0`, la orden se **bloquea** con `VALIDATION_UNRESOLVED` porque ese caso no está
  documentado.
- Para `MARKET` el notional se compara contra el precio que documenta Binance:
  `referencePrice` si existe; si no, `avgPrice` comprobando que su ventana (`mins`) sea la
  del filtro (`avgPriceMins`); si `avgPriceMins` es 0, el último precio. Si no se puede
  comprobar, la respuesta trae `validation.status = "incomplete"` y el filtro en
  `not_checked`. Otros filtros (p. ej. `PERCENT_PRICE_BY_SIDE`) también se listan ahí.
- **Una validación local aprobada no garantiza que Binance acepte la orden.**
- Cada orden lleva un `client_order_id` generado **antes** de enviarla. Si hay timeout,
  error 5xx, `-1007` o respuesta ilegible después de enviar, la respuesta es
  `OUTCOME_UNKNOWN` (`outcome: "unknown"`): **no** se afirma que falló ni que se ejecutó, y
  **no** se reintenta. Consultala con `get_order(symbol, client_order_id=...)`. En
  cancelaciones se devuelven el `symbol` y el `order_id` originales.

## Instalación

Requiere [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Lubodi-Code/binance-mcp.git
cd binance-mcp
uv sync
```

## Configuración en el cliente MCP

`server.py` **no lee `.env`**: las variables van en el bloque `env` del cliente. Ejemplo
para Claude Desktop (`claude_desktop_config.json`) o Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "binance": {
      "command": "uv",
      "args": ["--directory", "/ruta/absoluta/a/binance-mcp", "run", "server.py"],
      "env": {
        "BINANCE_TESTNET": "true",
        "BINANCE_ALLOW_TRADING": "false",
        "BINANCE_API_KEY": "tu_api_key_de_testnet",
        "BINANCE_API_SECRET": "tu_secret_de_testnet"
      }
    }
  }
}
```

- Solo precios: omití `BINANCE_API_KEY` / `BINANCE_API_SECRET`.
- Claves de testnet: https://testnet.binance.vision.
- No pegues tus claves en el chat del agente: van solo en este archivo de configuración.

### Testnet vs. producción

| | Testnet (`BINANCE_TESTNET=true`) | Producción (`false`) |
|---|---|---|
| Host público | `testnet.binance.vision` | `data-api.binance.vision` |
| Host privado | `testnet.binance.vision` | `api.binance.com` |
| Precios | **De prueba, no reales.** Cada respuesta lo advierte en `warning`. | Mercado real |
| Claves | Propias del testnet | Propias de la cuenta real |
| Saldos | Fondos ficticios del testnet | Fondos reales |

Para consultar precios reales sin arriesgar nada: `BINANCE_TESTNET=false`,
`BINANCE_ALLOW_TRADING=false` y sin claves.

## Ejemplos de consulta

- «¿A cuánto está BTCUSDT y cómo se movió en las últimas 24 h?» → `get_price`, `get_24h_stats`
- «Diagnosticá la conexión con Binance» → `diagnose_connection`
- «¿Qué saldos tengo?» → `get_account`
- «¿Tengo órdenes abiertas en ETHUSDT?» → `get_open_orders(symbol="ETHUSDT")`
- «¿Qué pasó con la orden mcp-…?» → `get_order(symbol=..., client_order_id="mcp-...")`

Si `diagnose_connection` reporta `clock_status` distinto de `ok`, sincronizá el reloj del
sistema: Binance rechaza firmas con más de 1 s de adelanto o fuera de `recvWindow` (`-1021`).

## Skill incluida: `binance-account-review`

`skills/binance-account-review/SKILL.md` guía al agente para revisar precios, saldos y
órdenes abiertas y entregar un informe breve en español (fecha, red, resultados, datos
faltantes). Es de solo lectura: no envía órdenes, no recomienda ni pide secretos.

Instalarla en Claude Code (requiere este MCP conectado):

```bash
cp -r skills/binance-account-review ~/.claude/skills/
```

O por proyecto: copiala a `.claude/skills/` del repo donde la uses. Reiniciá la sesión y
pedí, por ejemplo, «revisá mi cuenta de Binance».

## Desarrollo y verificación

```bash
uv run --group dev pytest -q
```

Pruebas unitarias con transporte HTTP simulado: cubren bloqueo de operaciones,
validaciones, precisión, errores, resultados inciertos y ausencia de secretos. **No
acreditan integración real.**

```bash
uv run python scripts/verify/run_verify.py
```

Verificación de integración real: levanta el servidor por stdio con el cliente MCP oficial
y llama **solo herramientas de lectura** con `BINANCE_ALLOW_TRADING=false`:

1. Consultas públicas contra Binance **real**, sin credenciales (el proceso hijo no hereda
   ninguna variable `BINANCE_*`).
2. Consultas privadas contra **testnet**, solo si existen `BINANCE_TESTNET_API_KEY` y
   `BINANCE_TESTNET_API_SECRET` en el entorno o en `.env` (que el script carga
   explícitamente). Nunca usa `BINANCE_API_KEY` del entorno, para no mandar claves de
   producción a testnet. Si faltan, el escenario queda «NO VERIFICADO».

La evidencia queda en `scripts/verify/out/<fecha>/` (ignorada por Git: puede tener datos de
cuenta). `summary.json` es un resumen sin montos, apto para compartir.

## Cambios de compatibilidad (0.2.0)

- **Eliminada** la herramienta `withdraw` y la variable `ENABLE_WITHDRAWALS` (si sigue
  definida, se ignora y `diagnose_connection` lo avisa en `legacy_env_ignored`).
- **Nuevo default de solo lectura**: `place_order`, `cancel_order` y `cancel_all_orders`
  devuelven `TRADING_DISABLED` salvo `BINANCE_ALLOW_TRADING=true`.
- **Formato de respuesta**: todas las herramientas devuelven `{ok, network,
  trading_enabled, data | error}` en vez del JSON crudo de Binance (ahora en `data`).
- `place_order`: `time_in_force` ya no tiene default `GTC` en la firma (se aplica solo a
  `LIMIT`); `MARKET` con `price` o `time_in_force` se rechaza; las cantidades se validan
  contra las reglas del símbolo y no se redondean.
- `BINANCE_TESTNET` solo acepta `true/false/1/0/yes/no`; antes cualquier otro valor
  activaba mainnet.
- Mainnet público usa `data-api.binance.vision` y no envía la API key.
- `get_account` omite saldos en cero (`omitZeroBalances=true`).
- `get_my_trades` devuelve `{trades, note}`.
- Nuevas herramientas: `get_order`, `diagnose_connection`.

## Aviso

Proyecto educativo. Operar con criptomonedas conlleva riesgo financiero; el autor no se
hace responsable de pérdidas. Empezá siempre en testnet y con permisos mínimos.

## Stack

Python 3.10+ · [MCP SDK](https://github.com/modelcontextprotocol/python-sdk) (FastMCP) ·
[httpx](https://www.python-httpx.org/) · [uv](https://docs.astral.sh/uv/)

## Licencia

MIT — ver [LICENSE](LICENSE).
