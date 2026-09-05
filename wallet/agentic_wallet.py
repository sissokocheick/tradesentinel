"""
TradeSentinel — Agentic Wallet Interface
Wraps Binance's Agentic sub-account operations:
- Balance queries
- Internal transfers (main → agentic)
- On-chain operations (future: swaps, DeFi)
"""

import asyncio
import logging
import os
from typing import Optional

from mcp.binance_mcp_client import BinanceMCPClient

log = logging.getLogger("agentic_wallet")

AGENTIC_ACCOUNT_ID = os.getenv("BINANCE_AGENTIC_ACCOUNT_ID", "")


class AgenticWallet:
    """
    Interface for the Binance Agentic sub-account.
    The Agentic sub-account is ISOLATED from the main account —
    funds cannot be withdrawn externally, only used for trading within Binance.
    This is a core safety feature of Agent OS.
    """

    def __init__(self, mcp: BinanceMCPClient):
        self.mcp = mcp
        self._cached_balance: Optional[dict] = None

    async def get_balance(self, asset: str = "USDC") -> float:
        """Get available balance for a specific asset in the Agentic sub-account."""
        try:
            balance_data = await self.mcp.get_account_balance()
            balances = balance_data.get("balances", [])
            for b in balances:
                if b.get("asset") == asset:
                    free = float(b.get("free", 0))
                    log.info(f"Agentic wallet balance: {free} {asset}")
                    return free
            return 0.0
        except Exception as e:
            log.error(f"Failed to fetch agentic wallet balance: {e}")
            return 0.0

    async def get_all_balances(self) -> dict[str, dict]:
        """Get all non-zero balances in the Agentic sub-account."""
        try:
            balance_data = await self.mcp.get_account_balance()
            balances = balance_data.get("balances", [])
            return {
                b["asset"]: {
                    "free": float(b.get("free", 0)),
                    "locked": float(b.get("locked", 0)),
                }
                for b in balances
                if float(b.get("free", 0)) > 0 or float(b.get("locked", 0)) > 0
            }
        except Exception as e:
            log.error(f"Failed to fetch all balances: {e}")
            return {}

    async def get_total_usdc_value(self) -> float:
        """Estimate total portfolio value in USDC (spot positions only)."""
        balances = await self.get_all_balances()
        total = 0.0
        for asset, amounts in balances.items():
            qty = amounts["free"] + amounts["locked"]
            if asset in ("USDT", "USDC", "BUSD"):
                total += qty
            else:
                try:
                    ticker = await self.mcp.get_ticker(f"{asset}USDT")
                    price = float(ticker.get("price", 0))
                    total += qty * price
                except Exception:
                    pass
        return round(total, 2)

    def format_balance_report(self, balances: dict) -> str:
        """Human-readable balance report for the dashboard."""
        if not balances:
            return "No assets in Agentic sub-account."
        lines = ["📊 Agentic Wallet Balances:"]
        for asset, amounts in balances.items():
            lines.append(f"  {asset}: {amounts['free']:.6f} free | {amounts['locked']:.6f} locked")
        return "\n".join(lines)
