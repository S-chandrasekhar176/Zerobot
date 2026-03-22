# CLAUDE.md — ZeroBot G3 ML Upgraded
## How to Use Claude Effectively on This Codebase

> Read this before asking Claude any ZeroBot question.
> Following this guide cuts token usage by 60–70% per session.

---

## 1. CODEBASE MAP

```
zb_final/
├── core/
│   ├── engine.py          # 1800+ lines — main trading loop, all tasks orchestrated here
│   ├── config.py          # Typed settings loader (settings.yaml + .env)
│   ├── state_manager.py   # BotState singleton — capital, pnl, open_positions
│   ├── groq_brain.py      # LLM calls: session brief, trade narrative, exit advice
│   ├── regime_detector.py # BULL/BEAR/CRISIS/DEFENSIVE regime classifier
│   └── events_calendar.py # NSE earnings/RBI/expiry event risk multipliers
│
├── risk/
│   ├── risk_engine.py     # ★ 13-gate validator, multi-VaR, CVaR, stress tests, Greeks
│   ├── groq_gates.py      # Groq LLM gates 6 & 11 (ML conf + news sentiment)
│   └── kelly_sizer.py     # Kelly fraction position sizing
│
├── strategies/            # 9 strategies — all inherit base_strategy.py
│   ├── momentum.py
│   ├── mean_reversion.py
│   ├── vwap_strategy.py
│   ├── market_making.py
│   ├── stat_arb.py        # Pairs: sector filter + correlation pre-gate
│   ├── supertrend.py
│   ├── opening_range_breakout.py
│   ├── rsi_divergence.py
│   └── breakout.py
│
├── data/feeds/
│   ├── realtime_feed.py   # Yahoo Finance polling (15s) — paper/hybrid mode
│   ├── shoonya_feed.py    # ★ S-Mode: Shoonya WS during mkt hours, Yahoo outside
│   ├── nse_option_chain.py# NSE option chain — NIFTY spot fixed (^NSEI)
│   ├── historical_feed.py # OHLCV parquet cache
│   └── fii_data.py        # FII/DII — NSE session cookie handshake
│
├── models/
│   ├── trainer.py         # XGBoost+LightGBM+ExtraTrees ensemble training
│   └── predictor.py       # Inference: direction + confidence
│
├── news/
│   └── feed_aggregator.py # ET Markets + NSE feed
│
├── broker/
│   ├── paper_broker.py         # Paper trading simulation
│   ├── shoonya_paper_broker.py # ★ S-Mode — Shoonya data + PaperBroker exec
│   ├── shounya.py              # Live Shoonya (Finvasia) broker
│   ├── angel_one.py            # Live Angel One broker
│   ├── dual_broker.py          # Angel One data + Shoonya execution
│   └── hybrid_broker.py        # Angel One data + paper execution
│
├── alerts/telegram_bot.py # All Telegram notifications
├── config/settings.yaml   # Risk params, symbols list, strategy toggles
└── config/.env            # API keys (never commit)
```

---

## 2. TRADING MODES

| Mode            | bot.mode  | broker.name      | Data Source              | Execution        |
|-----------------|-----------|------------------|--------------------------|------------------|
| **Paper**       | `paper`   | `paper`          | Yahoo Finance 15s        | Paper (simulated)|
| **S-Mode** ★   | `s_mode`  | `shoonya_paper`  | Shoonya WS / Yahoo fallback | Paper (simulated)|
| Angel Sim       | `paper`   | `angel_paper_sim`| Yahoo Finance 15s        | Paper (simulated)|
| Hybrid          | `hybrid`  | `hybrid`         | Angel One WS             | Paper (simulated)|
| Dual            | `live`    | `dual`           | Angel One WS             | Shoonya (real)   |
| Live Shoonya    | `live`    | `shoonya`        | Shoonya WS               | Shoonya (real)   |

