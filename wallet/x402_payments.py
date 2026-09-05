"""
TradeSentinel — x402 Payment Client
Handles machine-to-machine micropayments for premium data services
using the Binance x402 protocol (HTTP 402 Payment Required flow).
"""

import asyncio
import logging
import os
import httpx

log = logging.getLogger("x402")

X402_ENDPOINT     = os.getenv("BINANCE_X402_ENDPOINT", "https://agent.binance.com/x402")
X402_API_KEY      = os.getenv("BINANCE_X402_API_KEY", "")
PREMIUM_DATA_COST = float(os.getenv("X402_PREMIUM_DATA_COST_USDC", "0.001"))   # 0.001 USDC per call


class X402Client:
    """
    Binance x402 client for agent-driven micropayments.
    Used to unlock premium market data endpoints that require payment.
    Payments are settled automatically from the Agentic sub-account,
    no human intervention required.
    """

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=10.0)
        self._headers = {
            "X-MBX-APIKEY": X402_API_KEY,
            "Content-Type": "application/json",
        }
        self._total_spent = 0.0

    async def pay_for_trade_data(self, symbol: str) -> dict:
        """
        Pay for premium trade data for a given symbol.
        Returns a receipt confirming payment + data access.
        """
        payload = {
            "service": "premium_market_data",
            "symbol": symbol,
            "amount_usdc": PREMIUM_DATA_COST,
        }
        try:
            resp = await self._client.post(
                f"{X402_ENDPOINT}/pay",
                headers=self._headers,
                json=payload,
            )
            if resp.status_code == 402:
                # Standard x402 flow: server requests payment terms
                payment_terms = resp.json()
                log.info(f"x402 payment terms received for {symbol}: {payment_terms}")
                # Submit payment
                pay_resp = await self._client.post(
                    f"{X402_ENDPOINT}/settle",
                    headers=self._headers,
                    json={"payment_token": payment_terms.get("token"), **payload},
                )
                pay_resp.raise_for_status()
                receipt = pay_resp.json()
            elif resp.is_success:
                receipt = resp.json()
            else:
                resp.raise_for_status()
                receipt = {}

            self._total_spent += PREMIUM_DATA_COST
            log.info(f"x402 payment settled for {symbol} | Total spent: ${self._total_spent:.4f}")
            return receipt

        except Exception as e:
            log.warning(f"x402 payment failed (non-blocking): {e}")
            return {}

    async def get_spending_summary(self) -> dict:
        return {
            "total_spent_usdc": self._total_spent,
            "cost_per_call_usdc": PREMIUM_DATA_COST,
        }

    async def close(self):
        await self._client.aclose()
