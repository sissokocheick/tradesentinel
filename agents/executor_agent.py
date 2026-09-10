"""
TradeSentinel — Executor Agent
The final gate before any order touches real money.
Applies risk checks, requests human approval when needed,
places orders on the Agentic sub-account, and writes an immutable audit trail.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mcp.binance_mcp_client import BinanceMCPClient, BinanceMCPError
from skills.risk_manager import RiskManager, TradeIntent, RiskDecision
from wallet.agentic_wallet import AgenticWallet
from wallet.x402_payments import X402Client

log = logging.getLogger("executor")

AUDIT_LOG_PATH = Path(os.getenv("AUDIT_LOG_PATH", "logs/audit_trail.json"))
AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


class ExecutorAgent:
    """
    Consumes TradeIntent from the Strategist queue.
    Pipeline:
        1. Risk assessment (RiskManager)
        2. [Optional] Human approval prompt
        3. Order placement via Binance MCP / Agentic Wallet
        4. Stop-loss / take-profit placement
        5. Audit log entry written
    """

    def __init__(
        self,
        input_queue: asyncio.Queue,
        mcp: BinanceMCPClient,
        risk: RiskManager,
        wallet: Optional["AgenticWallet"] = None,
        x402: Optional["X402Client"] = None,
        human_approval_callback=None,
    ):
        self.in_q     = input_queue
        self.mcp      = mcp
        self.risk     = risk
        self.wallet   = wallet
        self.x402     = x402
        self._human_cb = human_approval_callback   # async fn(intent) → bool
        self._running = False
        self._trades_today: list[dict] = []

    async def start(self):
        log.info("Executor started — monitoring for trade intents.")
        self._running = True
        while self._running:
            try:
                intent: TradeIntent = await asyncio.wait_for(self.in_q.get(), timeout=5.0)
                await self._process(intent)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error(f"Executor error: {e}", exc_info=True)

    async def stop(self):
        self._running = False
        log.info("Executor stopped.")

    # ── Trade pipeline ───────────────────────────────────────

    async def _process(self, intent: TradeIntent):
        trade_id = str(uuid.uuid4())[:8]
        log.info(
            f"[{trade_id}] Intent received: {intent.side} {intent.symbol} "
            f"~${intent.quantity_usdc:.2f} | confidence={intent.confidence:.0f}% | "
            f"strategy={intent.strategy_name}"
        )

        # ── Step 1: Risk assessment ──────────────────────────
        assessment = self.risk.assess(intent)
        assessment_dict = {**assessment.__dict__, "decision": assessment.decision.value}
        self._audit(trade_id, "risk_assessment", intent, assessment=assessment_dict)

        if assessment.decision == RiskDecision.REJECTED:
            log.warning(f"[{trade_id}] ❌ REJECTED — {assessment.reason}")
            self._audit(trade_id, "rejected", intent, reason=assessment.reason)
            return

        # ── Step 2: Human approval (if required) ─────────────
        if assessment.requires_human:
            log.info(f"[{trade_id}] 🔔 AWAITING HUMAN APPROVAL — {assessment.reason}")
            approved = await self._request_human_approval(trade_id, intent, assessment)
            if not approved:
                log.info(f"[{trade_id}] ❌ Human rejected the trade.")
                self._audit(trade_id, "human_rejected", intent)
                return
            log.info(f"[{trade_id}] ✅ Human approved.")

        # ── Step 3: Pay for premium data via x402 (optional) ─
        if self.x402:
            try:
                await self.x402.pay_for_trade_data(intent.symbol)
            except Exception as e:
                log.warning(f"[{trade_id}] x402 payment skipped: {e}")

        # ── Step 4: Place order ──────────────────────────────
        quantity = assessment.approved_quantity
        if not quantity or quantity <= 0:
            log.error(f"[{trade_id}] Zero quantity — aborting.")
            return

        qty_dec, _ = self._get_precision(intent.symbol)
        quantity = round(quantity, qty_dec)
        if qty_dec == 0: quantity = int(quantity)

        try:
            order_result = await self.mcp.place_market_order(
                symbol           = intent.symbol,
                side             = intent.side,
                quantity         = quantity,
                client_order_id  = f"ts_{trade_id}",
            )
            log.info(f"[{trade_id}] ✅ ORDER PLACED — {order_result}")
            self._audit(trade_id, "order_placed", intent, order=order_result, assessment=assessment_dict)

            # Update risk state
            self.risk.record_trade(intent.symbol, intent.side, intent.entry_price, quantity)
            self._trades_today.append({
                "id": trade_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": quantity,
                "entry_price": intent.entry_price,
                "strategy": intent.strategy_name,
                "ts": datetime.now(timezone.utc).isoformat(),
            })

        except BinanceMCPError as e:
            log.error(f"[{trade_id}] ❌ Order failed: {e}")
            self._audit(trade_id, "order_failed", intent, error=str(e))
            return

        # ── Step 5: Place stop-loss & take-profit ────────────
        if assessment.stop_loss_price:
            await self._place_stop_limit(trade_id, intent, assessment, quantity)

    def _get_precision(self, symbol: str) -> tuple[int, int]:
        """Returns (qty_decimals, price_decimals) for formatting."""
        if symbol == "BTCUSDT": return 5, 2
        if symbol == "ETHUSDT": return 4, 2
        if symbol == "BNBUSDT": return 3, 2
        if symbol == "SOLUSDT": return 2, 3
        if symbol == "XRPUSDT": return 0, 4
        return 2, 2

    async def _place_stop_limit(self, trade_id, intent, assessment, quantity):
        """Place a limit stop-loss order to protect the position."""
        sl_side = "SELL" if intent.side == "BUY" else "BUY"
        qty_dec, price_dec = self._get_precision(intent.symbol)
        
        sl_price = round(assessment.stop_loss_price, price_dec)
        
        try:
            sl_order = await self.mcp.place_limit_order(
                symbol      = intent.symbol,
                side        = sl_side,
                quantity    = quantity, # Already rounded during market order
                price       = sl_price,
                time_in_force = "GTC",
            )
            log.info(f"[{trade_id}] 🛡️  Stop-loss set at ${sl_price}")
            self._audit(trade_id, "stop_loss_set", intent, sl_order=sl_order)
        except Exception as e:
            log.warning(f"[{trade_id}] Stop-loss placement failed: {e}")

    # ── Human approval flow ──────────────────────────────────

    async def _request_human_approval(self, trade_id: str, intent: TradeIntent, assessment) -> bool:
        if self._human_cb:
            return await self._human_cb(trade_id, intent, assessment)
        # Default: auto-approve if no callback (for automated testing)
        log.warning(f"[{trade_id}] No human callback registered — auto-approving.")
        return True

    # ── Audit trail ──────────────────────────────────────────

    def _audit(self, trade_id: str, event: str, intent: TradeIntent, **extra):
        entry = {
            "trade_id"   : trade_id,
            "event"      : event,
            "ts"         : datetime.now(timezone.utc).isoformat(),
            "symbol"     : intent.symbol,
            "side"       : intent.side,
            "entry_price": intent.entry_price,
            "quantity_usdc": intent.quantity_usdc,
            "confidence" : intent.confidence,
            "strategy"   : intent.strategy_name,
            "rationale"  : intent.rationale,
            **extra,
        }
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def get_trades_today(self) -> list[dict]:
        return list(self._trades_today)

    def get_pnl_summary(self) -> dict:
        """Basic P&L summary for dashboard display."""
        return {
            "trades_count": len(self._trades_today),
            "risk_summary": self.risk.get_risk_summary(),
        }
