# ZeroBot v2.1 — NSE India Algorithmic Trading System

## Quick Start

```bash
# Normal start (market hours: 9:15–3:25 IST)
python -X utf8 main.py

# Test suite (any day, no market hours required)
python test_bot.py

# Full end-to-end simulation (holiday / weekend)
python test_bot.py --simulate

# Windows — double-click
start.bat        # run bot
run_check.bat    # run tests
FIX_NOW.bat      # first-time setup
```

Dashboard: **http://127.0.0.1:8000**

---

## Paper vs Live

One line in `config/settings.yaml`:

```yaml
bot:
  mode: "paper"   # change to "live" for real Angel One orders
```

Everything else — 11-gate risk engine, ML ensemble, news sentiment, strategies,
stop/target monitoring — runs identically in both modes.

---

## Architecture

```
NSE/RSS headline
  → SentimentEngine.score()          (FinBERT + keyword rules)
  → NewsFeedAggregator               (instant callback if |score| >= 0.4)
  → Strategy.generate_signal()       (Momentum / MeanReversion / VWAP / StatArb)
  → RiskEngine.evaluate()            (11-gate pre-trade validation)
      1.  Not halted
      2.  Market hours 9:30-15:15
      3.  Daily loss < 3%
      4.  Open positions < 8
      5.  Loss streak < 3
      6.  ML confidence >= 65%
      7.  India VIX < 20           ← reads live ^VIX price via realtime feed
      8.  Margin available
      9.  Sector exposure < 50%    ← auto-maps ALL configured symbols to sectors
      10. No duplicate symbol
      11. News sentiment gate
  → broker.place_order()             (PaperBroker or AngelOneBroker)
  → _stop_target_loop every 5s
  → _auto_squareoff at 3:15 PM
```

---

## Configuration

```yaml
# config/settings.yaml
capital:
  initial: 25000          # INR

risk:
  max_open_positions: 8
  max_daily_loss_pct: 3.0
  vix_halt_threshold: 20.0

bot:
  mode: "paper"           # "paper" or "live"
```

**Restart required after any change.**

---

## Paper → Live Transition

After 2+ weeks of profitable paper trading:

1. Get Angel One SmartAPI credentials
2. Add to `config/.env`:
   ```
   ANGEL_API_KEY=...
   ANGEL_CLIENT_ID=...
   ANGEL_PASSWORD=...
   ANGEL_TOTP_SECRET=...
   ```
3. Set `mode: live` in `settings.yaml`
4. Run `python test_bot.py --simulate` to verify
5. Start with `python -X utf8 main.py`

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
