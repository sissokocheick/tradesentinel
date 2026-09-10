"""
TradeSentinel — Main Orchestrator
Wires all three agents together and runs the full pipeline.
Start here. Entry point for both production and demo mode.

Usage:
    python orchestrator.py               # Normal mode
    python orchestrator.py --demo        # Demo mode (mock orders)
    python orchestrator.py --dashboard   # With live dashboard
"""

import asyncio
import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from mcp.binance_mcp_client import BinanceMCPClient
from agents.scout_agent import ScoutAgent
from agents.strategist_agent import StrategistAgent
from agents.executor_agent import ExecutorAgent
from skills.risk_manager import RiskManager
from wallet.agentic_wallet import AgenticWallet
from wallet.x402_payments import X402Client

# ── Logging setup ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-12s] %(levelname)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/tradesentinel.log", mode="a"),
    ],
)
log = logging.getLogger("orchestrator")

# Force UTF-8 output on Windows to handle unicode characters
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BANNER = """
 _____ ____  _    ____  _____ ____  _____ _   _ _____ ___ _   _ _____ _
|_   _|  _ \| |  |  _ \| ____/ ___|| ____| \ | |_   _|_ _| \ | | ____| |
  | | | |_) | |  | | | |  _| \___ \|  _| |  \| | | |  | ||  \| |  _| | |
  | | |  _ <| |__| |_| | |___ ___) | |___| |\  | | |  | || |\  | |___| |___
  |_| |_| \_\_____|____/|_____|____/|_____|_| \_| |_| |___|_| \_|_____|_____|

  Binance Agent OS Hackathon 2026 -- Multi-Agent Trading System
  Scout  ->  Strategist  ->  Executor
"""


class TradeSentinelOrchestrator:
    """
    Top-level coordinator for the 3-agent pipeline:
    Scout → Strategist → Executor

    Inter-agent communication via asyncio Queues (lock-free, fast).
    """

    def __init__(self, demo_mode: bool = False, testnet: bool = False):
        self.demo_mode  = demo_mode
        self.testnet    = testnet

        # Shared infrastructure
        self.mcp     = BinanceMCPClient(testnet=testnet)
        self.risk    = RiskManager()
        self.wallet  = AgenticWallet(self.mcp)
        self.x402    = X402Client()

        # Inter-agent queues
        self._scout_to_strategist: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._strategist_to_executor: asyncio.Queue = asyncio.Queue(maxsize=20)

        # Agents
        self.scout      = ScoutAgent(self.mcp, self._scout_to_strategist)
        self.strategist = StrategistAgent(self._scout_to_strategist, self._strategist_to_executor)
        self.executor   = ExecutorAgent(
            input_queue = self._strategist_to_executor,
            mcp         = self.mcp,
            risk        = self.risk,
            wallet      = self.wallet,
            x402        = self.x402,
            human_approval_callback = self._human_approval_cli,
        )

        if demo_mode:
            self._patch_demo_mode()

    async def run(self):
        print(BANNER)
        log.info(f"TradeSentinel starting | demo_mode={self.demo_mode}")
        log.info(f"Session start: {datetime.now(timezone.utc).isoformat()}")

        # Initialize risk manager with current balance
        balance = await self.wallet.get_total_usdc_value()
        self.risk.set_session_balance(balance)
        log.info(f"[WALLET] Agentic balance: ${balance:.2f} USDC")

        try:
            import json
            os.makedirs("logs", exist_ok=True)
            with open("logs/state.json", "w", encoding="utf-8") as f:
                json.dump({"balance_usdc": balance or 250.00, "status": "running"}, f)
        except Exception:
            pass

        # Print risk limits
        risk_summary = self.risk.get_risk_summary()
        log.info(f"[RISK]   Limits: {risk_summary['limits']}")

        # Launch all 3 agents concurrently
        try:
            await asyncio.gather(
                self.scout.start(),
                self.strategist.start(),
                self.executor.start(),
            )
        except asyncio.CancelledError:
            log.info("Shutdown signal received.")
        finally:
            await self._shutdown()

    async def _shutdown(self):
        log.info("Shutting down TradeSentinel...")
        await self.scout.stop()
        await self.strategist.stop()
        await self.executor.stop()
        await self.mcp.close()
        await self.x402.close()

        trades = self.executor.get_trades_today()
        log.info(f"Session complete | Trades executed: {len(trades)}")
        if trades:
            for t in trades:
                log.info(f"  [{t['id']}] {t['side']} {t['symbol']} @ ${t['entry_price']:,.2f}")

    async def _human_approval_cli(self, trade_id: str, intent, assessment) -> bool:
        """CLI-based human approval for large trades."""
        print(f"\n{'='*60}")
        print(f"[APPROVAL REQUIRED] Trade {trade_id}")
        print(f"   {intent.side} {intent.symbol}")
        print(f"   Size:  ~${assessment.approved_quantity * intent.entry_price:.2f} USDC")
        print(f"   Entry: ${intent.entry_price:,.4f}")
        print(f"   SL:    ${assessment.stop_loss_price:,.4f}")
        print(f"   TP:    ${assessment.take_profit_price:,.4f}")
        print(f"   Rationale: {intent.rationale}")
        print(f"{'='*60}")
        try:
            answer = input("Approve? [y/N]: ").strip().lower()
            return answer == "y"
        except EOFError:
            return False

    def _patch_demo_mode(self):
        """Override order placement with mock responses in demo mode."""
        from datetime import datetime, timezone

        async def mock_order(symbol, side, quantity, **kwargs):
            print(f"\n[DEMO] Would place: {side} {quantity:.6f} {symbol}")
            return {
                "orderId": "DEMO-12345",
                "symbol": symbol,
                "side": side,
                "quantity": str(quantity),
                "status": "FILLED",
                "executedQty": str(quantity),
                "ts": datetime.now(timezone.utc).isoformat(),
            }

        async def mock_total_balance():
            return 250.00

        async def mock_auto_approve(trade_id, intent, assessment):
            print(f"\n[DEMO AUTO-APPROVED] Guardrails passed -> Executing {intent.side} {intent.symbol} (~${intent.quantity_usdc:.2f} USDC)")
            return True

        self.mcp.place_market_order = mock_order
        self.mcp.place_limit_order  = mock_order
        self.wallet.get_total_usdc_value = mock_total_balance
        self.executor._human_cb = mock_auto_approve
        log.info("DEMO MODE - orders simulated, auto-approval active, balance: $250.00 USDC")


# ── CLI entry point ───────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="TradeSentinel — Binance Agent OS Hackathon")
    parser.add_argument("--demo",      action="store_true", help="Mock mode (no real orders)")
    parser.add_argument("--testnet",   action="store_true", help="Run on Binance Spot Testnet")
    parser.add_argument("--dashboard", action="store_true", help="Launch web dashboard")
    args = parser.parse_args()

    import os
    os.makedirs("logs", exist_ok=True)

    if args.dashboard:
        import subprocess, sys
        subprocess.Popen(
            [sys.executable, "dashboard/app.py"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        log.info("Dashboard started at http://localhost:8000")

    orchestrator = TradeSentinelOrchestrator(demo_mode=args.demo, testnet=args.testnet)

    try:
        await orchestrator.run()
    except KeyboardInterrupt:
        log.info("Interrupted by user.")


if __name__ == "__main__":
    asyncio.run(main())