### S-Mode — Market Hours Behavior
```
09:15 IST → Market opens     : ShoonyaRealtimeFeed switches from Yahoo → Shoonya WS
15:30 IST → Market closes    : Shoonya WS goes quiet → Yahoo Finance polling resumes
Pre/Post market              : Yahoo Finance 15s polling (automatic fallback)
If Shoonya disconnects mid-day: Yahoo fallback kicks in automatically
```

---

## 3. BUGS FIXED (complete history)

| Bug | File | Fixed in |
|-----|------|---------|
| `get_portfolio_risk` AttributeError | risk/risk_engine.py | G2 |
| EOD report double-fire at 15:30 | core/engine.py | G2 |
| Option chain spot=0 when market closed | data/feeds/nse_option_chain.py | G3-O1 |
| VIX gate using hardcoded 15.0 | risk/risk_engine.py | H4 |
| NoneType burst on Yahoo rate limit | data/feeds/realtime_feed.py | Parquet cache |
| Daily PnL/peak_capital carrying across sessions | core/state_manager.py | P7 |
| Sector map missing new symbols | risk/risk_engine.py | H3 |
| Shoonya `totp_key` vs `totp_secret` naming | core/config.py | P16 |
| VIX=21 blocking ALL trades (threshold=20) | risk/risk_engine.py | S1 |
| NIFTY spot=0 (Yahoo used NIFTY.NS not ^NSEI) | data/feeds/nse_option_chain.py | S1 |
| StatArb spurious pairs (RELIANCE/NESTLEIND etc) | strategies/stat_arb.py | S1 |
| FII data always 0 (NSE needs session cookie) | data/feeds/fii_data.py | S1 |
| S-Mode using WS outside market hours | data/feeds/shoonya_feed.py | S1 |
| **NorenRestApiPy missing → WS silent crash** | broker/shounya.py | **G3-ML** |
| **subscribe_ticks no None guard → AttributeError** | broker/shounya.py | **G3-ML** |

---

## 4. S-MODE SHOONYA FIX (G3-ML)

**Problem**: If `NorenRestApiPy` is not installed, `self.api = None` silently,
then `subscribe_ticks()` crashes with `AttributeError: 'NoneType' object has no attribute 'start_websocket'`.
The bot falls back to Yahoo but the error message was confusing.

**Fix in `broker/shounya.py`**:
1. `connect()` now **auto-installs** `NorenRestApiPy pyotp` via `pip` if missing.
2. `subscribe_ticks()` raises a clear `RuntimeError` (not AttributeError) when `self.api is None`,
   which `shoonya_feed.py` catches and converts cleanly to Yahoo fallback.

**Manual fix if auto-install fails**:
```bash
pip install NorenRestApiPy pyotp
```
Then restart ZeroBot. The WS ticks will activate automatically next session.

---

## 5. VIX GATE — TIERED LOGIC (as of S1)

```
VIX ≤ 20            → Gate PASSES, full position size
20 < VIX ≤ 25       → Gate PASSES, confidence penalty 0-20pts (Kelly sizes down)
VIX > 25            → Gate BLOCKS — hard halt, no new trades
```
Setting in settings.yaml: `vix_halt_threshold: 25.0`
Soft warn at 80% of hard halt (20.0).

---

## 6. STAT ARB PAIR FILTERS

Two gates applied before expensive Engle-Granger test:
1. **Sector gate** — pairs must be in the same sector group (BANK, IT, NBFC, etc.)
2. **Correlation gate** — Pearson correlation ≥ 0.65 on closing prices

Valid pair examples: HDFCBANK/ICICIBANK, TCS/INFY, BAJFINANCE/BAJAJFINSV

---

## 7. PENDING GAPS

| Gap | Priority | File to change |
|-----|----------|---------------|
| S-Mode: token lookup for all 30 symbols | MEDIUM | broker/shoonya_paper_broker.py |
| No backtesting framework | MEDIUM | backtester/engine.py (stub exists) |
| ML checklist warning fires before training completes | LOW | main.py startup checklist |
| News non-universe symbols flooding | LOW | news/feed_aggregator.py NSE filter |

---

