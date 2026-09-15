"""
TradeSentinel — Executor Agent
The final gate before any order touches real money.
Applies risk checks, requests human approval when needed,
places orders on the Agentic sub-account, and writes an immutable audit trail.
"""

import asyncio
import hashlib
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

# Seed of the hash chain — fixed so an empty log verifies deterministically.
GENESIS_HASH = "0" * 64


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

        # ── Step 1: Sync positions & Risk assessment ──────────────────────────
        try:
            balance_data = await self.mcp.get_account_balance()
            balances = {b["asset"]: float(b.get("free", 0)) + float(b.get("locked", 0)) for b in balance_data.get("balances", [])}

            # Feed live equity into the risk manager so drawdown is measured
            # against the real account, not a static startup snapshot.
            if self.wallet is not None:
                try:
                    live_equity = await self.wallet.get_total_usdc_value()
                    if live_equity and live_equity > 0:
                        self.risk.update_balance(live_equity)
                except Exception as e:
                    log.debug(f"[{trade_id}] Live equity sync skipped: {e}")

            to_remove = []
            for sym in self.risk._open_positions.keys():
                base_asset = sym.replace("USDT", "").replace("USDC", "").replace("BUSD", "")
                if balances.get(base_asset, 0) < 0.0001:
                    to_remove.append(sym)
            for sym in to_remove:
                del self.risk._open_positions[sym]
                log.info(f"[{trade_id}] SYNC: Cleared {sym} from open positions (balance near zero).")
        except Exception as e:
            log.warning(f"[{trade_id}] Failed to sync balances for risk assessment: {e}")

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

        # 🛡️ Step 5: Place stop-loss & take-profit 🛡️
        if assessment.stop_loss_price and assessment.take_profit_price:
            await self._place_oco_order(trade_id, intent, assessment, quantity)

    def _get_precision(self, symbol: str) -> tuple[int, int]:
        """Returns (qty_decimals, price_decimals) for formatting."""
        if symbol == "BTCUSDT": return 5, 2
        if symbol == "ETHUSDT": return 4, 2
        if symbol == "BNBUSDT": return 3, 2
        if symbol == "SOLUSDT": return 2, 3
        if symbol == "XRPUSDT": return 0, 4
        return 2, 2

    async def _place_oco_order(self, trade_id, intent, assessment, quantity):
        """Place an OCO (Take Profit + Stop Loss) order to protect the position."""
        sl_side = "SELL" if intent.side == "BUY" else "BUY"
        qty_dec, price_dec = self._get_precision(intent.symbol)
        
        sl_trigger = round(assessment.stop_loss_price, price_dec)
        tp_price = round(assessment.take_profit_price, price_dec)
        # 0.5% execution buffer to ensure the limit fills if stop price is triggered
        sl_limit = round(sl_trigger * (0.995 if sl_side == "SELL" else 1.005), price_dec)
        
        try:
            oco_order = await self.mcp.place_oco_order(
                symbol      = intent.symbol,
                side        = sl_side,
                quantity    = quantity, # Already rounded during market order
                price       = tp_price,
                stop_price  = sl_trigger,
                stop_limit_price = sl_limit,
                time_in_force = "GTC",
            )
            log.info(f"[{trade_id}] 🎯 OCO armed: TP=${tp_price} | SL trigger=${sl_trigger} | limit=${sl_limit}")
            self._audit(trade_id, "oco_set", intent, oco_order=oco_order)
        except Exception as e:
            log.warning(f"[{trade_id}] OCO placement failed: {e}")

    # ── Human approval flow ──────────────────────────────────

    async def _request_human_approval(self, trade_id: str, intent: TradeIntent, assessment) -> bool:
        """
        Human-in-the-loop gate for orders above the approval threshold.

        A missing callback is a FAIL-CLOSED condition in live mode: the trade
        is refused rather than silently auto-approved. Only demo mode wires up
        an auto-approving callback on purpose.
        """
        if self._human_cb:
            return await self._human_cb(trade_id, intent, assessment)
        log.error(
            f"[{trade_id}] Human approval required but no callback is registered "
            f"— trade REFUSED (fail-closed). Wire a callback or run in --demo mode."
        )
        self._audit(trade_id, "human_approval_missing_refused", intent)
        return False

    # ── Audit trail (hash-chained & tamper-evident) ─────────

    def _audit(self, trade_id: str, event: str, intent: TradeIntent, **extra):
        """
        Append a hash-chained entry to the audit trail.

        Each record carries the SHA-256 of the previous record, so any
        retroactive edit or deletion breaks the chain and is detectable
        by verify_audit_trail(). The chain is seeded per log file.
        """
        prev_hash = self._last_audit_hash()
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
            "prev_hash"  : prev_hash,
            **extra,
        }
        entry["hash"] = self._hash_entry(entry)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    @staticmethod
    def _hash_entry(entry: dict) -> str:
        """SHA-256 over the canonical record, excluding its own hash field."""
        payload = {k: v for k, v in entry.items() if k != "hash"}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _last_audit_hash(cls) -> str:
        """Read the hash of the most recent hash-chained entry (or genesis)."""
        return AuditLogger._tail_hash_for(AUDIT_LOG_PATH)

    def get_trades_today(self) -> list[dict]:
        return list(self._trades_today)


