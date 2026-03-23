# ZeroBot Z1 — Architecture Overview

## System Design

ZeroBot Z1 is a modular, production-grade algorithmic trading engine built on asynchronous Python. The system orchestrates multiple trading strategies, risk management gates, ML-driven decision-making, and real-time market data feeds through a central engine loop.

---

## Core Components

### 1. **Engine Core** (`core/`)
- **engine.py** (1800+ lines)
  - Central trading loop orchestrating all tasks
  - Market hours monitoring (9:15–15:30 IST)
  - End-of-day settlement and reporting
  - Async task coordination

- **state_manager.py**
  - BotState singleton managing capital, PnL, open positions
  - Global market data cache (VIX, FII/DII, etc.)
  - Daily accounting and performance tracking

- **config.py**
  - YAML + environment file loader
  - Typed configuration object (dataclass-based)
  - Runtime immutability constraints

- **groq_brain.py**
  - LLM integration for trade rationale generation
  - Session briefing and trade narratives
  - Exit reasoning via Groq API (cached 5 min TTL)

- **regime_detector.py**
  - Market regime classification (BULL, BEAR, CRISIS, DEFENSIVE)
  - Volatility-driven logic routing

- **events_calendar.py**
  - NSE events: earnings, RBI sessions, expiry dates
  - Risk multiplier adjustments per event

### 2. **Risk Management**: `risk/`
- **risk_engine.py** — 13-gate validator
  ```
  1.  Halted status
  2.  Market hours (9:30–15:15 IST)
  3.  Daily loss limit (>-3% capital)
  4.  Position count (dynamic: ₹55k→8 slots)
  5.  Loss streak (<3 consecutive losses)
  6.  ML confidence (≥62%)
  7.  VIX gate (tiered: 20–25 penalty, >25 hard halt)
  8.  Margin (capital ≥ position × 1.20)
  9.  Sector exposure (≤50% capital)
  10. Correlation (max 40% in same sector)
  11. News sentiment (no fraud/SEBI blocks)
  12. Strategy circuit breaker (per-strategy halt)
  13. Portfolio VaR (≤5% capital)
  ```

- **groq_gates.py** — Gates 6 & 11
  - ML confidence scoring
  - News sentiment extraction (Groq)

- **kelly_sizer.py**
  - Position sizing via Kelly criterion
  - Risk-adjusted sizing based on win rate & payoff ratio

### 3. **Strategies** (`strategies/`)
Nine independent strategy implementations:
- **momentum.py** — Price trend following
- **mean_reversion.py** — Price mean reversion
- **vwap_strategy.py** — Volume-weighted average price
- **supertrend.py** — Supertrend indicator
- **market_making.py** — Bid/ask spread capture
- **stat_arb.py** — Statistical arbitrage (pairs trading with Engle-Granger)
- **opening_range_breakout.py** — Open range capture
- **rsi_divergence.py** — RSI divergence trades
- **breakout.py** — Support/resistance breakouts

All inherit from `base_strategy.py` (signal generation, validation, position management).

### 4. **Data Feeds** (`data/feeds/`)
- **realtime_feed.py** — Yahoo Finance polling (15s cadence)
- **shoonya_feed.py** — Shoonya WebSocket (S-Mode: market hours only, fallback to Yahoo outside)
- **nse_option_chain.py** — NSE NIFTY option chain for Greeks & volatility
- **historical_feed.py** — OHLCV parquet cache (data/cache/)
- **fii_data.py** — FII/DII flows (NSE session-based)

### 5. **ML Models** (`models/`)
- **trainer.py**
  - XGBoost + LightGBM + ExtraTrees ensemble
  - Triple-barrier labeling for classification
  - Expected return scoring

- **predictor.py**
  - Inference on new data
  - Direction prediction + confidence scores
  - Cached predictions (1-minute TTL)

### 6. **Broker Integration** (`broker/`)
- **paper_broker.py** — Simulator (FIFO, realistic slippage)
- **shoonya_paper_broker.py** — S-Mode: Shoonya data + paper execution
- **shoonya_live_broker.py** — Live Shoonya (Finvasia)
- **angel_one.py** — Live Angel One broker
- **dual_broker.py** — Angel One data + Shoonya execution
- **hybrid_broker.py** — Angel One data + paper execution

All maintain consistent interface: `connect()`, `place_order()`, `cancel_order()`, `get_positions()`, `get_quote()`.

### 7. **Alerts** (`alerts/`)
- **telegram_bot.py**
  - Trade entry/exit notifications
  - Daily performance summaries
  - Risk warnings (gate blocks, drawdown alerts)

### 8. **News Feed** (`news/`)
- **feed_aggregator.py** — ET Markets + NSE news
- **sentiment_engine.py** — Sentiment scoring (fraud/SEBI flags)

---

## Data Flow

```
┌─────────────────────────────────────────────────────┐
│                    Market Data                       │
│  (Shoonya/Yahoo→RealtimeFeed, ParquetCache)        │
└────────────────┬────────────────────────────────────┘
                 │
        ┌────────▼────────┐
        │ Engine.py Loop  │◄── Config (settings.yaml)
        │  09:15–15:30    │◄── BotState (singleton)
        └────────┬────────┘
                 │
        ┌────────▼────────┐
        │  9 Strategies   │ (signal generation)
        └────────┬────────┘
                 │
        ┌────────▼────────┐
        │  Risk Engine    │ (13-gate validation)
        │  (Groq gates)   │
        └────────┬────────┘
                 │
        ┌────────▼────────┐
        │    Broker       │ (place/cancel orders)
        │  (Paper/Live)   │
        └────────┬────────┘
                 │
        ┌────────▼────────┐
        │  Telegram Alert │ (notifications)
        └─────────────────┘
```

