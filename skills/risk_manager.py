"""
TradeSentinel — Risk Manager
Enforces all guardrails BEFORE any order reaches the Executor.
All limits are configurable via environment variables.
"""

import os
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

log = logging.getLogger("risk_manager")


# ── Risk profile (env-configurable) ──────────────────────────
MAX_POSITION_SIZE_USDC    = float(os.getenv("RISK_MAX_POSITION_USDC",    "50"))
MAX_DRAWDOWN_PCT          = float(os.getenv("RISK_MAX_DRAWDOWN_PCT",     "5"))
MAX_TRADES_PER_HOUR       = int(os.getenv("RISK_MAX_TRADES_PER_HOUR",    "10"))
REQUIRE_APPROVAL_ABOVE    = float(os.getenv("RISK_APPROVAL_THRESHOLD",   "20"))
MIN_CONFIDENCE_TO_TRADE   = float(os.getenv("RISK_MIN_CONFIDENCE",       "65"))
ALLOWED_SYMBOLS           = set(os.getenv("RISK_ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT").split(","))
MAX_OPEN_POSITIONS        = int(os.getenv("RISK_MAX_OPEN_POSITIONS",     "3"))
STOP_LOSS_PCT             = float(os.getenv("RISK_STOP_LOSS_PCT",        "2"))
TAKE_PROFIT_PCT           = float(os.getenv("RISK_TAKE_PROFIT_PCT",      "4"))


class RiskDecision(Enum):
    APPROVED          = "approved"
    REJECTED          = "rejected"
    NEEDS_APPROVAL    = "needs_human_approval"


@dataclass
class RiskAssessment:
    decision: RiskDecision
    reason: str
    approved_quantity: Optional[float] = None   # May be reduced
    stop_loss_price: Optional[float]  = None
    take_profit_price: Optional[float] = None
    requires_human: bool = False

    @property
    def is_approved(self) -> bool:
        return self.decision in (RiskDecision.APPROVED, RiskDecision.NEEDS_APPROVAL)


@dataclass
class TradeIntent:
    """Proposed trade from the Strategist."""
    symbol: str
    side: str              # "BUY" | "SELL"
    quantity_usdc: float   # Value in USDC
    entry_price: float
    confidence: float      # 0–100
    strategy_name: str
    rationale: str