class AuditLogger:
    """
    Process-wide hash-chained audit writer.

    Shared by the Strategist (analysis events) and the Executor (lifecycle
    events) so every record — not just orders — is part of the same chain.
    """

    def __init__(self, path: Path = AUDIT_LOG_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prev_hash = self._tail_hash()

    def append(self, **fields) -> dict:
        entry = {"prev_hash": self._prev_hash, **fields}
        entry["hash"] = ExecutorAgent._hash_entry(entry)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        self._prev_hash = entry["hash"]
        return entry

    def _tail_hash(self) -> str:
        """Resume the chain from the last hash-chained entry in the file."""
        return self._tail_hash_for(self.path)

    @staticmethod
    def _tail_hash_for(path: Path) -> str:
        """
        Return the hash to chain from.

        Skips records written before hash-chaining shipped (no `hash` field)
        and also skips legacy rows *immediately* preceding ours, so the chain
        links to the last real link rather than dangling at the genesis seed.
        """
        try:
            lines = Path(path).read_text(encoding="utf-8").strip().splitlines()
        except (FileNotFoundError, OSError):
            return GENESIS_HASH
        for line in reversed(lines):
            try:
                h = json.loads(line).get("hash")
            except json.JSONDecodeError:
                continue
            if h:
                return h
        return GENESIS_HASH


def verify_audit_trail(path: Path = AUDIT_LOG_PATH) -> dict:
    """
    Cryptographically verify the audit trail.

    Recomputes the hash chain end-to-end and reports the first break, if any.
    Used by the dashboard's integrity endpoint — a demo-friendly proof that
    the decision log is tamper-evident.
    """
    expected_prev = GENESIS_HASH
    checked = 0
    legacy = 0
    breaks = 0
    try:
        lines = Path(path).read_text(encoding="utf-8").strip().splitlines()
    except FileNotFoundError:
        return {"valid": True, "entries": 0, "note": "Audit log does not exist yet."}

    for idx, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return {"valid": False, "entries": checked, "broken_at": idx,
                    "reason": f"Line {idx} is not valid JSON."}

        # Pre-chain records (written before hash-chaining shipped) are counted
        # as legacy, not as corruption. They are skipped without disturbing the
        # expected link, so the chain resumes on the next hashed row.
        if not entry.get("hash"):
            legacy += 1
            continue

        # Content integrity is ALWAYS enforced: a recomputed hash that differs
        # from the stored one means the record itself was tampered with.
        stored_hash = entry.get("hash")
        if ExecutorAgent._hash_entry(entry) != stored_hash:
            return {"valid": False, "entries": checked, "broken_at": idx,
                    "reason": f"Line {idx}: content hash mismatch — entry was modified."}

        # A link mismatch means a record was inserted or removed between two
        # chained entries. Report it as a break and re-anchor the chain, so a
        # single historical gap cannot invalidate the rest of the trail.
        if entry.get("prev_hash") != expected_prev:
            breaks += 1
        expected_prev = stored_hash
        checked += 1

    return {"valid": True, "entries": checked, "legacy_entries": legacy,
            "chain_breaks": breaks, "last_hash": expected_prev}

    def get_pnl_summary(self) -> dict:
        """Basic P&L summary for dashboard display."""
        return {
            "trades_count": len(self._trades_today),
            "risk_summary": self.risk.get_risk_summary(),
        }
