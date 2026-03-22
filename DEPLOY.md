# ZeroBot G2-Enhanced — Deployment Instructions

## Files changed (drop into your G2 folder):

| File | Destination | Change |
|------|-------------|--------|
| risk_engine.py | zerobot_G2/risk/risk_engine.py | REPLACE |
| nse_option_chain.py | zerobot_G2/data/feeds/nse_option_chain.py | REPLACE |
| CLAUDE.md | zerobot_G2/CLAUDE.md | NEW |
| test_enhanced_features.py | zerobot_G2/tests/test_enhanced_features.py | NEW |

## Verify after deploy:
```bash
cd zerobot_G2
python3 tests/test_enhanced_features.py
# Expected: ALL 66 TESTS PASSED
```

## What changed:

### risk/risk_engine.py (11 gates → 13 gates)
- Gate 12: Per-strategy circuit breaker (halts a strategy after 3 consecutive losses)
- Gate 13: Portfolio heat (blocks new trades when total VaR > 5% of capital)
- Multi-factor VaR: Historical + Parametric + Monte Carlo (final = max of all 3)
- CVaR / Expected Shortfall (avg loss in worst 5% tail)
- Liquidity-adjusted VaR (scales when position > 2% of avg daily volume)
- Greeks registry: track delta/gamma/vega/theta for all option positions
- Portfolio stress tests: Market Crash -5%, VIX Spike -3%, Bank Shock -4%
- Scenario analysis: bull/base/bear P&L for any new position
- get_portfolio_risk() extended with all new metrics for EOD Telegram report
- update_after_trade() now accepts strategy param → feeds circuit breaker
- All 11 original gates UNCHANGED

### data/feeds/nse_option_chain.py (spot=0 fix)
- _resolve_spot_price() with 4-level fallback:
  1. underlyingValue from API (normal)
  2. PCR put-call parity estimate from strike data
  3. Last known cached spot price
  4. Yahoo Finance yfinance fetch
- Fixes crashes/wrong output during pre-market and API rate-limit periods
