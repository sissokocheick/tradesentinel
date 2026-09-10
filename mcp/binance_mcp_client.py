"""
TradeSentinel — Binance MCP Client (v2)
Supports TWO authentication modes:

MODE A — MCP OAuth (recommended, no API keys needed)
  The Binance MCP server authenticates via browser OAuth.
  Used when running through Claude Code, Claude Desktop, etc.
  Set BINANCE_MCP_AUTH_MODE=oauth in .env

MODE B — Traditional API Keys (for autonomous Python agent)
  Generate keys from a dedicated Binance sub-account.
  Set BINANCE_MCP_AUTH_MODE=apikey in .env
  Requires: BINANCE_MCP_API_KEY + BINANCE_MCP_SECRET

Official MCP endpoint: https://agent.binance.com/mcp/agentic
"""

import asyncio
import hmac
import hashlib
import json
import logging
import os
import time
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("mcp_client")

# ── Config ────────────────────────────────────────────────────
MCP_BASE_URL   = os.getenv("BINANCE_MCP_URL", "https://agent.binance.com/mcp/agentic")
AUTH_MODE      = os.getenv("BINANCE_MCP_AUTH_MODE", "oauth")   # "oauth" | "apikey"
MCP_API_KEY    = os.getenv("BINANCE_MCP_API_KEY", "")
MCP_SECRET     = os.getenv("BINANCE_MCP_SECRET", "")
MCP_SESSION    = os.getenv("BINANCE_MCP_SESSION_TOKEN", "")    # OAuth session token


