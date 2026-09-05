"""
TradeSentinel — Setup & Launcher Script
Guides the user through the complete setup and launches the system.
Run: python setup.py
"""

import asyncio
import os
import subprocess
import sys

YELLOW = "\033[93m"
GREEN  = "\033[92m"
RED    = "\033[91m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def banner():
    print(f"""
{YELLOW}{BOLD}
╔══════════════════════════════════════════════════════╗
║          TRADESENTINEL — SETUP WIZARD                ║
║     Binance Agent OS Mini Hackathon 2026             ║
╚══════════════════════════════════════════════════════╝
{RESET}""")


def step(n, title):
    print(f"\n{BLUE}{BOLD}[Step {n}]{RESET} {title}")
    print("─" * 50)


def ok(msg):
    print(f"  {GREEN}✅{RESET} {msg}")


def warn(msg):
    print(f"  {YELLOW}⚠️ {RESET} {msg}")


def err(msg):
    print(f"  {RED}❌{RESET} {msg}")


def info(msg):
    print(f"  ℹ️  {msg}")


# ─────────────────────────────────────────────────────
# Step 1: Check Python version
# ─────────────────────────────────────────────────────
def check_python():
    step(1, "Check Python version")
    v = sys.version_info
    if v.major >= 3 and v.minor >= 10:
        ok(f"Python {v.major}.{v.minor}.{v.micro} ✓")
    else:
        err(f"Python 3.10+ required, found {v.major}.{v.minor}")
        sys.exit(1)


# ─────────────────────────────────────────────────────
# Step 2: Install dependencies
# ─────────────────────────────────────────────────────
def install_deps():
    step(2, "Install Python dependencies")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        ok("httpx, fastapi, uvicorn, websockets, python-dotenv installed")
    else:
        warn("Some packages may have failed — continuing anyway")
        info(result.stderr[:200])


# ─────────────────────────────────────────────────────
# Step 3: Check .env file
# ─────────────────────────────────────────────────────
def check_env():
    step(3, "Check .env configuration")

    if not os.path.exists(".env"):
        warn(".env not found — creating from template")
        import shutil
        shutil.copy(".env.example", ".env")
        ok(".env created from .env.example")

    from dotenv import load_dotenv
    load_dotenv()

    auth_mode = os.getenv("BINANCE_MCP_AUTH_MODE", "oauth")
    llm_key   = os.getenv("LLM_API_KEY", "")

    info(f"Auth mode: {auth_mode}")

    if auth_mode == "oauth":
        ok("OAuth mode selected — no API keys needed for MCP!")
        info("Authentication happens via browser when you run Claude Code")
    elif auth_mode == "apikey":
        api_key = os.getenv("BINANCE_MCP_API_KEY", "")
        if api_key and api_key != "your_agentic_api_key_here":
            ok("Binance API key configured")
        else:
            warn("BINANCE_MCP_API_KEY not set — edit .env to add your key")

    if llm_key and llm_key != "your_anthropic_api_key_here":
        ok(f"LLM API key configured ({llm_key[:12]}...)")
    else:
        warn("LLM_API_KEY not set — Strategist will use fallback HOLD mode")
        info("Get a free key at: https://console.anthropic.com")


# ─────────────────────────────────────────────────────
# Step 4: Check/Install Claude Code (MCP connection)
# ─────────────────────────────────────────────────────
def setup_claude_code():
    step(4, "Claude Code + Binance MCP Server setup")

    # Check if claude is installed
    result = subprocess.run(["claude", "--version"], capture_output=True, text=True, shell=True)
    if result.returncode == 0:
        ok(f"Claude Code found: {result.stdout.strip()}")
    else:
        warn("Claude Code not installed")
        info("Installing Claude Code via npm...")
        r = subprocess.run(
            ["npm", "install", "-g", "@anthropic-ai/claude-code"],
            capture_output=True, text=True, shell=True
        )
        if r.returncode == 0:
            ok("Claude Code installed!")
        else:
            err("npm install failed. Install Node.js first: https://nodejs.org")
            info("Or skip this step and run in --demo mode")
            return False

    # Add Binance MCP server
    print(f"\n  {YELLOW}Adding Binance MCP Server to Claude Code...{RESET}")
    info("This will open your browser for Binance authentication.")
    info("You'll need to:")
    info("  1. Log into Binance")
    info("  2. Grant: Market Data + Trading permissions")
    info("  3. Create/select your Agentic sub-account")
    print()

    answer = input("  Run 'claude mcp add binance-mcp-server' now? [Y/n]: ").strip().lower()
    if answer in ("", "y", "yes"):
        result = subprocess.run(
            ["claude", "mcp", "add", "binance-mcp-server",
             "--transport", "http", "https://agent.binance.com/mcp/agentic"],
            shell=True
        )
        if result.returncode == 0:
            ok("Binance MCP Server added to Claude Code!")
        else:
            warn("Command ran but returned non-zero — check Claude Code output")
    else:
        info("Skipped. Run manually:")
        print(f"\n  {BOLD}claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic{RESET}\n")

    return True


