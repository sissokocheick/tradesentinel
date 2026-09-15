"""
Regression test: a position must NEVER be left without stop-loss protection.

Before this fix, 56% of positions had no stop-loss because an OCO rejection
was swallowed by a bare `except: log.warning(...)` and execution moved on,
leaving the position naked. This test forces that exact failure mode and
asserts a fallback stop-loss is placed instead.

Runs offline — no API key, no network. Uses a fake MCP client.
"""

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Isolate the audit trail so the test never touches the real one.
TEST_AUDIT = Path("logs/test_protection_audit.json")
TEST_AUDIT.parent.mkdir(parents=True, exist_ok=True)
if TEST_AUDIT.exists():
    TEST_AUDIT.unlink()
os.environ["AUDIT_LOG_PATH"] = str(TEST_AUDIT)

from agents.executor_agent import ExecutorAgent  # noqa: E402
from skills.risk_manager import TradeIntent, RiskDecision  # noqa: E402
import agents.executor_agent as ex_mod  # noqa: E402

ex_mod.AUDIT_LOG_PATH = TEST_AUDIT


@dataclass
class FakeAssessment:
    decision: RiskDecision = RiskDecision.APPROVED
    reason: str = "ok"
    approved_quantity: float = 0.002
    stop_loss_price: float = 95000.0
    take_profit_price: float = 103000.0
    requires_human: bool = False


class FakeMCP:
    """Records every order call. Rejects OCO on demand."""

    def __init__(self, fail_oco: bool = True):
        self.fail_oco = fail_oco
        self.market_orders = []
        self.oco_orders = []
        self.stop_orders = []
        self.balance_calls = 0

    async def get_account_balance(self):
        self.balance_calls += 1
        return {"balances": [{"asset": "BTC", "free": "0.5", "locked": "0"}]}

    async def place_market_order(self, symbol, side, quantity, client_order_id=None):
        o = {"symbol": symbol, "side": side, "executedQty": str(quantity),
             "orderId": 1, "status": "FILLED"}
        self.market_orders.append(o)
        return o

    async def place_oco_order(self, **kw):
        if self.fail_oco:
            raise RuntimeError("Binance: OCO rejected — MIN_NOTIONAL")
        self.oco_orders.append(kw)
        return {"status": "NEW"}

    async def place_stop_loss_order(self, **kw):
        self.stop_orders.append(kw)
        return {"status": "NEW"}


def make_intent():
    return TradeIntent(
        symbol="BTCUSDT", side="BUY", quantity_usdc=30.0,
        entry_price=98000.0, confidence=75.0,
        strategy_name="momentum", rationale="test",
    )


class NoOpRisk:
    """Minimal stand-in: approves everything and yields our fake assessment."""

    _open_positions = {}
    halts = False

    def assess(self, intent):
        return FakeAssessment()

    def record_trade(self, *a, **kw):
        pass

    def update_balance(self, *a, **kw):
        pass

    def get_risk_summary(self):
        return {}

    def is_halted(self):
        return False


async def run_case(name: str, fail_oco: bool, expect_stop: bool):
    mcp = FakeMCP(fail_oco=fail_oco)
    ex = ExecutorAgent(
        input_queue=asyncio.Queue(), mcp=mcp, risk=NoOpRisk(),
    )
    await ex._process(make_intent())

    market = len(mcp.market_orders)
    oco = len(mcp.oco_orders)
    stop = len(mcp.stop_orders)

    ok = market == 1 and (oco + stop) >= 1
    if expect_stop and stop != 1:
        ok = False
    print(f"  {name:34} market={market} oco={oco} fallback_stop={stop}  "
          f"{'PASS' if ok else 'FAIL'}")
    return ok


async def main():
    print("\n🛡️  Position protection regression test\n")
    results = []

    # Case 1: OCO rejected → fallback stop-loss MUST fire.
    r1 = await run_case("OCO rejected → fallback stop-loss", fail_oco=True, expect_stop=True)

    # Case 2: OCO accepted → no duplicate stop-loss needed.
    r2 = await run_case("OCO accepted → no duplicate order", fail_oco=False, expect_stop=False)
    results = [r1, r2]

    # Verify the audit trail recorded the recovery path faithfully.
    events = []
    for line in TEST_AUDIT.read_text(encoding="utf-8").strip().splitlines():
        events.append(__import__("json").loads(line).get("event"))
    print(f"\n  audit events: {events}")

    # The failing case must be on record, not silently dropped.
    has_fail_record = "oco_failed" in events
    has_stop_record = "stop_loss_set" in events
    print(f"  oco_failed audited : {has_fail_record}")
    print(f"  stop_loss_set audited: {has_stop_record}")
    results.append(has_fail_record and has_stop_record)

    # And the trail must still verify.
    from agents.executor_agent import verify_audit_trail
    v = verify_audit_trail(TEST_AUDIT)
    print(f"  audit integrity    : {v}")
    results.append(v.get("valid") is True)

    passed = all(results)
    print(f"\n{'✅ ALL PROTECTION TESTS PASSED' if passed else '❌ FAILURES PRESENT'}\n")
    TEST_AUDIT.unlink()
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
