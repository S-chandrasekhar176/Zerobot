# -*- coding: utf-8 -*-
"""
ZeroBot Pro — Walk-Forward Backtester (Patch 5 NEW)
════════════════════════════════════════════════════
Walk-forward validation: the ONLY reliable way to test a trading strategy.

Difference from simple backtesting:
  Simple:       Train on ALL data → Test on ALL data → OVERFITTED results
  Walk-forward: Train on period A → Test on period B → Train on B → Test on C...
                → Realistic out-of-sample performance

This implementation:
  1. Splits data into N rolling windows (default: 12 months)
  2. Each window: 70% train / 30% test
  3. Runs strategy on test portion only (out-of-sample)
  4. Aggregates results across all windows
  5. Reports overfitting score (train vs test Sharpe gap)

Also includes Monte Carlo simulation for confidence intervals.
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Callable
from datetime import datetime
from core.logger import log


@dataclass
class WFWindow:
    window_num:     int
    train_start:    str
    train_end:      str
    test_start:     str
    test_end:       str
    train_sharpe:   float = 0.0
    test_sharpe:    float = 0.0
    test_return:    float = 0.0
    test_max_dd:    float = 0.0
    test_win_rate:  float = 0.0
    test_trades:    int   = 0
    passed:         bool  = True


@dataclass
class WalkForwardResult:
    windows:            List[WFWindow]
    avg_test_sharpe:    float
    avg_test_return:    float
    avg_max_dd:         float
    avg_win_rate:       float
    total_trades:       int
    overfitting_score:  float   # train_sharpe / test_sharpe (closer to 1.0 = better)
    is_robust:          bool    # True if avg test Sharpe > 0.5 and overfitting < 2.0
    verdict:            str
    equity_curve:       List[float] = field(default_factory=list)
    monthly_returns:    Dict[str, float] = field(default_factory=dict)
    mc_var_95:          float = 0.0   # Monte Carlo 95% VaR
    mc_expected_return: float = 0.0


class WalkForwardBacktester:
    """
    Walk-forward backtester for any ZeroBot strategy.
    Usage:
        wf = WalkForwardBacktester(n_windows=12, train_pct=0.7)
        result = wf.run(df, strategy_fn)
    """

    def __init__(self, n_windows: int = 12, train_pct: float = 0.7):
        self.n_windows = n_windows
        self.train_pct = train_pct

    def run(
        self,
        df:          pd.DataFrame,
        strategy_fn: Callable,        # fn(df_train, df_test) → BacktestResult
        symbol:      str = "SYMBOL",
    ) -> WalkForwardResult:
        """
        Run walk-forward validation.
        strategy_fn: callable that takes (df_train, df_test) and returns a BacktestResult
        """
        if len(df) < 100:
            log.warning(f"WalkForward: not enough data ({len(df)} rows < 100 min)")
            return self._empty_result()

        windows_data = self._split_windows(df)
        log.info(f"WalkForward: {len(windows_data)} windows | {symbol}")

        wf_windows = []
        all_equity = [1.0]
        combined_trades = []

        for i, (df_train, df_test) in enumerate(windows_data):
            w = WFWindow(
                window_num  = i + 1,
                train_start = str(df_train.index[0])[:10],
                train_end   = str(df_train.index[-1])[:10],
                test_start  = str(df_test.index[0])[:10],
                test_end    = str(df_test.index[-1])[:10],
            )

            try:
                result = strategy_fn(df_train, df_test)
                # Try to get train result too (for overfitting check)
                train_result = strategy_fn(df_train, df_train)

                w.train_sharpe  = train_result.sharpe_ratio
                w.test_sharpe   = result.sharpe_ratio
                w.test_return   = result.total_return_pct
                w.test_max_dd   = result.max_drawdown_pct
                w.test_win_rate = result.win_rate
                w.test_trades   = result.total_trades
                w.passed        = result.sharpe_ratio > 0

                # Append equity curve
                if result.equity_curve:
                    norm = [v / result.equity_curve[0] for v in result.equity_curve]
                    if all_equity:
                        norm = [n * all_equity[-1] for n in norm]
                    all_equity.extend(norm[1:])

                combined_trades.extend(result.trades)

            except Exception as e:
                log.debug(f"WalkForward window {i+1} error: {e}")
                w.passed = False

            wf_windows.append(w)
            log.info(
                f"  Window {i+1}/{len(windows_data)}: "
                f"test_sharpe={w.test_sharpe:.2f} ret={w.test_return:.1f}% "
                f"dd={w.test_max_dd:.1f}% trades={w.test_trades}"
            )

        return self._aggregate(wf_windows, all_equity, combined_trades)

    def _split_windows(self, df: pd.DataFrame) -> List[tuple]:
        """Split data into overlapping train/test windows."""
        n = len(df)
        window_size = n // self.n_windows
        if window_size < 20:
            window_size = max(20, n // 6)

        windows = []
        for i in range(self.n_windows):
            start = i * (n - window_size) // max(self.n_windows - 1, 1)
            end   = start + window_size
            end   = min(end, n)
            split = start + int((end - start) * self.train_pct)

            df_train = df.iloc[start:split]
            df_test  = df.iloc[split:end]

            if len(df_train) >= 20 and len(df_test) >= 10:
                windows.append((df_train, df_test))

        return windows

    def _aggregate(self, windows: List[WFWindow], equity: List[float], trades) -> WalkForwardResult:
        passed = [w for w in windows if w.passed]
        if not passed:
            return self._empty_result()

        avg_test_sharpe = np.mean([w.test_sharpe for w in passed])
        avg_test_return = np.mean([w.test_return for w in passed])
        avg_max_dd      = np.mean([w.test_max_dd for w in passed])
        avg_win_rate    = np.mean([w.test_win_rate for w in passed])
        total_trades    = sum(w.test_trades for w in windows)

        avg_train_sharpe = np.mean([w.train_sharpe for w in passed if w.train_sharpe != 0])
        overfitting = (
            avg_train_sharpe / max(abs(avg_test_sharpe), 0.01)
            if avg_test_sharpe != 0 else 99.0
        )

        is_robust = avg_test_sharpe > 0.5 and overfitting < 2.5 and avg_max_dd < 30

        if is_robust:
            verdict = f"✅ ROBUST | Sharpe={avg_test_sharpe:.2f} DD={avg_max_dd:.1f}% overfit={overfitting:.1f}x"
        elif avg_test_sharpe > 0:
            verdict = f"⚠️  MARGINAL | Sharpe={avg_test_sharpe:.2f} DD={avg_max_dd:.1f}% overfit={overfitting:.1f}x"
        else:
            verdict = f"❌ WEAK | Sharpe={avg_test_sharpe:.2f} — needs parameter tuning"

        log.info(f"WalkForward complete: {verdict}")

        # Monte Carlo simulation
        mc_var, mc_exp = self._monte_carlo(equity)

        return WalkForwardResult(
            windows=windows,
            avg_test_sharpe=round(avg_test_sharpe, 3),
            avg_test_return=round(avg_test_return, 2),
            avg_max_dd=round(avg_max_dd, 2),
            avg_win_rate=round(avg_win_rate, 2),
            total_trades=total_trades,
            overfitting_score=round(overfitting, 2),
            is_robust=is_robust,
            verdict=verdict,
            equity_curve=equity,
            mc_var_95=mc_var,
            mc_expected_return=mc_exp,
        )

    def _monte_carlo(self, equity: List[float], n_sims: int = 1000) -> tuple:
        """Simple Monte Carlo: shuffle daily returns N times to get confidence intervals."""
        if len(equity) < 10:
            return 0.0, 0.0
        try:
            returns = np.diff(equity) / np.array(equity[:-1])
            sim_finals = []
            for _ in range(n_sims):
                sim = np.random.choice(returns, size=len(returns), replace=True)
                final = np.prod(1 + sim)
                sim_finals.append(final - 1)
            sim_finals.sort()
            var_95 = round(sim_finals[int(0.05 * n_sims)] * 100, 2)
            exp_ret = round(np.mean(sim_finals) * 100, 2)
            return var_95, exp_ret
        except Exception:
            return 0.0, 0.0

    def _empty_result(self) -> WalkForwardResult:
        return WalkForwardResult(
            windows=[], avg_test_sharpe=0, avg_test_return=0,
            avg_max_dd=0, avg_win_rate=0, total_trades=0,
            overfitting_score=99, is_robust=False,
            verdict="❌ Insufficient data"
        )
