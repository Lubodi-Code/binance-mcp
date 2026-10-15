"""
Verificacion de integracion real del MCP de Binance (solo lectura).

Levanta server.py por stdio con el cliente MCP oficial y llama UNICAMENTE herramientas de
lectura, con BINANCE_ALLOW_TRADING=false forzado:

  1. mainnet-public : consultas publicas contra Binance real, SIN credenciales.
  2. testnet-private: consultas privadas contra testnet, solo si existen
     BINANCE_TESTNET_API_KEY y BINANCE_TESTNET_API_SECRET (entorno o .env).

Nunca reutiliza BINANCE_API_KEY/BINANCE_API_SECRET del entorno padre: asi no se envian
claves de produccion a testnet ni a la corrida publica. Las credenciales de testnet se leen
unicamente para autenticar; nunca se imprimen ni se guardan en la evidencia.

Uso (desde la raiz del repo):
    uv run python scripts/verify/run_verify.py

Evidencia: scripts/verify/out/<fecha>/ (ignorado por Git; contiene datos de cuenta).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[2]
OUT_ROOT = REPO / "scripts" / "verify" / "out"
COMMAND = "uv run python scripts/verify/run_verify.py"

# Lista blanca: cualquier otra herramienta se rechaza antes de llamarla.
READ_ONLY_TOOLS = {
    "diagnose_connection", "get_price", "get_24h_stats", "get_klines",
    "get_order_book", "get_account", "get_open_orders", "get_my_trades", "get_order",
}
MUTATING_TOOLS = {"place_order", "cancel_order", "cancel_all_orders", "withdraw"}

PUBLIC_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("diagnose_connection", {}),
    ("get_price", {"symbol": "BTCUSDT"}),
    ("get_24h_stats", {"symbol": "BTCUSDT"}),
    ("get_order_book", {"symbol": "BTCUSDT", "limit": 5}),
    ("get_klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 3}),
    ("get_price", {"symbol": "NOEXISTE123"}),   # error controlado esperado
    ("get_account", {}),                        # MISSING_CREDENTIALS esperado
]
PRIVATE_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("diagnose_connection", {}),
    ("get_price", {"symbol": "BTCUSDT"}),
    ("get_account", {}),
    ("get_open_orders", {}),
    ("get_my_trades", {"symbol": "BTCUSDT", "limit": 5}),
]


def load_dotenv_testnet(path: Path) -> dict[str, str]:
    """Carga explicita y minima de .env: solo las variables de testnet de verificacion."""
    wanted = {"BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"}
    found: dict[str, str] = {}
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        if key in wanted:
            found[key] = value.strip().strip('"').strip("'")
    return found


def base_child_env() -> dict[str, str]:
    """Entorno del hijo sin NINGUNA variable BINANCE_* heredada."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("BINANCE_")}
    env["BINANCE_ALLOW_TRADING"] = "false"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def make_redactor(secrets: list[str]):
    secrets = [s for s in secrets if len(s) >= 4]

    def redact(text: str) -> str:
        text = re.sub(r"(signature=)[^&\s'\"]+", r"\1<redacted>", text, flags=re.I)
        for secret in secrets:
            text = text.replace(secret, "<redacted>")
        return text

    return redact


def parse_result(result: Any) -> Any:
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except ValueError:
                return {"raw_text": text}
    return None


