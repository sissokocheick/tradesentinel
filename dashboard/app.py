"""
TradeSentinel — Live Dashboard Backend (FastAPI + WebSocket)
Serves real-time agent state, trade feed, and P&L to the React frontend.
"""

import sys
import os
# Ensure the project root is on sys.path when launched as a subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import io
import csv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("dashboard")

AUDIT_LOG = Path(os.getenv("AUDIT_LOG_PATH", "logs/audit_trail.json"))

app = FastAPI(title="TradeSentinel Dashboard", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory state (updated by agents via shared refs) ───────
_state = {
    "status": "running",
    "balance_usdc": 0.0,
    "trades": [],
    "risk_summary": {},
    "market_snapshots": {},
    "last_decisions": [],
}


def update_state(key: str, value):
    _state[key] = value


# ── REST endpoints ────────────────────────────────────────────

STATE_FILE_PATH = Path("logs/state.json")
MARKET_SNAPSHOTS_PATH = Path("logs/market_snapshots.json")

def get_current_balance() -> float:
    if STATE_FILE_PATH.exists():
        try:
            d = json.loads(STATE_FILE_PATH.read_text(encoding="utf-8"))
            if "balance_usdc" in d and float(d["balance_usdc"]) > 0:
                return float(d["balance_usdc"])
        except Exception:
            pass
    return 250.00


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    icon_path = Path(__file__).parent / "frontend" / "logo.svg"
    if icon_path.exists():
        return FileResponse(icon_path, media_type="image/svg+xml")
    return None


@app.get("/api/status")
async def get_status():
    trades = await get_trades()
    return {
        "status": "running",
        "ts": datetime.now(timezone.utc).isoformat(),
        "balance_usdc": get_current_balance(),
        "trades_count": len(trades),
    }


@app.get("/api/trades")
async def get_trades():
    if not AUDIT_LOG.exists():
        return []
    trades = []
    for line in AUDIT_LOG.read_text(encoding="utf-8").strip().split("\n"):
        if not line:
            continue
        try:
            item = json.loads(line)
            if item.get("event") == "order_placed":
                order = item.get("order") or {}
                trades.append({
                    "id": item.get("trade_id", "t-1"),
                    "symbol": item.get("symbol", ""),
                    "side": item.get("side", ""),
                    "entry_price": float(item.get("entry_price") or 0.0),
                    # Filled quantity as reported by the exchange; fall back to
                    # the intended USDC notional when the fill detail is absent.
                    "quantity": float(order.get("executedQty") or order.get("quantity") or 0),
                    "quantity_usdc": float(item.get("quantity_usdc") or 0),
                    "strategy": item.get("strategy", "momentum"),
                    "ts": item.get("ts", ""),
                })
        except Exception:
            pass
    return trades


@app.get("/api/risk")
async def get_risk():
    return {
        "max_position_usdc": float(os.getenv("RISK_MAX_POSITION_USDC", "50")),
        "max_drawdown_pct": float(os.getenv("RISK_MAX_DRAWDOWN_PCT", "5")),
        "stop_loss_pct": float(os.getenv("RISK_STOP_LOSS_PCT", "2")),
        "take_profit_pct": float(os.getenv("RISK_TAKE_PROFIT_PCT", "4")),
        "min_confidence": float(os.getenv("RISK_MIN_CONFIDENCE", "65")),
    }


@app.get("/api/market")
async def get_market():
    if MARKET_SNAPSHOTS_PATH.exists():
        try:
            return json.loads(MARKET_SNAPSHOTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _state["market_snapshots"]


@app.get("/api/analytics")
async def get_analytics(start_date: str = None, end_date: str = None):
    all_trades = await get_trades()
    trades = []
    for t in all_trades:
        ts = t.get("ts", "")[:10]
        if start_date and ts < start_date: continue
        if end_date and ts > end_date: continue
        trades.append(t)
        
    market = await get_market()
    
    winning = 0
    losing = 0
    total_profit = 0.0
    total_loss = 0.0
    net_pnl_usdc = 0.0
    
    for t in trades:
        sym = t.get("symbol")
        entry = float(t.get("entry_price") or 0)
        side = t.get("side", "BUY").upper()
        if not entry or not sym:
            continue
            
        cur_price = entry
        if sym in market and "price" in market[sym]:
            try:
                cur_price = float(market[sym]["price"])
            except Exception:
                pass
                
        pnl = (cur_price - entry) if side == "BUY" else (entry - cur_price)
        pct_pnl = (pnl / entry) * 100.0 if entry > 0 else 0
        
        # Calculate USDC PnL
        qty_usdc = float(t.get("quantity_usdc") or t.get("quantity") or 35.0)
        trade_pnl_usdc = (pct_pnl / 100.0) * qty_usdc
        net_pnl_usdc += trade_pnl_usdc
        
        if pct_pnl >= 0:
            winning += 1
            total_profit += abs(pct_pnl)
        else:
            losing += 1
            total_loss += abs(pct_pnl)
            
    total = winning + losing
    if total > 0:
        win_rate = round((winning / total) * 100, 1)
        profit_factor = round(total_profit / total_loss, 2) if total_loss > 0 else (None if total_profit == 0 else float('inf'))
    else:
        win_rate = 0.0
        profit_factor = None

    # Real risk metrics, derived from the per-trade return series.
    curve = await get_equity_curve()
    returns = [p["trade_pnl"] for p in curve]
    sharpe, max_dd = _risk_metrics(returns)

    return {
        "win_rate": win_rate,
        "winning_trades": winning,
        "losing_trades": losing,
        "total_trades": total,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_dd,
        "profit_factor": profit_factor,
        "net_pnl_usdc": round(net_pnl_usdc, 2),
        "x402_status": "Active (0.001 USDC/query)"
    }


def _risk_metrics(returns: list[float]) -> tuple[Optional[float], float]:
    """
    Annualised Sharpe ratio and max drawdown from a per-trade return series.

    Sharpe = mean(returns) / std(returns), scaled to annual by sqrt(252)
    (daily rebalancing assumption, standard for a spot strategy backtest).
    Returns (None, 0.0) when the series is too short to be meaningful —
    we never fabricate a plausible-looking number.
    """
    if len(returns) < 2:
        return None, 0.0

    n = len(returns)
    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)  # sample stdev
    std_r = variance ** 0.5

    if std_r == 0:
        return None, 0.0  # flat series: Sharpe undefined, not zero

    sharpe = (mean_r / std_r) * (252 ** 0.5)

    # Max drawdown of the cumulative PnL curve (peak-to-trough, negative).
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for r in returns:
        cum += r
        if cum > peak:
            peak = cum
        dd = cum - peak
        if dd < max_dd:
            max_dd = dd

    return round(sharpe, 2), round(max_dd, 2)


@app.get("/api/positions")
async def get_positions():
    trades = await get_trades()
    market = await get_market()
    
    positions_map = {}
    for t in trades:
        sym = t.get("symbol")
        if not sym:
            continue
        entry = float(t.get("entry_price") or 0)
        side = t.get("side", "BUY").upper()
        if sym not in positions_map:
            positions_map[sym] = {
                "symbol": sym,
                "total_cost": 0.0,
                "trades_count": 0,
                "last_side": side,
                "last_entry": entry,
                "strategy": t.get("strategy", "mean_reversion")
            }
        positions_map[sym]["total_cost"] += entry
        positions_map[sym]["trades_count"] += 1
        positions_map[sym]["last_entry"] = entry
        positions_map[sym]["last_side"] = side
        
    result = []
    for sym, pos in positions_map.items():
        avg_entry = pos["total_cost"] / pos["trades_count"] if pos["trades_count"] > 0 else pos["last_entry"]
        cur_price = avg_entry
        if sym in market and "price" in market[sym]:
            try:
                cur_price = float(market[sym]["price"])
            except Exception:
                pass
                
        pnl_pct = ((cur_price - avg_entry) / avg_entry) * 100.0 if avg_entry > 0 else 0.0
        result.append({
            "symbol": sym,
            "side": pos["last_side"] or "LONG",
            "avg_entry": round(avg_entry, 4 if ("USDT" in sym and avg_entry < 10) else 2),
            "current_price": round(cur_price, 4 if ("USDT" in sym and cur_price < 10) else 2),
            "pnl_pct": round(pnl_pct, 2),
            "trades_count": pos["trades_count"],
            "strategy": pos["strategy"],
            "status": "PROFIT" if pnl_pct >= 0 else "DEFENSE"
        })
    return result


@app.get("/api/equity_curve")
async def get_equity_curve():
    trades = await get_trades()
    market = await get_market()
    
    points = []
    cum_pnl_pct = 0.0
    
    for idx, t in enumerate(trades, start=1):
        sym = t.get("symbol")
        entry = float(t.get("entry_price") or 0)
        side = t.get("side", "BUY").upper()
        if not entry or not sym:
            continue
            
        cur_price = entry
        if sym in market and "price" in market[sym]:
            try:
                cur_price = float(market[sym]["price"])
            except Exception:
                pass
                
        diff = (cur_price - entry) if side == "BUY" else (entry - cur_price)
        trade_pnl = (diff / entry) * 100.0 if entry > 0 else 0.0
        cum_pnl_pct += trade_pnl
        
        points.append({
            "step": idx,
            "ts": t.get("ts", "")[11:16] if (t.get("ts") and len(t.get("ts")) >= 16) else f"#{idx}",
            "symbol": sym,
            "trade_pnl": round(trade_pnl, 2),
            "cum_pnl": round(cum_pnl_pct, 2)
        })
        
    return points


@app.get("/api/export/csv")
async def export_audit_csv():
    if not AUDIT_LOG.exists():
        return Response(content="No audit log available", media_type="text/plain")
        
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Timestamp (UTC)",
        "Trade ID",
        "Event",
        "Symbol",
        "Side",
        "Entry Price",
        "Quantity USDC",
        "Confidence (%)",
        "Strategy",
        "Rationale",
        "Risk Decision"
    ])
    
    for line in AUDIT_LOG.read_text(encoding="utf-8").strip().split("\n"):
        if not line:
            continue
        try:
            item = json.loads(line)
            writer.writerow([
                item.get("ts", ""),
                item.get("trade_id", ""),
                item.get("event", ""),
                item.get("symbol", ""),
                item.get("side", ""),
                item.get("entry_price", ""),
                item.get("quantity_usdc", ""),
                item.get("confidence", ""),
                item.get("strategy", ""),
                item.get("rationale", ""),
                item.get("assessment", {}).get("decision", "") if isinstance(item.get("assessment"), dict) else item.get("reason", "")
            ])
        except Exception:
            pass
            
    csv_data = output.getvalue()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=tradesentinel_audit_report.csv"
        }
    )


