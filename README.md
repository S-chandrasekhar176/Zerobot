# ZeroBot Z1 — AI-Driven Algorithmic Trading System

[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-green)]()
[![Status: Active Development](https://img.shields.io/badge/Status-Active-brightgreen)]()
[![NSE India](https://img.shields.io/badge/Market-NSE%20India-orange)]()

**ZeroBot Z1** is a production-grade algorithmic trading engine for the National Stock Exchange (NSE), India. It combines multi-strategy ensemble execution, ML-driven signal generation, sophisticated risk management (13-gate validator), and real-time market data integration into a modular, extensible system.

Designed for quantitative traders and developers who want a serious, professionally-structured trading platform.

---

## 🎯 Key Features

- **Multi-Strategy Engine**: 9 independent strategies (Momentum, Mean Reversion, VWAP, SuperTrend, Market Making, Statistical Arbitrage, RSI Divergence, Opening Range Breakout, Breakout)
- **ML Ensemble**: XGBoost + LightGBM + ExtraTrees with triple-barrier labeling and expected return scoring
- **13-Gate Risk Management**: Comprehensive pre-trade validation including daily loss limits, VIX gates (tiered), sector exposure, correlation checks, margin requirements, and portfolio VaR
- **Real-Time Data Feeds**: Shoonya WebSocket (S-Mode), Yahoo Finance fallback, NSE option chain integration
- **Multi-Broker Support**: Paper trading, Shoonya Finvasia, Angel One SmartAPI
- **Async Execution**: Non-blocking I/O for responsive trading loops
- **LLM Integration**: Groq-powered trade rationale, session briefing, and news sentiment analysis
- **Production Safety**: Paper broker for dry-runs, configurable position limits, automatic circuit breakers

---

## 📊 System Architecture

```
Market Data Feed (Shoonya/Yahoo)
         ↓
    Engine Loop (09:15–15:30 IST)
         ↓
  Strategy Ensemble
  (9 signals in parallel)
         ↓
    Risk Engine
  (13-gate validation)
         ↓
    Broker
  (Paper/Live execution)
         ↓
   Portfolio State
  (P&L, drawdown, VaR)
         ↓
   Alerts (Telegram)
```

For detailed architecture, see [docs/architecture.md](docs/architecture.md).

---

## 🚀 Quick Start

### 1. Install

```bash
git clone https://github.com/yourusername/zerobot.git
cd zerobot

# Virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure

Copy and edit environment file:
```bash
cp config/.env.example config/.env
```

Edit `config/.env`:
```env
GROQ_API_KEY=gsk_your_groq_key_here
GROQ_ENABLED=true
SHOONYA_LOGIN=your_shoonya_username
SHOONYA_PASSWORD=your_shoonya_password
TELEGRAM_TOKEN=your_telegram_token  # optional
```

Edit `config/settings.yaml`:
```yaml
capital: 100000  # ₹100,000 starting capital

symbols:
  - RELIANCE
  - TCS
  - INFY
  - HDFCBANK
  # Add more NSE symbols

strategies:
  momentum: { enabled: true, weight: 1.0 }
  mean_reversion: { enabled: true, weight: 0.8 }
  supertrend: { enabled: true, weight: 0.9 }

risk:
  daily_loss_limit_pct: -3.0
  vix_halt_threshold: 25.0
  max_sector_exposure_pct: 50

broker:
  mode: "paper"  # paper | s_mode | hybrid | dual | live
  name: "paper"
```

### 3. Run

```bash
# Paper trading (simulated, safe)
python main.py --mode paper

# S-Mode: Shoonya WS data + paper execution
python main.py --mode s_mode

# Live trading (real money)
python main.py --mode live --broker shoonya
```

For complete setup guide, see [docs/usage.md](docs/usage.md).

---

## 📈 What It Does

**During Trading Hours (09:15–15:30 IST)**:

1. **Signal Generation**: 9 strategies analyze price action, volume, technicals
2. **Risk Validation**: 13-gate checks for market conditions, capital limits, sector exposure, news sentiment  
3. **Execution**: Accepted signals → broker orders (paper or live)
4. **Monitoring**: Real-time PnL, drawdown tracking, position Greeks
5. **Alerts**: Telegram notifications on entries, exits, risk events

**End-of-Day (15:30 IST)**:

- Close all positions
- Calculate daily PnL, Sharpe ratio, win rate
- Generate trade narrative (via Groq LLM)
- Send summary to Telegram
- Save state for next session

Example output:
```
[09:16] MOMENTUM: LONG RELIANCE @ ₹2,785 (confidence: 78%)
[09:16] Risk: PASS (13/13 gates)
[09:16] Execution: BUY 10 shares
[15:30] EOD: Daily PnL = +₹2,345 (+2.35%), Win Rate = 4/5
```

---

## 🛡️ Risk Management (13 Gates)

Every trade must pass all checks:

| # | Gate | Check | Threshold |
|---|------|-------|-----------|
| 1 | Halted | Bot not manually halted | Manual flag |
| 2 | Market Hours | Within trading hours | 09:15–15:30 IST |
| 3 | Daily Loss | Session loss limit | > -3% capital |
| 4 | Position Count | Max open positions | ≤ 8 (dynamic) |
| 5 | Loss Streak | Consecutive losses | < 3 |
| 6 | ML Confidence | Model confidence score | ≥ 62% |
| 7 | VIX | Volatility gate (tiered) | Pass 20–25, halt >25 |
| 8 | Margin | Available capital buffer | ≥ position × 1.20 |
| 9 | Sector Exposure | Max single sector | ≤ 50% capital |
| 10 | Correlation | Same-sector positions | ≤ 40% of portfolio |
| 11 | News Sentiment | No fraud/SEBI blocks | Groq news analysis |
| 12 | Strategy Circuit | Per-strategy halt flag | Not halted |
| 13 | Portfolio VaR | Value-at-risk | ≤ 5% capital |

**VIX Gate Details** (as of G3):
- **VIX ≤ 20**: Full position size
- **20 < VIX ≤ 25**: Pass but reduce size via Kelly criterion (confidence penalty)
- **VIX > 25**: Hard halt, no new trades

See [risk/risk_engine.py](risk/risk_engine.py) for complete validation logic.

---

## 🎓 Example Usage

Run paper trading with a simple example:

```bash
python examples/run_bot.py
```

Or write custom code:

```python
from core.engine import TradingEngine
from core.config import load_config

config = load_config("config/settings.yaml", "config/.env")
engine = TradingEngine(config)

# Dry-run mode: uses Yahoo Finance, simulates orders
engine.start(mode="paper")
```

For more examples, see [examples/](examples/).

---

## 📁 Project Structure

```
zerobot/
├── core/                    # Engine, state, config, LLM integration
│   ├── engine.py           # Main trading loop (1800+ lines)
│   ├── state_manager.py    # Global BotState singleton
│   ├── config.py           # Settings + env loader
│   ├── groq_brain.py       # LLM calls (trade narratives)
│   ├── regime_detector.py  # Market regime classifier
│   └── events_calendar.py  # NSE events, earnings
│
├── risk/                    # Risk validation & position sizing
│   ├── risk_engine.py      # 13-gate validator
│   ├── groq_gates.py       # ML confidence + news sentiment gates
│   └── kelly_sizer.py      # Kelly criterion position sizing
│
├── strategies/              # 9 independent strategies
│   ├── base_strategy.py    # Base class
│   ├── momentum.py
│   ├── mean_reversion.py
│   ├── vwap_strategy.py
│   ├── market_making.py
│   ├── stat_arb.py         # Statistical arbitrage (pairs)
│   ├── supertrend.py
│   ├── opening_range_breakout.py
│   ├── rsi_divergence.py
│   └── breakout.py
│
├── data/                    # Market data feeds & preprocessing
│   ├── feeds/
│   │   ├── realtime_feed.py
│   │   ├── shoonya_feed.py (S-Mode WS)
│   │   ├── nse_option_chain.py
│   │   ├── historical_feed.py
│   │   └── fii_data.py
│   ├── cache/              # Parquet OHLCV cache
│   └── storage/
│
├── models/                  # ML ensemble training & inference
│   ├── trainer.py          # XGBoost + LightGBM + ExtraTrees
│   └── predictor.py        # Signal generation + confidence
│
├── broker/                  # Broker integrations
│   ├── paper_broker.py     # Simulated trading
│   ├── shoonya_paper_broker.py (S-Mode safe wrapper)
│   ├── shoonya_live_broker.py
│   ├── angel_one.py
│   ├── dual_broker.py
│   ├── hybrid_broker.py
│   └── factory.py          # Broker selection
│
├── news/                    # News feed & sentiment
│   ├── feed_aggregator.py  # ET Markets + NSE news
│   └── sentiment_engine.py
│
├── alerts/                  # Notifications
│   └── telegram_bot.py
│
├── config/
│   ├── settings.yaml       # Core trading config
│   └── .env                # API keys (git-ignored)
│
├── docs/                    # Documentation
│   ├── architecture.md
│   └── usage.md
│
├── examples/                # Runnable example scripts
│   └── run_bot.py
│
├── logs/                    # Trade, signal, error logs
│   ├── trades/
│   ├── signals/
│   └── errors/
│
├── tests/                   # Test suite
│   ├── test_critical_fixes.py
│   ├── test_edge_cases.py
│   └── test_system_boot.py
│
├── main.py                  # Entry point
├── healthcheck.py           # System health diagnostics
├── requirements.txt
├── LICENSE (MIT)
├── README.md (this file)
└── CONTRIBUTING.md
```

---

## 🔧 Trading Modes

| Mode | Data Source | Execution | Use Case |
|------|--------|-----------|----------|
| **Paper** | Yahoo Finance 15s | Simulated | Backtesting, dry-run |
| **S-Mode** ⭐ | Shoonya WS (market hrs) / Yahoo | Simulated | Low-latency testing |
| **Hybrid** | Angel One WS | Simulated | Angel data validation |
| **Dual** | Angel One WS | Shoonya Live | Real trading (Angel data) |
| **Live (S)** | Shoonya WS | Shoonya Live | Real trading (Shoonya data) |

---

## 🧪 Testing

```bash
# Health check
python healthcheck.py

# Critical system tests (use any day, no market hours)
python test_critical_fixes.py

# Edge case & regression tests
python test_edge_cases.py

# Full system boot test
python test_system_boot.py

# Backtesting
python run_backtest.py --symbol RELIANCE --start 2024-01-01 --end 2025-12-31
```

---

## 📊 Typical Session Output

```
[2026-03-23 09:15:23] Initializing ZeroBot Z1 in PAPER mode...
[2026-03-23 09:15:24] Loading config: 100000 capital, 9 strategies
[2026-03-23 09:15:25] Connecting to realtime feed (Yahoo Finance)...
[2026-03-23 09:15:26] Market opened. Starting trading loop.
[2026-03-23 09:16:12] [MOMENTUM] Signal LONG on RELIANCE @ ₹2,785.50 (confidence: 78%)
[2026-03-23 09:16:12] Risk Engine: PASS (13/13 gates)
[2026-03-23 09:16:13] [PAPER BROKER] BUY 10 shares RELIANCE @ ₹2,785.50
[2026-03-23 09:16:13] Position opened: qty=10, entry=₹2,785.50, risk=₹500
[2026-03-23 09:47:32] [MOMENTUM] Signal LONG on TCS @ ₹4,125.25 (confidence: 85%)
[2026-03-23 09:47:32] Risk Engine: PASS (13/13 gates)
[2026-03-23 09:47:33] [PAPER BROKER] BUY 5 shares TCS @ ₹4,125.25
[2026-03-23 12:15:45] [CLOSURE] Stop-loss triggered on RELIANCE. Close 10 shares @ ₹2,770
[2026-03-23 12:15:46] Trade: RELIANCE LONG | Entry: 2,785.50 | Exit: 2,770 | P&L: -₹155
[2026-03-23 15:30:00] Market closed. Running EOD settlement.
[2026-03-23 15:30:02] Closing position TCS (5 shares @ ₹4,130)
[2026-03-23 15:30:03] Trade: TCS LONG | Entry: 4,125.25 | Exit: 4,130 | P&L: +₹24
[2026-03-23 15:30:05] Daily Summary:
  Total trades: 2
  Wins: 1, Losses: 1, Win Rate: 50%
  Daily P&L: -₹131 (-0.13%)
  Peak capital: ₹100,000 | Current: ₹99,869
  Drawdown: 0.13%
[2026-03-23 15:30:06] Generating EOD report and sending alerts...
[2026-03-23 15:30:10] [TELEGRAM] Daily Summary sent
```

---

## 🔐 Security & Disclaimer

⚠️ **IMPORTANT**:

- **Never commit `.env` files** — API keys and credentials are git-ignored
- **Paper mode first** — Always validate strategies in simulated mode before live trading
- **Risk limited** — Daily loss limits, position limits, and VIX gates are enforced
- **No guarantees** — Past performance ≠ future results. Markets are unpredictable.

**Trading involves substantial risk of loss.** ZeroBot is provided "as-is" for educational and research purposes. The maintainers assume no responsibility for trading losses or system failures.

---

## 🛠️ Installation & Dependencies

```bash
# Python 3.9+
python --version

# Install
pip install -r requirements.txt

# Check system
python healthcheck.py
```

Key dependencies:
- **pandas, numpy** — Data manipulation
- **xgboost, lightgbm** — ML models
- **scikit-learn** — Preprocessing, ensemble
- **aiohttp** — Async HTTP
- **requests** — Broker APIs
- **pyyaml** — Config loading
- **pytz** — Timezone handling
- **telegram** — Alerts

---

## 📚 Documentation

- [docs/architecture.md](docs/architecture.md) — System design, components, data flow
- [docs/usage.md](docs/usage.md) — Setup, configuration, running examples
- [CONTRIBUTING.md](CONTRIBUTING.md) — How to contribute, add strategies, coding standards
- [examples/](examples/) — Runnable example scripts

---

## 🚦 Roadmap

**Completed (G3-ML)**:
- ✅ 13-gate risk engine with VIX tiering
- ✅ S-Mode Shoonya WebSocket integration
- ✅ ML ensemble (XGBoost, LightGBM, ExtraTrees)
- ✅ Groq LLM integration (trade narratives, sentiment)
- ✅ Paper broker with realistic slippage
- ✅ Telegram alerts

**Coming Soon**:
- 🔮 Backtesting framework with walk-forward validation
- 🔮 Options trading strategies
- 🔮 Multi-account support
- 🔮 Dashboard UI (Streamlit)
- 🔮 Advanced order types (iceberg, VWAP slices)

---

## 💡 How to Contribute

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for:
- How to add a new strategy
- How to add a new broker
- Coding standards and conventions
- Running tests

---

## 📝 License

MIT License — See [LICENSE](LICENSE) for details.

Used with ZeroBot Z1 © 2025.

---

## 🤝 Support

- **Questions**: Open a GitHub Issue with the `question` label
- **Bug Report**: File a GitHub Issue with `bug` label + reproduction steps
- **Feature Request**: GitHub Discussions or Issue with `enhancement` label
- **Security**: Report privately to maintainers (do not open public issues)

---

## 🎓 About

ZeroBot Z1 is a production-grade algorithmic trading engine designed for:
- Quantitative traders
- ML engineers exploring market microstructure
- Developers building trading systems
- Students studying algorithmic trading

It emphasizes **code quality, safety, modularity, and professional architecture** — not just profit maximization.

---

**Last updated**: 2026-03-23 | **Version**: Z1 G3-ML | **Status**: Active Development

---

## Understanding Key Log Messages

| Message | Meaning |
|---|---|
| `RISK BLOCK [Pos Count] 8/8` | Max positions full — waits for a close |
| `RISK BLOCK [VIX] 22.1` | India VIX above 20 — too volatile for new trades |
| `RISK BLOCK [Correlation] 3/3 in BANKING` | Sector limit hit — other sectors still trade |
| `[E1] Urgent BUY scan queued` | High-impact bullish headline → instant signal scan |
| `[E2] NEWS GUARD hard block` | Fraud/ED/SEBI news on open position → emergency exit |
| `[E4] Tick spike 3.2% UP` | Price jumped 3%+ → urgent strategy check |
| `Telegram send failed` | Not configured — bot works fine without it |

---

## Holiday / Weekend Testing

```cmd
:: Windows CMD
set ZEROBOT_FORCE_MARKET_OPEN=1
python -X utf8 main.py
```

```powershell
# PowerShell
$env:ZEROBOT_FORCE_MARKET_OPEN="1"
python -X utf8 main.py
```

Bypasses the market hours gate only. All 10 other gates remain active.

---

## Bot Daily Schedule

| Time (IST) | Event |
|---|---|
| 09:00 | Daily reset, load symbols |
| 09:15 | Market opens, news feed active |
| 09:15–09:30 | Warmup — no new trades |
| 09:30 | Active trading begins |
| 15:15 | No new entries |
| 15:15–15:25 | Auto square-off all positions |
| 15:30 | Telegram daily report |

---

## Database

See `DATABASE_SETUP.md`. Without PostgreSQL the bot runs fully in-memory —
all features work, trades just aren't persisted across restarts.