class RiskManager:
    """
    Stateful risk manager that tracks open positions and hourly trade count.
    All checks run synchronously before order submission.
    """

    def __init__(self):
        self._hourly_trade_count = 0
        self._hour_bucket = self._current_hour()
        self._open_positions: dict[str, dict] = {}  # symbol → position info
        self._session_start_balance: Optional[float] = None
        self._peak_balance: Optional[float] = None

    def set_session_balance(self, balance_usdc: float):
        """Call once at startup with the Agentic account balance."""
        self._session_start_balance = balance_usdc
        self._peak_balance = balance_usdc
        log.info(f"Risk Manager initialized | Session balance: ${balance_usdc:.2f} USDC")

    def update_balance(self, current_balance: float):
        if self._peak_balance is None or current_balance > self._peak_balance:
            self._peak_balance = current_balance

    def assess(self, intent: TradeIntent) -> RiskAssessment:
        """Run all risk checks. Returns an assessment with the final decision."""
        self._refresh_hour_bucket()

        # 1. Symbol whitelist
        if intent.symbol not in ALLOWED_SYMBOLS:
            return RiskAssessment(
                decision=RiskDecision.REJECTED,
                reason=f"Symbol {intent.symbol} is not in the allowed list."
            )

        # 2. Confidence threshold
        if intent.confidence < MIN_CONFIDENCE_TO_TRADE:
            return RiskAssessment(
                decision=RiskDecision.REJECTED,
                reason=f"Confidence {intent.confidence:.0f}% below minimum {MIN_CONFIDENCE_TO_TRADE:.0f}%."
            )

        # 3. Hourly trade count
        if self._hourly_trade_count >= MAX_TRADES_PER_HOUR:
            return RiskAssessment(
                decision=RiskDecision.REJECTED,
                reason=f"Hourly trade limit reached ({MAX_TRADES_PER_HOUR})."
            )

        # 4. Max open positions
        if intent.side == "BUY" and len(self._open_positions) >= MAX_OPEN_POSITIONS:
            return RiskAssessment(
                decision=RiskDecision.REJECTED,
                reason=f"Max open positions ({MAX_OPEN_POSITIONS}) already held."
            )

        # 5. Max position size
        approved_qty_usdc = min(intent.quantity_usdc, MAX_POSITION_SIZE_USDC)
        if approved_qty_usdc < intent.quantity_usdc:
            log.warning(f"Position size reduced from ${intent.quantity_usdc:.2f} → ${approved_qty_usdc:.2f}")

        # 6. Drawdown check
        if self._peak_balance is not None:
            drawdown = ((self._peak_balance - self._peak_balance) / self._peak_balance) * 100
            if drawdown >= MAX_DRAWDOWN_PCT:
                return RiskAssessment(
                    decision=RiskDecision.REJECTED,
                    reason=f"Max drawdown {MAX_DRAWDOWN_PCT}% reached. Trading halted."
                )

        # 7. Compute stop-loss & take-profit
        sl_price = tp_price = None
        if intent.entry_price > 0:
            if intent.side == "BUY":
                sl_price = round(intent.entry_price * (1 - STOP_LOSS_PCT / 100), 6)
                tp_price = round(intent.entry_price * (1 + TAKE_PROFIT_PCT / 100), 6)
            else:
                sl_price = round(intent.entry_price * (1 + STOP_LOSS_PCT / 100), 6)
                tp_price = round(intent.entry_price * (1 - TAKE_PROFIT_PCT / 100), 6)

        # 8. Human approval for large orders
        if approved_qty_usdc > REQUIRE_APPROVAL_ABOVE:
            return RiskAssessment(
                decision=RiskDecision.NEEDS_APPROVAL,
                reason=f"Order >${REQUIRE_APPROVAL_ABOVE} USDC requires human confirmation.",
                approved_quantity=approved_qty_usdc / intent.entry_price if intent.entry_price else None,
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
                requires_human=True,
            )

        approved_qty = approved_qty_usdc / intent.entry_price if intent.entry_price else 0

        return RiskAssessment(
            decision=RiskDecision.APPROVED,
            reason="All risk checks passed.",
            approved_quantity=approved_qty,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
        )

    def record_trade(self, symbol: str, side: str, entry_price: float, quantity: float):
        """Call after a successful order to update internal state."""
        self._hourly_trade_count += 1
        if side == "BUY":
            self._open_positions[symbol] = {
                "entry_price": entry_price,
                "quantity": quantity,
                "side": side,
            }
        elif side == "SELL" and symbol in self._open_positions:
            del self._open_positions[symbol]

    def get_risk_summary(self) -> dict:
        return {
            "hourly_trades": self._hourly_trade_count,
            "max_hourly_trades": MAX_TRADES_PER_HOUR,
            "open_positions": len(self._open_positions),
            "max_open_positions": MAX_OPEN_POSITIONS,
            "session_start_balance": self._session_start_balance,
            "peak_balance": self._peak_balance,
            "limits": {
                "max_position_usdc": MAX_POSITION_SIZE_USDC,
                "max_drawdown_pct": MAX_DRAWDOWN_PCT,
                "stop_loss_pct": STOP_LOSS_PCT,
                "take_profit_pct": TAKE_PROFIT_PCT,
                "min_confidence": MIN_CONFIDENCE_TO_TRADE,
            }
        }

    def _refresh_hour_bucket(self):
        current = self._current_hour()
        if current != self._hour_bucket:
            self._hourly_trade_count = 0
            self._hour_bucket = current

    @staticmethod
    def _current_hour() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H")
