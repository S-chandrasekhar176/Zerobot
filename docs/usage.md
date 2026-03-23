# ZeroBot Z1 — Usage Guide

## Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/zerobot.git
cd zerobot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or: venv\Scripts\activate (Windows)

# Install dependencies
pip install -r requirements.txt
```

### 2. Configuration

Create `.env` file in the project root:
```bash
cp config/.env.example config/.env
```

Edit `config/.env`:
```
GROQ_API_KEY=gsk_your_groq_key
GROQ_ENABLED=true
SHOONYA_LOGIN=your_shoonya_username
SHOONYA_PASSWORD=your_shoonya_password
SHOONYA_TOTP_SECRET=your_2fa_secret  # Optional
ANGEL_API_KEY=your_angel_api_key     # Optional
TELEGRAM_TOKEN=your_telegram_token   # Optional
```

Edit `config/settings.yaml`:
```yaml
capital: 100000                        # Starting capital in ₹

symbols:
  - RELIANCE
  - TCS
  - INFY
  - HDFCBANK
  - ICICIBANK
  # Add more NSE symbols here

strategies:
  momentum: { enabled: true, weight: 1.0 }
  mean_reversion: { enabled: true, weight: 0.8 }
  supertrend: { enabled: true, weight: 0.9 }

risk:
  daily_loss_limit_pct: -3.0           # Stop if daily PnL < -3%
  vix_halt_threshold: 25.0             # VIX > 25 → hard halt
  max_sector_exposure_pct: 50          # Max sector concentration
  max_position_size_pct: 5             # Max single position

broker:
  mode: "paper"                        # paper | s_mode | hybrid | dual | live
  name: "paper"                        # paper | shoonya_paper | angel_paper | hybrid | dual | shoonya
```

---

## Running the Bot

### Paper Trading (Simulated)

```bash
# Dry-run mode: uses Yahoo Finance, simulates execution
python main.py --mode paper
```

Output:
```
[2026-03-23 09:15:23] Initializing ZeroBot Z1 in PAPER mode
[2026-03-23 09:15:24] Loading config: 100000 capital, 9 strategies
[2026-03-23 09:15:25] Market opened. Starting trading loop.
[2026-03-23 09:16:12] [MOMENTUM] Signal LONG on RELIANCE @ ₹2785.50 (confidence: 78%)
[2026-03-23 09:16:12] Risk Engine: PASS (13/13 gates)
[2026-03-23 09:16:13] [PAPER BROKER] BUY 10 shares RELIANCE @ ₹2785.50
[2026-03-23 09:16:13] Position opened: RELIANCE | qty=10 | entry=2785.50 | risk=₹500
...
[2026-03-23 15:30:00] Market closed. Running EOD settlement.
[2026-03-23 15:30:05] Daily PnL: +₹2,345 (+2.35%)
[2026-03-23 15:30:06] Generating EOD report via Groq...
[2026-03-23 15:30:10] [TELEGRAM] Daily Summary: 5 trades, +₹2,345, 4/5 wins
```

### S-Mode (Shoonya WebSocket + Paper Execution)

```bash
# Production-style mode: live Shoonya data, simulated execution
python main.py --mode s_mode
```

- **Market hours (09:15–15:30 IST)**: Uses Shoonya WebSocket (real-time ticks)
- **Outside hours**: Automatically falls back to Yahoo Finance
- **Execution**: Paper (safe for testing)

### Live Trading (Shoonya)

```bash
# REAL MONEY: live execution on Shoonya broker
python main.py --mode live --broker shoonya
```

⚠️ **WARNING**: This is LIVE trading. Use only after extensive backtesting and dry-run validation.

### Hybrid Mode (Angel One Data + Paper Execution)

```bash
python main.py --mode hybrid
```

Useful for testing Angel One data integration before going live.

---

## Monitoring

### Terminal Output

ZeroBot prints trades, signals, and risk gates to stdout. Look for:

```
[MOMENTUM] Signal LONG on TCS @ ₹4125 (confidence: 85%)
Risk Engine: PASS (gate 7/VIX = 18.5)  
[PAPER BROKER] BUY 5 shares TCS @ ₹4125
```

### Logs

Check log files in `logs/` directory:

```bash
# Trade execution log
tail -f logs/trades/trades.log

# Strategy signals
tail -f logs/signals/signals.log

# Errors
tail -f logs/errors/errors.log
```

### Telegram Notifications

If `TELEGRAM_TOKEN` is set:
- Entry signals (with reason)
- Exit signals (with PnL)
- Daily summary (trades, PnL, drawdown)
- Risk warnings (VIX halts, drawdown limits)

---

## Example 1: Basic Run

```python
# examples/run_bot.py
from core.engine import TradingEngine
from core.config import load_config

config = load_config("config/settings.yaml", "config/.env")
engine = TradingEngine(config)

# Run in dry mode
engine.start(mode="paper")
```

Run it:
```bash
python examples/run_bot.py
```

---

## Example 2: Custom Strategy Testing

```python
# Add to examples/test_strategy.py
from strategies.momentum import MomentumStrategy
from data.feeds.realtime_feed import RealtimeFeed
import pandas as pd

