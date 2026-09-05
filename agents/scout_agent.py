"""
TradeSentinel — Scout Agent
Continuously monitors Binance markets via the MCP Server.
Publishes structured MarketSnapshot objects consumed by the Strategist.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

from mcp.binance_mcp_client import BinanceMCPClient, BinanceMCPError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SCOUT] %(message)s")
log = logging.getLogger("scout")

# ── Symbols to watch ─────────────────────────────────────────
WATCHED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
POLL_INTERVAL_SEC = int(os.getenv("SCOUT_POLL_INTERVAL", "30"))


@dataclass
class MarketSnapshot:
    """Structured market data packet passed to the Strategist."""
    symbol: str
    price: float
    price_change_24h_pct: float
    volume_24h: float
    high_24h: float
    low_24h: float
    bid: float
    ask: float
    spread_pct: float
    rsi_14: Optional[float]          # Computed from klines
    macd_signal: Optional[str]       # "bullish" | "bearish" | "neutral"
    bb_position: Optional[str]       # "above_upper" | "below_lower" | "middle"
    order_book_imbalance: float      # +1 = heavy buy pressure, -1 = heavy sell
    funding_rate: Optional[float]    # Futures funding rate (if available)
    timestamp: str = ""

    def __post_init__(self):
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    def to_llm_summary(self) -> str:
        """Human-readable summary for the Strategist LLM prompt."""
        rsi_str     = f"{self.rsi_14:.1f}"     if self.rsi_14     is not None else "N/A"
        funding_str = f"{self.funding_rate:.4f}" if self.funding_rate is not None else "N/A"
        return (
            f"[{self.symbol}] Price: ${self.price:,.2f} | "
            f"24h: {self.price_change_24h_pct:+.2f}% | "
            f"Vol: ${self.volume_24h:,.0f} | "
            f"RSI(14): {rsi_str} | "
            f"MACD: {self.macd_signal or 'N/A'} | "
            f"BB: {self.bb_position or 'N/A'} | "
            f"OB Imbalance: {self.order_book_imbalance:+.2f} | "
            f"Funding: {funding_str}"
        )


class ScoutAgent:
    """
    Polls Binance MCP Server at regular intervals.
    Computes derived technical indicators from raw kline data.
    Publishes MarketSnapshot objects to an asyncio queue.
    """

    def __init__(self, mcp: BinanceMCPClient, output_queue: asyncio.Queue):
        self.mcp = mcp
        self.queue = output_queue
        self._running = False

    async def start(self):
        log.info(f"Scout started — watching {WATCHED_SYMBOLS} every {POLL_INTERVAL_SEC}s")
        self._running = True
        while self._running:
            snapshots = await asyncio.gather(
                *[self._scan_symbol(sym) for sym in WATCHED_SYMBOLS],
                return_exceptions=True,
            )
            valid_snaps = {}
            for snap in snapshots:
                if isinstance(snap, Exception):
                    log.warning(f"Scan error: {snap}")
                    continue
                valid_snaps[snap.symbol] = snap.to_dict()
                await self.queue.put(snap)
                log.info(snap.to_llm_summary())

            try:
                os.makedirs("logs", exist_ok=True)
                with open("logs/market_snapshots.json", "w", encoding="utf-8") as f:
                    json.dump(valid_snaps, f, indent=2)
            except Exception as e:
                log.warning(f"Failed to save market snapshots: {e}")

            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def stop(self):
        self._running = False
        log.info("Scout stopped.")

    # ── Per-symbol scan ──────────────────────────────────────

    async def _scan_symbol(self, symbol: str) -> MarketSnapshot:
        ticker, stats, klines, ob = await asyncio.gather(
            self.mcp.get_ticker(symbol),
            self.mcp.get_24h_stats(symbol),
            self.mcp.get_klines(symbol, interval="1h", limit=50),
            self.mcp.get_orderbook(symbol, limit=20),
            return_exceptions=True,
        )

        # Funding rate (best-effort, futures only)
        funding_rate = None
        try:
            fr_data = await self.mcp.get_funding_rate(symbol.replace("USDT", "USDT_PERP"))
            funding_rate = float(fr_data.get("lastFundingRate", 0))
        except Exception:
            pass

        price            = float(ticker.get("price", 0))             if not isinstance(ticker, Exception) else 0.0
        change_24h_pct   = float(stats.get("priceChangePercent", 0)) if not isinstance(stats, Exception)  else 0.0
        volume_24h       = float(stats.get("quoteVolume", 0))        if not isinstance(stats, Exception)   else 0.0
        high_24h         = float(stats.get("highPrice", 0))          if not isinstance(stats, Exception)   else 0.0
        low_24h          = float(stats.get("lowPrice", 0))           if not isinstance(stats, Exception)   else 0.0

        bid, ask, spread_pct = self._compute_spread(ob)
        rsi_14             = self._compute_rsi(klines, period=14)
        macd_signal        = self._compute_macd_signal(klines)
        bb_position        = self._compute_bollinger_position(klines, price)
        ob_imbalance       = self._compute_ob_imbalance(ob)

        return MarketSnapshot(
            symbol=symbol,
            price=price,
            price_change_24h_pct=change_24h_pct,
            volume_24h=volume_24h,
            high_24h=high_24h,
            low_24h=low_24h,
            bid=bid,
            ask=ask,
            spread_pct=spread_pct,
            rsi_14=rsi_14,
            macd_signal=macd_signal,
            bb_position=bb_position,
            order_book_imbalance=ob_imbalance,
            funding_rate=funding_rate,
        )

    # ── Technical indicator helpers ──────────────────────────

    def _compute_rsi(self, klines, period: int = 14) -> Optional[float]:
        """Relative Strength Index."""
        if isinstance(klines, Exception) or len(klines) < period + 1:
            return None
        closes = [float(k[4]) for k in klines]  # index 4 = close price
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas[-period:]]
        losses = [abs(min(d, 0)) for d in deltas[-period:]]
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    def _compute_macd_signal(self, klines) -> Optional[str]:
        """Simple MACD crossover direction."""
        if isinstance(klines, Exception) or len(klines) < 26:
            return None
        closes = [float(k[4]) for k in klines]
        ema12 = self._ema(closes, 12)
        ema26 = self._ema(closes, 26)
        macd_line = ema12 - ema26
        signal    = self._ema([macd_line], 1)  # simplified
        if macd_line > 0:
            return "bullish"
        elif macd_line < 0:
            return "bearish"
        return "neutral"

    def _ema(self, data: list[float], period: int) -> float:
        """Exponential Moving Average."""
        k = 2 / (period + 1)
        ema = data[0]
        for price in data[1:]:
            ema = price * k + ema * (1 - k)
        return ema

    def _compute_bollinger_position(self, klines, current_price: float, period: int = 20) -> Optional[str]:
        """Where current price sits relative to Bollinger Bands."""
        if isinstance(klines, Exception) or len(klines) < period:
            return None
        closes = [float(k[4]) for k in klines[-period:]]
        sma = sum(closes) / period
        std = (sum((c - sma) ** 2 for c in closes) / period) ** 0.5
        upper = sma + 2 * std
        lower = sma - 2 * std
        if current_price > upper:
            return "above_upper"
        elif current_price < lower:
            return "below_lower"
        return "middle"

    def _compute_spread(self, ob) -> tuple[float, float, float]:
        if isinstance(ob, Exception):
            return 0.0, 0.0, 0.0
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        if not bids or not asks:
            return 0.0, 0.0, 0.0
        bid = float(bids[0][0])
        ask = float(asks[0][0])
        spread_pct = ((ask - bid) / bid) * 100 if bid > 0 else 0
        return bid, ask, round(spread_pct, 4)

    def _compute_ob_imbalance(self, ob) -> float:
        """Order book imbalance: +1 = all bids, -1 = all asks."""
        if isinstance(ob, Exception):
            return 0.0
        bid_vol = sum(float(b[1]) for b in ob.get("bids", []))
        ask_vol = sum(float(a[1]) for a in ob.get("asks", []))
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return round((bid_vol - ask_vol) / total, 4)


# ── Standalone test ───────────────────────────────────────────

async def main():
    mcp   = BinanceMCPClient()
    queue = asyncio.Queue()
    scout = ScoutAgent(mcp, queue)

    # Run for 2 cycles then stop
    task = asyncio.create_task(scout.start())
    await asyncio.sleep(POLL_INTERVAL_SEC * 2 + 5)
    await scout.stop()
    task.cancel()

    print(f"\n📦 {queue.qsize()} snapshots collected")
    while not queue.empty():
        snap: MarketSnapshot = await queue.get()
        print(f"  → {snap.to_llm_summary()}")

    await mcp.close()


if __name__ == "__main__":
    asyncio.run(main())
