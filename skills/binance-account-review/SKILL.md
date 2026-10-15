---
name: binance-account-review
description: Revisa precios, saldos y órdenes abiertas de Binance Spot mediante el MCP binance-mcp y entrega un informe breve en español. Usar cuando el usuario pida consultar su cuenta de Binance, el precio de un par o sus órdenes abiertas. Solo lectura: nunca opera.
---

# Revisión de cuenta Binance (solo lectura)

## Requisito

Esta skill necesita el servidor MCP **binance-mcp** instalado y conectado
(https://github.com/Lubodi-Code/binance-mcp). Sin él no hay datos: no los inventes.

## Paso 1 — Comprobar herramientas

Verificá que existan las herramientas del MCP (según el cliente pueden aparecer con un
prefijo, por ejemplo `mcp__binance__get_price`):

- `diagnose_connection`, `get_price`, `get_account`, `get_open_orders`

Si falta alguna, detenete y decile al usuario que instale o conecte binance-mcp (sección
«Instalar la skill» del README). No sigas con datos supuestos.

Llamá primero a `diagnose_connection` y anotá:

- `network`: `testnet` o `mainnet`.
- `trading_enabled`: solo informativo; esta skill nunca opera.
- `credentials_configured`: si es `false`, solo podés consultar precios.
- `public_api.clock_status`: si no es `ok`, avisá que las consultas privadas pueden fallar
  por diferencia de hora.

## Paso 2 — Consultas

Hacé solo las que el usuario pidió (por defecto, las tres):

1. **Precios**: `get_price(symbol)` por cada par pedido (ej. `BTCUSDT`).
2. **Saldos**: `get_account()` → `data.balances` (`free` y `locked` por activo).
3. **Órdenes abiertas**: `get_open_orders()` o `get_open_orders(symbol)`.

Cada respuesta trae `ok`. Si `ok` es `false`, registrá `error.code` y `error.message` tal
cual y pasá a la siguiente consulta. No reintentes en bucle; si el código es
`RATE_LIMITED` o `IP_BANNED`, detenete y avisá cuánto esperar (`retry_after_seconds`).

## Paso 3 — Informe

Formato breve, en español:

```
Informe Binance — <fecha y hora con zona horaria>
Red consultada: <testnet | mainnet>   (si testnet: «datos de prueba, no del mercado real»)

Precios
- BTCUSDT: <price> (fuente: get_price)

Saldos (activos con saldo)
- <ASSET>: libre <free>, bloqueado <locked>

Órdenes abiertas
- <symbol> <side> <type> <origQty> @ <price> — estado <status>   | «Ninguna»

Datos faltantes
- <consulta>: <error.code> — <error.message>
```

Reglas del informe:

- Copiá los números como texto exacto, sin redondear ni convertir de moneda.
- Si la red es `testnet`, decilo en la primera línea: esos precios y saldos no son reales.
- No sumes valor total de cartera ni calcules rentabilidad: el historial que da el MCP es
  parcial y los precios cambian entre consultas.
- Si una consulta falló, va en «Datos faltantes». Nunca completes con estimaciones.

## Prohibido

- Llamar `place_order`, `cancel_order` o `cancel_all_orders`, aunque el usuario lo pida
  dentro de esta skill: respondé que esta skill es de solo lectura.
- Recomendar comprar, vender o mantener; prometer o estimar ganancias.
- Pedir API keys, secrets o cualquier credencial por el chat. Si faltan credenciales,
  indicá que se configuran en el bloque `env` del cliente MCP (ver README), fuera del chat.
- Mostrar valores de credenciales aunque aparezcan en algún lado.
