#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║          ZeroBot Pro — Walk-Forward Backtest Runner             ║
║                                                                  ║
║  HOW TO RUN:                                                     ║
║    python run_backtest.py                         ← all symbols  ║
║    python run_backtest.py RELIANCE                ← one symbol   ║
║    python run_backtest.py RELIANCE TCS INFY       ← 3 symbols    ║
║    python run_backtest.py --strategy supertrend   ← one strategy ║
║    python run_backtest.py --windows 12            ← more windows ║
║                                                                  ║
║  STRATEGIES:                                                     ║
║    momentum | mean_reversion | vwap | supertrend | all (default) ║
╚══════════════════════════════════════════════════════════════════╝
"""
import sys
import os
import argparse
from pathlib import Path
from datetime import datetime

# ── Make sure we can import ZeroBot modules ──────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

# Suppress noisy logs from ZeroBot during backtest
os.environ["ZEROBOT_QUIET"] = "1"

import pandas as pd
import numpy as np

# ── Argument parser ──────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="ZeroBot Walk-Forward Backtester")
parser.add_argument("symbols", nargs="*", help="NSE symbols to test (e.g. RELIANCE TCS INFY)")
parser.add_argument("--strategy", default="all",
                    choices=["momentum","mean_reversion","vwap","supertrend","all"],
                    help="Strategy to test (default: all)")
parser.add_argument("--windows",  type=int, default=8,  help="Walk-forward windows (default: 8)")
parser.add_argument("--period",   default="2y",          help="Data period: 1y, 2y, 3y (default: 2y)")
parser.add_argument("--capital",  type=float, default=55000, help="Starting capital in Rs (default: 55000)")
parser.add_argument("--interval", default="1d",          help="Candle interval: 1d, 1h (default: 1d)")
args = parser.parse_args()

# ── Default symbols if none given ────────────────────────────────────────────
DEFAULT_SYMBOLS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "SBIN.NS", "MARUTI.NS", "AXISBANK.NS", "WIPRO.NS", "BAJFINANCE.NS",
]

raw_syms = args.symbols if args.symbols else DEFAULT_SYMBOLS
# Auto-add .NS if missing
SYMBOLS = [s if "." in s else s + ".NS" for s in raw_syms]

# ── Imports ──────────────────────────────────────────────────────────────────
print("\n" + "═"*65)
print("  ZeroBot Pro v3.0 — Walk-Forward Backtest")
print(f"  Symbols  : {', '.join(SYMBOLS)}")
print(f"  Strategy : {args.strategy.upper()}")
print(f"  Windows  : {args.windows}  Period: {args.period}  Capital: ₹{args.capital:,.0f}")
print("═"*65 + "\n")

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: pip install yfinance")
    sys.exit(1)

try:
    from backtester.engine import BacktestEngine, BacktestResult
    from backtester.walk_forward import WalkForwardBacktester, WalkForwardResult
    from data.processors.indicator_engine import IndicatorEngine
except ImportError as e:
    print(f"ERROR importing ZeroBot modules: {e}")
    print("Make sure you are running from the ZeroBot folder:")
    print("  cd path/to/zero_pro_patch5")
    print("  python run_backtest.py")
    sys.exit(1)

# ── Load strategies ──────────────────────────────────────────────────────────
def get_strategies(name: str):
    strats = []
    if name in ("momentum", "all"):
        from strategies.momentum import MomentumStrategy
        strats.append(MomentumStrategy())
    if name in ("mean_reversion", "all"):
        from strategies.mean_reversion import MeanReversionStrategy
        strats.append(MeanReversionStrategy())
    if name in ("vwap", "all"):
        from strategies.vwap_strategy import VWAPStrategy
        strats.append(VWAPStrategy())
    if name in ("supertrend", "all"):
        from strategies.supertrend import SupertrendStrategy
        strats.append(SupertrendStrategy())
    return strats

strategies = get_strategies(args.strategy)

# ── Download data ────────────────────────────────────────────────────────────
print("📥 Downloading price data from Yahoo Finance...")
all_data = {}
for sym in SYMBOLS:
    try:
        raw = yf.download(sym, period=args.period, interval=args.interval,
                          progress=False, auto_adjust=True)
        if raw.empty:
            print(f"  ⚠  {sym}: no data — skipping")
            continue
        # Flatten MultiIndex columns if present
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() for c in raw.columns]
        else:
            raw.columns = [c.lower() for c in raw.columns]
        # Rename Volume column variants
        for vc in ["volume", "vol"]:
            if vc in raw.columns:
                raw.rename(columns={vc: "volume"}, inplace=True)
                break
        raw = raw.dropna(subset=["close"])
        all_data[sym] = raw
        print(f"  ✅ {sym}: {len(raw)} candles  ({str(raw.index[0])[:10]} → {str(raw.index[-1])[:10]})")
    except Exception as e:
        print(f"  ❌ {sym}: {e}")

if not all_data:
    print("\nNo data downloaded. Check internet connection.")
    sys.exit(1)

# ── Run walk-forward backtest ────────────────────────────────────────────────
print(f"\n🔄 Running walk-forward validation ({args.windows} windows each)...\n")

wf = WalkForwardBacktester(n_windows=args.windows, train_pct=0.70)
bt = BacktestEngine(initial_capital=args.capital)

summary_rows = []

for sym, df in all_data.items():
    for strategy in strategies:
        print(f"  Testing {strategy.name:20s} on {sym}...")
        try:
            result = wf.run(
                df=df,
                strategy_fn=lambda tr, te, s=strategy: bt.run(te, s, sym),
                symbol=sym,
            )

            row = {
                "Symbol":    sym.replace(".NS", ""),
                "Strategy":  strategy.name,
                "WF Sharpe": result.avg_test_sharpe,
                "Return %":  result.avg_test_return,
                "Max DD %":  result.avg_max_dd,
                "Win Rate":  result.avg_win_rate,
                "Trades":    result.total_trades,
                "Overfit":   result.overfitting_score,
                "Robust":    "✅ YES" if result.is_robust else "❌ NO",
                "Verdict":   result.verdict,
                "MC VaR95":  result.mc_var_95,
            }
            summary_rows.append(row)

            # Per-result output
            status = "✅" if result.is_robust else "⚠️ "
            print(f"     {status} Sharpe={result.avg_test_sharpe:+.2f} "
                  f"Ret={result.avg_test_return:+.1f}% "
                  f"DD={result.avg_max_dd:.1f}% "
                  f"WinRate={result.avg_win_rate:.0f}% "
                  f"Overfit={result.overfitting_score:.1f}x "
                  f"Trades={result.total_trades}")
        except Exception as e:
            print(f"     ❌ Error: {e}")

# ── Summary table ────────────────────────────────────────────────────────────
print("\n" + "═"*65)
print("  RESULTS SUMMARY")
print("═"*65)

if summary_rows:
    df_results = pd.DataFrame(summary_rows)

    # Sort by Sharpe descending
    df_results = df_results.sort_values("WF Sharpe", ascending=False)

    # Print nicely
    col_widths = {"Symbol":8,"Strategy":16,"WF Sharpe":10,"Return %":9,"Max DD %":9,
                  "Win Rate":9,"Trades":7,"Overfit":8,"Robust":8}
    header = "  " + "".join(k.ljust(v) for k,v in col_widths.items())
    print(header)
    print("  " + "-"*(sum(col_widths.values())))
    for _, r in df_results.iterrows():
        row_str = (
            f"  {r['Symbol']:<8}{r['Strategy']:<16}"
            f"{r['WF Sharpe']:+.2f}     "
            f"{r['Return %']:+.1f}%    "
            f"{r['Max DD %']:.1f}%    "
            f"{r['Win Rate']:.0f}%     "
            f"{int(r['Trades']):<7}"
            f"{r['Overfit']:.1f}x    "
            f"{r['Robust']}"
        )
        print(row_str)

    print("\n" + "═"*65)
    print("  TOP PERFORMERS (Sharpe > 0.5 = trade-worthy)")
    print("═"*65)
    top = df_results[df_results["WF Sharpe"] > 0.5].head(5)
    if top.empty:
        print("  None yet — try different strategy parameters or more data")
    else:
        for _, r in top.iterrows():
            print(f"  ★  {r['Symbol']} + {r['Strategy']}: {r['Verdict']}")

    print("\n  AVOID (Sharpe < 0 = consistent loser)")
    avoid = df_results[df_results["WF Sharpe"] < 0]
    if avoid.empty:
        print("  None — all strategies at least marginally positive")
    else:
        for _, r in avoid.iterrows():
            print(f"  ✗  {r['Symbol']} + {r['Strategy']}: {r['Verdict']}")

    # Save to CSV
    out_path = Path(__file__).parent / f"backtest_results_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    df_results.to_csv(out_path, index=False)
    print(f"\n  Results saved: {out_path.name}")

print("\n" + "═"*65)
print("  HOW TO READ THIS:")
print("    Sharpe > 1.0   = excellent  | 0.5-1.0 = good | <0.5 = weak")
print("    Overfit < 2.0  = not overfit (train vs test performance gap)")
print("    Robust = YES   = Sharpe>0.5 AND Overfit<2.5 AND MaxDD<30%")
print("    MC VaR95       = worst 5% outcome in Monte Carlo simulation")
print("═"*65 + "\n")