async def run_scenario(name: str, env: dict[str, str], calls, redact, out_dir: Path) -> dict[str, Any]:
    stderr_tmp = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    params = StdioServerParameters(command=sys.executable, args=[str(REPO / "server.py")], env=env, cwd=str(REPO))
    record: dict[str, Any] = {"scenario": name, "env_names": sorted(k for k in env if k.startswith("BINANCE_")), "calls": []}
    async with stdio_client(params, errlog=stderr_tmp) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            record["tools_listed"] = sorted(tools)
            record["withdraw_absent"] = "withdraw" not in tools
            for tool, args in calls:
                if tool not in READ_ONLY_TOOLS or tool in MUTATING_TOOLS:
                    raise RuntimeError(f"Herramienta no permitida en verificacion: {tool}")
                started = datetime.now(timezone.utc).isoformat()
                result = await session.call_tool(tool, args)
                record["calls"].append({
                    "tool": tool,
                    "args": args,
                    "called_at_utc": started,
                    "is_error": bool(result.isError),
                    "response": parse_result(result),
                })
    stderr_tmp.seek(0)
    (out_dir / f"{name}.stderr.log").write_text(redact(stderr_tmp.read()), encoding="utf-8")
    stderr_tmp.close()
    (out_dir / f"{name}.json").write_text(redact(json.dumps(record, indent=2, ensure_ascii=False)), encoding="utf-8")
    return record


def summarize(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Resumen sin montos ni datos de cuenta: apto para compartir."""
    rows = []
    for call in record["calls"]:
        resp = call["response"] or {}
        row: dict[str, Any] = {
            "tool": call["tool"],
            "args": call["args"],
            "ok": resp.get("ok"),
            "network": resp.get("network"),
            "trading_enabled": resp.get("trading_enabled"),
        }
        if not resp.get("ok"):
            row["error_code"] = (resp.get("error") or {}).get("code")
        data = resp.get("data")
        if call["tool"] == "get_account" and isinstance(data, dict):
            row["balances_count"] = len(data.get("balances", []))
        elif call["tool"] == "get_open_orders" and isinstance(data, list):
            row["open_orders_count"] = len(data)
        elif call["tool"] == "get_my_trades" and isinstance(data, dict):
            row["trades_count"] = len(data.get("trades") or [])
        elif call["tool"] == "diagnose_connection" and isinstance(data, dict):
            row["public_api"] = (data.get("public_api") or {}).get("status")
            row["clock_status"] = (data.get("public_api") or {}).get("clock_status")
            row["private_api"] = (data.get("private_api") or {}).get("status")
        rows.append(row)
    return rows


async def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = OUT_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    creds = {k: os.environ[k] for k in ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET") if os.environ.get(k)}
    creds_source = "entorno" if len(creds) == 2 else None
    if len(creds) < 2:
        creds = load_dotenv_testnet(REPO / ".env")
        creds_source = ".env" if len(creds) == 2 and all(creds.values()) else None
    redact = make_redactor(list(creds.values()))

    summary: dict[str, Any] = {"command": COMMAND, "started_utc": stamp, "scenarios": {}}

    public_env = base_child_env()
    public_env["BINANCE_TESTNET"] = "false"
    public = await run_scenario("mainnet-public", public_env, PUBLIC_CALLS, redact, out_dir)
    summary["scenarios"]["mainnet-public"] = {
        "status": "ejecutado",
        "withdraw_absent": public["withdraw_absent"],
        "calls": summarize(public),
    }

    if creds_source:
        private_env = base_child_env()
        private_env["BINANCE_TESTNET"] = "true"
        private_env["BINANCE_API_KEY"] = creds["BINANCE_TESTNET_API_KEY"]
        private_env["BINANCE_API_SECRET"] = creds["BINANCE_TESTNET_API_SECRET"]
        private = await run_scenario("testnet-private", private_env, PRIVATE_CALLS, redact, out_dir)
        summary["scenarios"]["testnet-private"] = {
            "status": "ejecutado",
            "credentials_source": creds_source,
            "calls": summarize(private),
        }
    else:
        summary["scenarios"]["testnet-private"] = {
            "status": "NO VERIFICADO",
            "reason": "Faltan BINANCE_TESTNET_API_KEY y BINANCE_TESTNET_API_SECRET (entorno o .env).",
        }

    text = redact(json.dumps(summary, indent=2, ensure_ascii=False))
    (out_dir / "summary.json").write_text(text, encoding="utf-8")
    print(text)
    print(f"\nEvidencia local: {out_dir.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