# ─────────────────────────────────────────────────────
# Step 5: Test MCP connectivity (public endpoint)
# ─────────────────────────────────────────────────────
async def test_mcp():
    step(5, "Test MCP Server connectivity (public market data)")
    try:
        from mcp.binance_mcp_client import BinanceMCPClient, BinanceMCPError
        async with BinanceMCPClient() as client:
            ticker = await client.get_ticker("BTCUSDT")
            price  = ticker.get("price")
            if price:
                ok(f"Live BTC price: ${float(price):,.2f}")
                stats = await client.get_24h_stats("BNBUSDT")
                ok(f"BNB 24h change: {stats.get('priceChangePercent', 'N/A')}%")
                ok("MCP Server is live and responding!")
            else:
                warn("Connected but no price data — check network/VPN")
    except Exception as e:
        warn(f"Public market data test failed: {e}")
        info("This is OK if you are behind a firewall — the system will retry at runtime")


# ─────────────────────────────────────────────────────
# Step 6: Fund Agentic sub-account reminder
# ─────────────────────────────────────────────────────
def funding_reminder():
    step(6, "Fund your Agentic sub-account")
    print(f"""
  Per official Binance docs, you must manually fund the Agentic
  sub-account before the agent can trade. Do this on Binance web:

  {BOLD}https://www.binance.com/en/my/sub-account/asset-management/transfer?asset=USDT{RESET}

  Recommended: Transfer {YELLOW}20–50 USDT/USDC{RESET} to the Agentic sub-account.
  The agent CANNOT pull funds from your main account by itself.

  Emergency stop available any time:
  Profile → Dashboard → Sub-account → Account Management → Emergency Stop
""")


# ─────────────────────────────────────────────────────
# Step 7: Launch options
# ─────────────────────────────────────────────────────
def launch_menu():
    step(7, "Launch TradeSentinel")
    print(f"""
  Choose how to run:

  {BOLD}[1]{RESET} Demo mode (mock orders, safe)
      python orchestrator.py --demo --dashboard

  {BOLD}[2]{RESET} Live mode (real orders on Agentic sub-account)
      python orchestrator.py --dashboard

  {BOLD}[3]{RESET} Test MCP via Claude Code (for the hackathon demo video)
      claude  (then type your trading commands in plain language)

  {BOLD}[4]{RESET} Exit
""")
    choice = input("  Enter choice [1/2/3/4]: ").strip()

    if choice == "1":
        print(f"\n{GREEN}Launching in DEMO mode...{RESET}\n")
        os.system(f"{sys.executable} orchestrator.py --demo --dashboard")
    elif choice == "2":
        print(f"\n{YELLOW}Launching in LIVE mode — real money!{RESET}\n")
        os.system(f"{sys.executable} orchestrator.py --dashboard")
    elif choice == "3":
        print(f"\n{GREEN}Opening Claude Code...{RESET}")
        print("  In Claude: ask 'Show my BTCUSDT price via Binance MCP Server'\n")
        os.system("claude")
    else:
        print("Goodbye!")


# ─────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────
def main():
    banner()
    check_python()
    install_deps()
    check_env()
    setup_claude_code()
    asyncio.run(test_mcp())
    funding_reminder()
    launch_menu()


if __name__ == "__main__":
    main()