---

## Trading Modes

| Mode | Data | Execution | Use Case |
|------|------|-----------|----------|
| **Paper** | Yahoo Finance 15s | Simulated | Backtesting, dry-run |
| **S-Mode** | Shoonya WS (market) / Yahoo (pre/post) | Simulated | Low-latency testing |
| **Hybrid** | Angel One WebSocket | Simulated | Angel One data validation |
| **Dual** | Angel One WebSocket | Shoonya Live | Real trading (Angel data) |
| **Live (S)** | Shoonya WebSocket | Shoonya Live | Real trading (Shoonya data) |

---

## State Management

```python
class BotState:
    capital: float              # Initial capital
    daily_pnl: float            # P&L since session start
    drawdown_pct: float         # (peak - current) / peak
    open_positions: Dict[str, PosDict]  # Symbol → position details
    daily_trades: int           # Trade count this session
    consecutive_losses: int     # Streak since last win
    market_data: Dict           # VIX, FII/DII, etc.
    is_halted: bool             # Manual halt flag
    status: str                 # "RUNNING" | "HALTED" | "WARMUP"
```

BotState is a singleton, accessed globally via `from core.state_manager import state_mgr`.

---

## Configuration

All runtime settings live in `config/settings.yaml`:
```yaml
capital: 100000
symbols: [AAPL, GOOGL, MSFT, ...]  # NSE/NIFTY symbols
strategies:
  momentum: { enabled: true, weight: 1.0 }
  mean_reversion: { enabled: true, weight: 0.8 }
risk:
  daily_loss_limit_pct: -3
  vix_halt_threshold: 25.0
  max_sector_exposure_pct: 50
```

Environment variables (`.env`):
```
GROQ_API_KEY=...
GROQ_ENABLED=true|false
SHOONYA_LOGIN=...
SHOONYA_PASSWORD=...
ANGEL_API_KEY=...
```

---

## Execution Flow (Daily Cycle)

1. **Pre-market (09:00)**
   - Initialize engine, load state
   - Generate session briefing via Groq

2. **Market open (09:15)**
   - Activate real-time feeds (Shoonya WS or Yahoo)
   - Start strategy signal generation loop

3. **Trading hours (09:15–15:30)**
   - Every tick: 9 strategies generate signals
   - Risk engine validates each signal (13 gates)
   - Accepted signals → positional orders (paper/live)
   - Market-making: constant bid/ask placement

4. **End-of-day (15:30)**
   - Close all open positions
   - Calculate daily PnL
   - Generate EOD report (Groq narrative)
   - Send Telegram summary

5. **Post-market**
   - Switch back to Yahoo polling (pre-market data)
   - Write state to disk (dailies, performance metrics)

---

## Extensibility Points

### Adding a New Strategy
1. Create `strategies/my_strategy.py` inheriting from `BaseStrategy`
2. Implement `generate_signal(ohlcv, state) → SignalDict`
3. Add to `config/settings.yaml` with weight
4. Engine auto-loads via factory pattern

### Adding a New Broker
1. Create `broker/my_broker.py` inheriting from `BaseBroker`
2. Implement required interface: `connect()`, `place_order()`, `get_positions()`, etc.
3. Register in `broker/factory.py`
4. Update config to point to new broker

### Adding Risk Gates
1. Create new validation method in `risk/risk_engine.py`
2. Add to `evaluate_gates()` pipeline
3. Return `RiskEvaluation(passed=bool, reason=str, severity=str)`

---

## Performance & Optimization

- **Caching**: 5-minute TTL for Groq outputs; 1-minute for ML predictions
- **Async**: All I/O operations use asyncio Tasks
- **Batch processing**: Options chains, FII data fetched once per cycle
- **Parquet cache**: OHLCV data persisted locally for fast backtests

---

## Monitoring & Health

Check system health:
```bash
python healthcheck.py
```

Logs location:
- `logs/trades/` — order execution logs
- `logs/signals/` — strategy signals
- `logs/errors/` — runtime errors

---

## Testing

Run safety tests:
```bash
python test_critical_fixes.py
python test_system_boot.py
```

Backtesting:
```bash
python run_backtest.py
```

---

## Dependencies

| Module | Version | Purpose |
|--------|---------|---------|
| pandas | 2.x | Time series, OHLCV |
| numpy | 1.x | Numerical computing |
| xgboost | 2.x | Model training |
| lightgbm | 4.x | Model training |
| scikit-learn | 1.x | Preprocessing, ensemble |
| requests | 2.x | HTTP, broker APIs |
| pytz | 2024.x | TZ handling (IST) |
| pyyaml | 6.x | Config parsing |
| aiohttp | 3.x | Async HTTP |

---

## Known Limitations & Future Work

- No backtesting framework (stub exists in `backtester/`)
- S-Mode: token lookup for all 30 symbols pending
- News: non-universe symbols may flood during high volatility
- ML: model retraining interval (currently daily)

See ROADMAP in main README for future enhancements.