class BinanceMCPClient:
    """
    Unified async client for the Binance Agent OS MCP Server.
    Now with full Spot Testnet support.
    """

    def __init__(
        self,
        base_url:    str = MCP_BASE_URL,
        auth_mode:   str = AUTH_MODE,
        api_key:     str = MCP_API_KEY,
        secret:      str = MCP_SECRET,
        session_token: str = MCP_SESSION,
        testnet:     bool = False,
    ):
        self.testnet       = testnet
        if self.testnet:
            log.info("Testnet enabled: overriding keys and URLs for testnet.binance.vision")
            self.api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
            self.secret = os.getenv("BINANCE_TESTNET_SECRET", "")
            self.base_url = "https://testnet.binance.vision"
            self.auth_mode = "apikey"
        else:
            self.base_url      = base_url.rstrip("/")
            self.auth_mode     = auth_mode
            self.api_key       = api_key
            self.secret        = secret

        self.session_token = session_token
        self._client       = httpx.AsyncClient(timeout=15.0)
        log.info(f"BinanceMCPClient initialized | auth_mode={self.auth_mode} | endpoint={self.base_url}")

    # ─────────────────────────────────────────────────────────
    # Public Market Data
    # Uses Binance public REST API as fallback when MCP OAuth
    # session is not available (pure market data, no auth needed).
    # ─────────────────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> dict:
        """Live price ticker — e.g. BTCUSDT."""
        return await self._public_rest("GET", "/api/v3/ticker/price", {"symbol": symbol})

    async def get_24h_stats(self, symbol: str) -> dict:
        """24-hour price change statistics."""
        return await self._public_rest("GET", "/api/v3/ticker/24hr", {"symbol": symbol})

    async def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> list:
        """Candlestick (OHLCV) data. interval: 1m 5m 15m 1h 4h 1d"""
        return await self._public_rest("GET", "/api/v3/klines", {
            "symbol": symbol, "interval": interval, "limit": limit
        })

    async def get_orderbook(self, symbol: str, limit: int = 20) -> dict:
        """Order book depth."""
        return await self._public_rest("GET", "/api/v3/depth", {"symbol": symbol, "limit": limit})

    async def get_funding_rate(self, symbol: str) -> dict:
        """Futures funding rate."""
        return await self._public_rest("GET", "/fapi/v1/fundingRate", {"symbol": symbol, "limit": 1})

    # ─────────────────────────────────────────────────────────
    # Account (requires Account scope)
    # ─────────────────────────────────────────────────────────

    async def get_account_balance(self) -> dict:
        """Agentic sub-account balances across all wallets."""
        return await self._call("account/balance", signed=True)

    async def get_open_orders(self, symbol: Optional[str] = None) -> list:
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._call("account/openOrders", params, signed=True)

    async def get_trade_history(self, symbol: str, limit: int = 50) -> list:
        return await self._call("account/trades", {"symbol": symbol, "limit": limit}, signed=True)

    # ─────────────────────────────────────────────────────────
    # Trading (requires Trade scope)
    # Every order requires user confirmation per Binance docs.
    # Our Executor Agent handles this confirmation gate.
    # ─────────────────────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side:   str,       # "BUY" | "SELL"
        quantity: float,
        client_order_id: Optional[str] = None,
    ) -> dict:
        payload = {
            "symbol":   symbol,
            "side":     side,
            "type":     "MARKET",
            "quantity": str(round(quantity, 8)),
        }
        if client_order_id:
            payload["newClientOrderId"] = client_order_id
        return await self._call("order", payload, signed=True, method="POST")

    async def place_limit_order(
        self,
        symbol:        str,
        side:          str,
        quantity:      float,
        price:         float,
        time_in_force: str = "GTC",
    ) -> dict:
        payload = {
            "symbol":      symbol,
            "side":        side,
            "type":        "LIMIT",
            "quantity":    str(round(quantity, 8)),
            "price":       str(price),
            "timeInForce": time_in_force,
        }
        return await self._call("order", payload, signed=True, method="POST")

    async def place_stop_loss_order(
        self,
        symbol:        str,
        side:          str,
        quantity:      float,
        stop_price:    float,
        limit_price:   Optional[float] = None,
        time_in_force: str = "GTC",
    ) -> dict:
        limit_price = limit_price or stop_price
        payload = {
            "symbol":      symbol,
            "side":        side,
            "type":        "STOP_LOSS_LIMIT",
            "quantity":    str(quantity),
            "stopPrice":   str(stop_price),
            "price":       str(limit_price),
            "timeInForce": time_in_force,
        }
        return await self._call("order", payload, signed=True, method="POST")

    async def cancel_order(self, symbol: str, order_id: str) -> dict:
        return await self._call("order", {"symbol": symbol, "orderId": order_id}, signed=True, method="DELETE")

    # ─────────────────────────────────────────────────────────
    # Internal transfer (within Agentic sub-account only)
    # Per docs: Transfer scope only moves funds between wallets
    # INSIDE the sub-account. Cannot pull from main account.
    # ─────────────────────────────────────────────────────────

    async def internal_transfer(self, asset: str, amount: float, from_wallet: str, to_wallet: str) -> dict:
        """Move funds between wallets inside the Agentic sub-account."""
        payload = {
            "asset":      asset,
            "amount":     str(amount),
            "fromWallet": from_wallet,   # e.g. "SPOT"
            "toWallet":   to_wallet,     # e.g. "USDM_FUTURES"
        }
        return await self._call("account/transfer", payload, signed=True, method="POST")

    # ─────────────────────────────────────────────────────────
    # MCP JSON-RPC call (direct protocol access)
    # ─────────────────────────────────────────────────────────

    async def mcp_jsonrpc(self, tool_name: str, arguments: dict) -> dict:
        """
        Send a raw MCP JSON-RPC 2.0 request directly to the server.
        Used when running in OAuth mode where the session token is available.
        """
        payload = {
            "jsonrpc": "2.0",
            "id":      int(time.time() * 1000),
            "method":  "tools/call",
            "params":  {
                "name":      tool_name,
                "arguments": arguments,
            }
        }
        headers = self._auth_headers(oauth=True)
        try:
            resp = await self._client.post(self.base_url, headers=headers, json=payload)
            resp.raise_for_status()
            result = resp.json()
            if "error" in result:
                raise BinanceMCPError(f"MCP error: {result['error']}")
            return result.get("result", {})
        except httpx.HTTPStatusError as e:
            raise BinanceMCPError(f"HTTP {e.response.status_code}: {e.response.text}")

    # ─────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────

    async def _call(
        self,
        path:    str,
        params:  dict = None,
        signed:  bool = False,
        method:  str  = "GET",
    ) -> Any:
        """Send request. If on testnet, send direct signed REST. Else, MCP JSON-RPC."""
        if self.testnet:
            # Map MCP endpoints to Standard Spot REST endpoints
            rest_path = path
            if path == "account/balance":
                rest_path = "account"
                method = "GET"
            elif path == "account/openOrders":
                rest_path = "openOrders"
                method = "GET"
            elif path == "account/trades":
                rest_path = "myTrades"
                method = "GET"
                
            return await self._signed_rest(method, f"/api/v3/{rest_path}", params)
            
        # When using MCP JSON-RPC protocol:
        return await self.mcp_jsonrpc(path, params or {})

    async def _signed_rest(self, method: str, path: str, params: dict = None) -> Any:
        """Direct Binance signed REST call (used for testnet)."""
        params = params or {}
        params['timestamp'] = int(time.time() * 1000)
        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        signature = hmac.new(self.secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
        params['signature'] = signature
        
        url = self.base_url + path
        headers = {"X-MBX-APIKEY": self.api_key}
        try:
            if method == "GET":
                resp = await self._client.get(url, headers=headers, params=params)
            elif method == "DELETE":
                resp = await self._client.delete(url, headers=headers, params=params)
            else:
                resp = await self._client.post(url, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise BinanceMCPError(f"REST {e.response.status_code} on {path}: {e.response.text[:200]}")

    async def _public_rest(self, method: str, path: str, params: dict = None) -> Any:
        """Call the Binance public REST API."""
        base = self.base_url if self.testnet else "https://api.binance.com"
        if not self.testnet and path.startswith("/fapi"):
            base = "https://fapi.binance.com"
        url = base + path
        headers = {"Content-Type": "application/json"}
        try:
            if method == "GET":
                resp = await self._client.get(url, headers=headers, params=params or {})
            else:
                resp = await self._client.post(url, headers=headers, json=params or {})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise BinanceMCPError(f"REST {e.response.status_code} on {path}: {e.response.text[:200]}")
        except httpx.RequestError as e:
            raise BinanceMCPError(f"Connection error on {path}: {e}")

    def _auth_headers(self, oauth: bool = False) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.auth_mode == "oauth" or oauth:
            if self.session_token:
                headers["Authorization"] = f"Bearer {self.session_token}"
        elif self.auth_mode == "apikey":
            if self.api_key:
                headers["X-MBX-APIKEY"] = self.api_key
        return headers

    def _sign(self, data: dict) -> dict:
        """HMAC-SHA256 signature for API key mode."""
        data["timestamp"] = int(time.time() * 1000)
        query_string = "&".join(f"{k}={v}" for k, v in sorted(data.items()))
        signature = hmac.new(
            self.secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        data["signature"] = signature
        return data

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()


class BinanceMCPError(Exception):
    pass


# ─────────────────────────────────────────────────────────────
# Quick connectivity test (public endpoint, no auth needed)
# ─────────────────────────────────────────────────────────────

async def test_connection():
    print("Testing Binance MCP Server connection (public market data)...")
    async with BinanceMCPClient() as client:
        try:
            ticker = await client.get_ticker("BTCUSDT")
            price  = ticker.get("price", "N/A")
            print(f"  ✅ BTC price : ${float(price):,.2f}")

            stats  = await client.get_24h_stats("BNBUSDT")
            change = stats.get("priceChangePercent", "N/A")
            print(f"  ✅ BNB 24h   : {change}%")

            ob = await client.get_orderbook("ETHUSDT", limit=5)
            bids = ob.get("bids", [])
            print(f"  ✅ ETH top bid : ${float(bids[0][0]):,.2f}" if bids else "  ⚠️ No order book data")

            print("\n✅ MCP Server reachable and returning live data!")
        except BinanceMCPError as e:
            print(f"\n❌ MCP connection failed: {e}")
            print("   → Make sure you are connected via Claude Code first:")
            print("   → claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic")


if __name__ == "__main__":
    asyncio.run(test_connection())
