# ZeroBot Z1 — Quick Start Guide

Get ZeroBot running in 5 minutes for paper trading.

---

## 1. Install (2 minutes)

```bash
# Clone
git clone https://github.com/yourusername/zerobot.git
cd zerobot

# Python 3.9+
python --version

# Virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install
pip install -r requirements.txt
```

---

## 2. Configure (2 minutes)

```bash
# Copy env template
cp config/.env.example config/.env

# Edit (minimal: leave everything empty for paper mode)
nano config/.env
```

That's it. `config/settings.yaml` already has default symbols and risk settings.

---

## 3. Run (1 minute)

```bash
# Paper trading (simulated, Yahoo Finance, zero risk)
python main.py

# OR use the example script
python examples/run_bot.py --mode paper
```

**Expected output**:
```
[09:15:23] Initializing ZeroBot Z1 in PAPER mode
[09:15:24] Loading config: 100000 capital, 9 strategies
[09:15:25] Market opened. Starting trading loop.
[09:16:12] [MOMENTUM] Signal LONG on RELIANCE @ ₹2,785
[09:16:12] Risk Engine: PASS (13/13 gates)
[09:16:13] [PAPER BROKER] BUY 10 shares RELIANCE @ ₹2,785
...
[15:30:00] Daily PnL: +₹2,345 (+2.35%)
```

---

## Health Check

Verify everything is working:

```bash
python healthcheck.py
```

Should show ✅ for all 10 checks.

---

## Next Steps

### Test Strategies
```bash
python examples/test_strategy.py momentum --symbol RELIANCE --bars 100
```

### Review Configuration
```bash
cat config/settings.yaml    # Edit symbols, risk params
cat config/.env             # Add API keys when ready
```

### Explore Modes

| Mode | Data | Execution | When to Use |
|------|------|-----------|-----------|
| **Paper** | Yahoo (15s) | Simulated | Testing, default |
| **S-Mode** | Shoonya WS | Simulated | Low-latency testing |
| **Hybrid** | Angel One | Simulated | Angel data testing |
| **Dual** | Angel One | Shoonya Live | Real trading (Angel data) |
| **Live (S)** | Shoonya WS | Shoonya Live | Real trading (Shoonya data) |

For S-Mode:
```bash
# Add to config/.env
SHOONYA_LOGIN=your_username
SHOONYA_PASSWORD=your_password
SHOONYA_TOTP_SECRET=your_totp  # optional

# Run
python main.py --mode s_mode
```

### Run Tests

```bash
python test_system_boot.py        # Check startup
python test_critical_fixes.py     # Regression tests
python test_edge_cases.py         # Edge case handling
```

---

## Troubleshooting

### Bot won't start
```bash
# Check Python version
python --version  # Should be 3.9+

# Check imports
python -c "from core.engine import TradingEngine; print('OK')"

# Check logs
tail -f logs/errors/errors.log
```

### No signals
1. Market hours: 09:15–15:30 IST only (Mon–Fri)
2. Check logs: `tail -f logs/signals/signals.log`
3. Verify symbols in `config/settings.yaml`
4. Check strategy enabled: `strategies.momentum.enabled: true`

### Slow performance
- Reduce symbol count in `settings.yaml`
- Disable less-used strategies
- Check `VERBOSE_LOGGING=false` in `.env`

---

## Documentation

- [Full README](README.md) — Project overview, features, security
- [docs/usage.md](docs/usage.md) — Detailed setup, configuration, examples
- [docs/architecture.md](docs/architecture.md) — System design, components
- [CONTRIBUTING.md](CONTRIBUTING.md) — How to contribute, add strategies

---

## Support

- **Issues**: [GitHub Issues](https://github.com/yourrepo/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourrepo/discussions)
- **Docs**: [docs/](docs/)

---

**Happy trading! 🚀**