## 8. HOW TO ASK CLAUDE EFFICIENTLY

```
Context: ZeroBot G3 ML, mode=paper (or s_mode), Python 3.12.
File: [filename] (~N lines)
Problem: [specific problem]
Show me only the changed function, not the whole file.
```
**Golden rule: paste only the relevant function (20–80 lines), not the whole file.**

---

## 9. GROQ TOKEN BUDGET

```python
_BUDGET_PER_SESSION = 40   # max Groq calls per trading day
_CACHE_TTL_SECS     = 300  # 5-min cache
_TIMEOUT_S          = 4.0
```
Typical day: 15–25 calls. Disable with `GROQ_ENABLED=false` in `.env`.

---

## 10. RISK ENGINE — 13 GATES

```
1.  Halted          — bot not manually halted
2.  Mkt Hours       — 9:30–15:15 IST only
3.  Daily Loss      — daily PnL > -3% capital
4.  Pos Count       — dynamic: ₹55k→8 positions
5.  Loss Streak     — consecutive losses < 3
6.  ML Conf         — confidence ≥ 62% (or Groq override)
7.  VIX             — TIERED: pass+penalise 20-25, hard halt >25
8.  Margin          — capital ≥ position_inr × 1.20
9.  Sector          — sector exposure ≤ 50% capital
10. Correlation     — max 40% of positions in same sector
11. News            — no fraud/SEBI hard block
12. StratCircuit    — strategy not halted by per-strategy circuit breaker
13. PortfolioHeat   — total portfolio VaR ≤ 5% capital
```

---

## 11. STATE FIELDS (BotState)

```python
state.capital             # float — configured capital
state.daily_pnl           # float — P&L since session start
state.drawdown_pct        # (peak - current) / peak * 100
state.open_positions      # Dict[symbol, pos_dict]
state.daily_trades        # int
state.consecutive_losses  # int — reset on any win
state.market_data         # Dict — {"india_vix": float, ...}
state.is_halted           # bool
state.status              # "RUNNING" | "HALTED" | "WARMUP"
```

---

## 12. ARCHITECTURE INVARIANTS (don't break these)

1. **BotState is a singleton** — always `from core.state_manager import state_mgr`
2. **RiskEngine is synchronous** — `evaluate()` is blocking; Groq runs in ThreadPoolExecutor
3. **Paper broker is always safe** — never sends real orders
4. **ShoonyaPaperBroker is safe** — only DATA comes from Shoonya; orders go to PaperBroker
5. **Engine tasks start in parallel** — asyncio Tasks, not threads
6. **OHLCV cache first** — check `data/cache/ohlcv/*.parquet` before Yahoo fetch
7. **Config is immutable at runtime** — restart required for any settings.yaml change
8. **VIX gate is tiered** — don't revert to binary; Kelly handles sizing at elevated VIX

---

## 13. QUICK HEALTH CHECK COMMANDS

```bash
# Syntax check all Python files
python3 -c "import ast, pathlib; errors=[]; [errors.append(str(f)) for f in pathlib.Path('.').rglob('*.py') if '__pycache__' not in str(f) and not (lambda: ast.parse(f.read_text(encoding='utf-8')))()]; print('✅ OK' if not errors else errors)"

# Check S-Mode imports
python3 -c "from broker.shoonya_paper_broker import ShoonyaPaperBroker; from data.feeds.shoonya_feed import ShoonyaRealtimeFeed; print('✅ S-Mode ok')"

# Spot-check Shoonya broker
python3 -c "from broker.shounya import ShounyaBroker; print('✅ ShounyaBroker ok')"
```

---

## 14. MODE EVOLUTION ROADMAP

```
Paper Mode ──► S-Mode ──► Dual Mode
(Yahoo 15s)    (Shoonya WS   (Angel One historical +
               market hours,  Shoonya live execution)
               Yahoo outside) ← activate when Angel One ready
```

---

*Last updated: G3-ML — Shoonya auto-install fix · WS None guard · duplicate file cleanup*