feed = RealtimeFeed()
strategy = MomentumStrategy()

# Fetch data
data = feed.get_ohlcv("RELIANCE", "15m", bars=100)

# Generate signal
signal = strategy.generate_signal(data)
print(f"Signal: {signal}")
```

---

## Example 3: Backtest a Strategy

```bash
python run_backtest.py --strategy momentum --symbol RELIANCE --start 2024-01-01 --end 2025-12-31
```

Output: returns, Sharpe ratio, max drawdown, win rate.

---

## Troubleshooting

### Bot won't start

```bash
# Check Python version (3.9+)
python --version

# Check imports
python -c "from core.engine import TradingEngine; print('OK')"

# Run health check
python healthcheck.py
```

### Shoonya connection fails

```bash
# Auto-install missing dependencies
pip install NorenRestApiPy pyotp

# Verify credentials in config/.env
grep SHOONYA_LOGIN config/.env
```

### No signals being generated

Check:
1. Market is open (09:15–15:30 IST, Mon–Fri)
2. Symbols are valid (NSE only: RELIANCE, TCS, INFY, etc.)
3. Strategies are enabled in `config/settings.yaml`
4. Check logs: `tail -f logs/signals/signals.log`

### Memory leak / slow performance

- Reduce symbol count in `config/settings.yaml`
- Disable less important strategies
- Check Groq API rate limits

---

## Configuration Reference

### strategy: Fields

```yaml
strategies:
  momentum:
    enabled: true          # Enable/disable strategy
    weight: 1.0            # Allocation weight (sum = 1)
    params:
      lookback: 20         # Bars for trend calculation
      threshold: 0.02      # Min momentum to trigger
```

### risk: Fields

```yaml
risk:
  daily_loss_limit_pct: -3.0      # Stop trading if daily loss exceeds this
  vix_halt_threshold: 25.0        # VIX > this → no new trades
  max_sector_exposure_pct: 50     # Single sector max exposure
  max_position_size_pct: 5        # Single position max size
  max_consecutive_losses: 3       # Stop after N consecutive losses
  min_margin_buffer_pct: 20       # Capital buffer for margin
```

### broker: Fields

```yaml
broker:
  name: "paper"           # paper | shoonya | angel_one | dual | hybrid
  mode: "paper"           # paper | s_mode | hybrid | dual | live
  slippage_bps: 2         # Slippage in basis points (paper mode)
  commission_pct: 0.03    # Commission per trade
```

---

## API Reference

### Starting the Engine

```python
from core.engine import TradingEngine
from core.config import load_config

config = load_config("config/settings.yaml", "config/.env")
engine = TradingEngine(config)

# Start trading
engine.start(mode="paper")

# Stop trading (graceful shutdown)
engine.stop()
```

### Accessing State

```python
from core.state_manager import state_mgr

print(f"Capital: {state_mgr.capital}")
print(f"Daily PnL: {state_mgr.daily_pnl}")
print(f"Open positions: {state_mgr.open_positions}")
print(f"Consecutive losses: {state_mgr.consecutive_losses}")
print(f"Drawdown: {state_mgr.drawdown_pct:.2f}%")
```

### Checking Risk Gates

```python
from risk.risk_engine import RiskEngine

risk_engine = RiskEngine(config)
evaluation = risk_engine.evaluate_gates(symbol="RELIANCE", qty=5, side="BUY")

if evaluation.passed:
    print("✓ All gates PASS")
else:
    print(f"✗ Gate {evaluation.failed_gate} blocked: {evaluation.reason}")
```

### Placing Orders Manually

```python
from broker.paper_broker import PaperBroker

broker = PaperBroker(capital=100000)
order = broker.place_order(
    symbol="RELIANCE",
    qty=10,
    side="BUY",
    price=2785.50,
    order_type="LIMIT"
)

print(f"Order {order['id']}: {order['status']}")
print(f"Filled: {order.get('filled', 0)} / {order['qty']}")
```

---

## FAQ

**Q: Can I trade across multiple time zones?**  
A: ZeroBot is hardcoded for IST (Indian Standard Time). Multi-timezone support is planned.

**Q: What symbols can I trade?**  
A: Any NSE-traded symbol: RELIANCE, TCS, INFY, HDFCBANK, etc. Nifty 50 symbols recommended.

**Q: How much capital do I need?**  
A: Minimum ₹50,000 for realistic position sizing. Pod version supports ₹10,000+ for testing.

**Q: Can I run multiple instances?**  
A: Not recommended. Single instance per machine. Multi-account support planned.

**Q: Does it trade options?**  
A: Not yet. Equity only. Options support in ZeroBot Z2 (roadmap).

**Q: How often does it retrain the ML model?**  
A: Daily at 16:00 IST on prior session's data.

---

## Getting Help

- **Issues**: GitHub Issues (bug reports, feature requests)
- **Discussions**: GitHub Discussions (Q&A, ideas)
- **Documentation**: See `/docs` for more detailed guides
- **Examples**: See `/examples` for runnable code samples

---

*Last updated: 2026-03-23 | ZeroBot Z1 v1.0.0*