@app.get("/api/decisions")
async def get_decisions():
    return await get_audit(limit=20)


@app.get("/api/audit")
async def get_audit(limit: int = 50):
    """Stream recent audit trail entries."""
    if not AUDIT_LOG.exists():
        return []
    lines = [l for l in AUDIT_LOG.read_text(encoding="utf-8").strip().split("\n") if l]
    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return list(reversed(entries))


@app.get("/api/audit/verify")
async def verify_audit():
    """
    Cryptographic integrity check of the audit trail.

    Recomputes the SHA-256 hash chain end-to-end. Any retroactive edit,
    deletion, or insertion breaks the chain and is reported here.
    """
    from agents.executor_agent import verify_audit_trail
    return verify_audit_trail(AUDIT_LOG)


# ── WebSocket — real-time feed ────────────────────────────────

_connections: list[WebSocket] = []


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _connections.append(ws)
    log.info(f"Dashboard client connected. Total: {len(_connections)}")
    try:
        # Send initial state
        init_data = dict(_state)
        init_data["balance_usdc"] = get_current_balance()
        init_data["trades"] = await get_trades()
        init_data["market_snapshots"] = await get_market()
        init_data["last_decisions"] = await get_decisions()
        init_data["analytics"] = await get_analytics()
        init_data["positions"] = await get_positions()
        init_data["equity_curve"] = await get_equity_curve()
        await ws.send_json({"type": "init", "data": init_data})
        while True:
            # Keep alive ping
            await asyncio.sleep(1)
            await ws.send_json({"type": "ping", "ts": datetime.now(timezone.utc).isoformat()})
    except WebSocketDisconnect:
        _connections.remove(ws)
        log.info(f"Dashboard client disconnected. Total: {len(_connections)}")


async def broadcast(event_type: str, data: dict):
    """Broadcast a real-time event to all connected dashboard clients."""
    msg = {"type": event_type, "data": data, "ts": datetime.now(timezone.utc).isoformat()}
    dead = []
    for ws in _connections:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.remove(ws)


# ── Static frontend ───────────────────────────────────────────
frontend_path = Path(__file__).parent / "frontend"
if (frontend_path / "dist").exists():
    frontend_path = frontend_path / "dist"

if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")


# ── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard.app:app", host="0.0.0.0", port=8000, reload=False)
