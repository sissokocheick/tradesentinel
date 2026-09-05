# 🛡️ TradeSentinel — Multi-Agent Autonomous Trading System

> **Binance Agent OS Mini Hackathon 2026 — Track A Submission**

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://python.org)
[![Binance Agent OS](https://img.shields.io/badge/Binance-Agent%20OS-f0b90b)](https://binance.com/en/agent-os)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## 🎯 What Is TradeSentinel?

TradeSentinel is a **multi-agent AI trading system** built entirely on [Binance Agent OS](https://www.binance.com/en/agent-os). Three specialized agents collaborate in a real-time pipeline to monitor markets, generate strategies, and execute trades — all with strict risk controls and a full audit trail.

```
┌─────────────────────────────────────────────────────────────┐
│               TRADESENTINEL — 3-AGENT PIPELINE              │
│                                                             │
│  [Scout Agent] ──→ [Strategist Agent] ──→ [Executor Agent] │
│   Real-time data    LLM-powered analysis    Safe execution  │
│                                                             │
│   Binance MCP        Skills Hub            Agentic Wallet   │
│   Market Data        LLM Reasoning         x402 Payments    │
└─────────────────────────────────────────────────────────────┘
```

---

## 🤖 The Three Agents

### 🔍 Scout Agent
Continuously polls the **Binance MCP Server** for:
- Real-time price tickers for 5 major pairs
- Order book depth & bid/ask spread
- 24h volume, high/low
- Computes RSI(14), MACD signal, Bollinger Band position
- Calculates order book imbalance (buy/sell pressure)

### 🧠 Strategist Agent
Receives structured market snapshots and:
- Builds rich context prompts with **session memory** (last 20 decisions)
- Calls Claude via API to generate structured `{action, symbol, confidence, rationale}` JSON
- Supports three strategies: **momentum**, **mean_reversion**, **breakout**
- Only emits trade intents when `confidence ≥ 65%`

### ⚡ Executor Agent
The final safety gate before any real money moves:
1. ✅ **Risk assessment** — 7 independent checks
2. 🔔 **Human approval** — required for orders > $20 USDC
3. 💸 **x402 micropayment** — pays for premium data
4. 📤 **Order placement** — via Binance MCP on Agentic sub-account
5. 🛡️ **Stop-loss setup** — auto-placed at entry
6. 📋 **Audit log** — every decision written to `logs/audit_trail.json`

---

## 🛡️ Safety & Risk Management

All guardrails are configurable via environment variables:

| Guardrail | Default | Description |
|-----------|---------|-------------|
| `RISK_MAX_POSITION_USDC` | $50 | Max value per trade |
| `RISK_MAX_DRAWDOWN_PCT` | 5% | Emergency halt threshold |
| `RISK_MAX_TRADES_PER_HOUR` | 10 | Anti-frénésie rate limit |
| `RISK_APPROVAL_THRESHOLD` | $20 | Requires human confirmation |
| `RISK_MIN_CONFIDENCE` | 65% | Minimum LLM confidence to trade |
| `RISK_STOP_LOSS_PCT` | 2% | Auto stop-loss on every position |
| `RISK_TAKE_PROFIT_PCT` | 4% | Auto take-profit target |
| `RISK_MAX_OPEN_POSITIONS` | 3 | Concurrent position limit |

**The Agentic sub-account is isolated from the main Binance account.** External withdrawals are disabled by Agent OS — funds can only be used for trading within Binance.

---

## 🛠️ Agent OS Tools Used

| Tool | Usage |
|------|-------|
| **MCP Server** | Market data (tickers, klines, order book, funding rates) + order execution |
| **Agentic Wallet** | Isolated sub-account for all agent trading capital |
| **x402 Payments** | Automated micropayments for premium data per trade |
| **Skills Hub** | Technical analysis skills (RSI, MACD, Bollinger Bands) |

---

## ⚡ Quick Start

### 1. Clone & Install
```bash
git clone https://github.com/YOUR_USERNAME/tradesentinel
cd tradesentinel
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env
# Edit .env with your Binance Agent OS API keys
```

### 3. Setup Agentic Sub-account
1. Go to [binance.com/en/agent-os](https://www.binance.com/en/agent-os)
2. Create an **Agentic sub-account**
3. Grant permissions: `market_data`, `trading`
4. Transfer a small amount of USDC (e.g., $20) to the sub-account
5. Copy the API key & secret to `.env`

### 4. Run

**Demo mode (no real orders):**
```bash
python orchestrator.py --demo
```

**Live mode with dashboard:**
```bash
python orchestrator.py --dashboard
```
Then open `http://localhost:8000` in your browser.

**Docker:**
```bash
docker-compose up
```

---

## 📊 Live Dashboard

The dashboard provides real-time visibility into all agent activity:

- 💰 Agentic wallet balance
- 🤖 Per-agent status (Scout / Strategist / Executor)
- 📈 Live market data (5 pairs from Binance MCP)
- 🗂️ Trade history with strategy tags
- 📋 Immutable audit trail (every decision logged)
- 🖥️ Live system log

---

## 📁 Project Structure

```
tradesentinel/
├── agents/
│   ├── scout_agent.py        # MCP market data + technical indicators
│   ├── strategist_agent.py   # LLM-powered trade decisions
│   └── executor_agent.py     # Risk-gated order execution
├── mcp/
│   └── binance_mcp_client.py # Official Binance MCP wrapper
├── wallet/
│   ├── agentic_wallet.py     # Agentic sub-account operations
│   └── x402_payments.py      # Machine-to-machine micropayments
├── skills/
│   └── risk_manager.py       # 7-check risk engine
├── dashboard/
│   ├── app.py                # FastAPI + WebSocket backend
│   └── frontend/index.html   # Real-time dashboard
├── logs/
│   └── audit_trail.json      # Immutable decision log (append-only)
├── orchestrator.py           # Main entry point
├── .env.example              # Configuration template
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## 🔒 Security Notes

- API keys are loaded from environment variables — never hardcoded
- All agent activity is confined to the **Agentic sub-account** (isolated from main funds)
- The MCP server does **not** support external withdrawals
- Every decision is logged with a timestamp, rationale, and risk assessment
- Human approval required for any order above $20 USDC

---

## 🏆 Hackathon Submission

**Track:** A — Best Agent Built with Binance Agent OS  
**Event:** Binance Agent OS Mini Hackathon 2026  
**Demo video:** [Link to be added]

---

## License

MIT — free to use, modify, and distribute.
