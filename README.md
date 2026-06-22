# Binance MCP Server

> Servidor [MCP (Model Context Protocol)](https://modelcontextprotocol.io) que le da a Claude (u otro cliente MCP) acceso a la REST API de **Binance Spot**: consultar precios, velas, balances, y operar — con los **retiros desactivados por defecto** por seguridad.
>
> An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) access to the **Binance Spot** REST API: prices, candlesticks, balances, and trading — with **withdrawals disabled by default** for safety.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![MCP](https://img.shields.io/badge/MCP-1.2%2B-purple)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 🇪🇸 Español

### ¿Qué es esto?

Un servidor MCP en Python que expone la API de Binance Spot como **herramientas (tools)** que un modelo como Claude puede invocar en lenguaje natural. Le preguntas *"¿a cuánto está el BTC?"* o *"compra 0.01 ETH"* y el modelo llama a la herramienta correspondiente.

### Seguridad primero 🔒

Mover dinero con un LLM es delicado, así que el servidor está diseñado a la defensiva:

- **Retiros desactivados a nivel de código.** `withdraw` no hace nada salvo que pongas `ENABLE_WITHDRAWALS=true` *y* la API key tenga permiso de retiro. La recomendación es **no dárselo**.
- **Testnet por defecto** (`BINANCE_TESTNET=true`): prueba contra el entorno de pruebas de Binance antes de tocar fondos reales.
- **Firma HMAC SHA256** en cada request privado, igual que exige Binance.
- Recomendado: crear la API key **sin permiso de retiro** y con **IP whitelist**.

### Herramientas disponibles

| Herramienta | Tipo | Descripción |
|---|---|---|
| `get_price` | pública | Precio actual de un símbolo (`BTCUSDT`) |
| `get_24h_stats` | pública | Estadísticas 24h: volumen, cambio %, máx/mín |
| `get_klines` | pública | Velas / candlesticks (`1m`…`1w`) |
| `get_order_book` | pública | Libro de órdenes (bids/asks) |
| `get_account` | firmada | Balances y permisos de tu cuenta spot |
| `get_open_orders` | firmada | Órdenes abiertas |
| `get_my_trades` | firmada | Tu historial de trades |
| `place_order` | firmada | Crea orden `MARKET` o `LIMIT` |
| `cancel_order` | firmada | Cancela una orden por `orderId` |
| `cancel_all_orders` | firmada | Cancela todas las órdenes de un símbolo |
| `withdraw` | firmada | Retiro (**desactivado por defecto**) |

### Instalación

Requiere [uv](https://docs.astral.sh/uv/) (gestor de paquetes de Python).

```bash
git clone https://github.com/Lubodi-Code/binance-mcp.git
cd binance-mcp
uv sync
```

### Configuración

Copia `.env.example` a `.env` y rellena tus claves de Binance:

```bash
cp .env.example .env
```

```env
BINANCE_API_KEY=tu_api_key
BINANCE_API_SECRET=tu_secret
BINANCE_TESTNET=true          # empieza siempre en testnet
ENABLE_WITHDRAWALS=false      # no lo cambies salvo que sepas lo que haces
```

> Para el testnet de spot, genera tus claves en https://testnet.binance.vision

### Conectarlo a Claude Desktop

Añade esto a tu `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "binance": {
      "command": "uv",
      "args": ["--directory", "/ruta/absoluta/a/binance-mcp", "run", "server.py"],
      "env": {
        "BINANCE_API_KEY": "tu_api_key",
        "BINANCE_API_SECRET": "tu_secret",
        "BINANCE_TESTNET": "true"
      }
    }
  }
}
```

Reinicia Claude Desktop y pídele, por ejemplo: *"¿a cuánto está el BTC y cómo se movió en las últimas 24h?"*

---

## 🇬🇧 English

### What is this?

A Python MCP server that exposes the Binance Spot API as **tools** an LLM like Claude can call from natural language. Ask *"what's the BTC price?"* or *"buy 0.01 ETH"* and the model invokes the right tool.

### Safety first 🔒

Moving money with an LLM is risky, so the server is defensive by design:

- **Withdrawals disabled in code.** `withdraw` is a no-op unless you set `ENABLE_WITHDRAWALS=true` *and* the API key has withdraw permission. The recommendation is **not to grant it**.
- **Testnet by default** (`BINANCE_TESTNET=true`): test against Binance's sandbox before touching real funds.
- **HMAC SHA256 signing** on every private request, as Binance requires.
- Recommended: create the API key **without withdraw permission** and with an **IP whitelist**.

### Available tools

| Tool | Type | Description |
|---|---|---|
| `get_price` | public | Current price of a symbol (`BTCUSDT`) |
| `get_24h_stats` | public | 24h stats: volume, % change, high/low |
| `get_klines` | public | Candlesticks (`1m`…`1w`) |
| `get_order_book` | public | Order book (bids/asks) |
| `get_account` | signed | Spot account balances & permissions |
| `get_open_orders` | signed | Open orders |
| `get_my_trades` | signed | Your trade history |
| `place_order` | signed | Create a `MARKET` or `LIMIT` order |
| `cancel_order` | signed | Cancel an order by `orderId` |
| `cancel_all_orders` | signed | Cancel all orders for a symbol |
| `withdraw` | signed | Withdraw (**disabled by default**) |

### Install

Requires [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Lubodi-Code/binance-mcp.git
cd binance-mcp
uv sync
cp .env.example .env   # then fill in your keys
```

### Run

```bash
uv run server.py
```

Then wire it into Claude Desktop's `claude_desktop_config.json` (see the Spanish section above for the exact config block).

---

## ⚠️ Disclaimer

Este proyecto es educativo. Operar con criptomonedas conlleva riesgo financiero. El autor no se hace responsable de pérdidas. Úsalo bajo tu propia responsabilidad y empieza siempre en testnet.

This project is for educational purposes. Crypto trading carries financial risk. The author is not liable for any losses. Use at your own risk and always start on testnet.

## Stack

Python 3.10+ · [MCP SDK](https://github.com/modelcontextprotocol/python-sdk) (FastMCP) · [httpx](https://www.python-httpx.org/) · [uv](https://docs.astral.sh/uv/)

## License

MIT — see [LICENSE](LICENSE).
