"""
ZeroBot — Backtesting Engine
Walk-forward backtest with realistic costs, slippage, and position sizing.
Computes: Sharpe, Sortino, Max Drawdown, Win Rate, Profit Factor.
"""
import numpy as np
import pandas as pd
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from core.logger import log
from execution.transaction_cost import CostCalculator
from data.processors.indicator_engine import IndicatorEngine


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    qty: int = 1
    pnl: float = 0.0
    net_pnl: float = 0.0
    costs: float = 0.0
    strategy: str = ""


@dataclass
class BacktestResult:
    total_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    total_trades: int
    wins: int
    losses: int
    avg_trade_pnl: float
    equity_curve: List[float]
    trades: List[BacktestTrade]
    monthly_returns: Dict[str, float]
    start_capital: float
    end_capital: float
    start_date: str
    end_date: str


class BacktestEngine:
    """
    Vectorized backtesting engine.
    Simulates strategy on historical data with all costs.
    """

    def __init__(self, initial_capital: float = 10000.0):
        self._capital = initial_capital
        self._ie = IndicatorEngine()
        self._cost_calc = CostCalculator()

    def run(
        self,
        df: pd.DataFrame,
        strategy,
        symbol: str = "NIFTY",
        position_size_pct: float = 20.0,   # % of capital per trade
    ) -> BacktestResult:
        """Run backtest for one strategy on one symbol."""

        log.info(f"🔄 Backtesting {strategy.name} on {symbol} | {len(df)} candles")

        # Add indicators
        df = self._ie.add_all(df)

        capital = self._capital
        equity_curve = [capital]
        trades: List[BacktestTrade] = []
        open_trade: Optional[BacktestTrade] = None
        monthly: Dict[str, float] = {}

        for i in range(50, len(df)):
            window = df.iloc[:i+1]
            row = df.iloc[i]
            cmp = row["close"]
            ts = row.name if hasattr(row.name, 'strftime') else datetime.now()

            # Check if we should close existing trade (stop or target)
            if open_trade:
                atr = row.get("ATRr_14", cmp * 0.01)
                stop = open_trade.entry_price - 1.5 * atr if open_trade.side == "BUY" else open_trade.entry_price + 1.5 * atr
                target = open_trade.entry_price + 3 * atr if open_trade.side == "BUY" else open_trade.entry_price - 3 * atr

                should_close = False
                if open_trade.side == "BUY" and (cmp <= stop or cmp >= target):
                    should_close = True
                elif open_trade.side == "SELL" and (cmp >= stop or cmp <= target):
                    should_close = True

                if should_close:
                    open_trade.exit_price = cmp
                    open_trade.exit_time = ts
                    costs = self._cost_calc.compute("SELL", open_trade.qty, cmp)
                    gross = (cmp - open_trade.entry_price) * open_trade.qty
                    if open_trade.side == "SELL":
                        gross = -gross
                    net = gross - costs["total"] - open_trade.costs
                    open_trade.pnl = gross
                    open_trade.net_pnl = net
                    open_trade.costs += costs["total"]
                    capital += net
                    trades.append(open_trade)
                    open_trade = None

                    # Monthly tracking
                    month = ts.strftime("%Y-%m") if hasattr(ts, 'strftime') else "2024-01"
                    monthly[month] = monthly.get(month, 0) + net

            # Generate new signal (only if no open trade)
            if not open_trade:
                signal = strategy.generate_signal(window, symbol)
                if signal and signal.confidence >= 65.0:
                    qty = max(1, int(capital * position_size_pct / 100 / cmp))
                    buy_costs = self._cost_calc.compute("BUY", qty, cmp)
                    required = qty * cmp + buy_costs["total"]

                    if capital >= required:
                        open_trade = BacktestTrade(
                            symbol=symbol,
                            side=signal.side,
                            entry_time=ts,
                            entry_price=cmp,
                            qty=qty,
                            costs=buy_costs["total"],
                            strategy=signal.strategy,
                        )
                        capital -= required

            equity_curve.append(capital)

        # Close any remaining trade at end
        if open_trade and not df.empty:
            last_price = df.iloc[-1]["close"]
            gross = (last_price - open_trade.entry_price) * open_trade.qty
            if open_trade.side == "SELL":
                gross = -gross
            open_trade.exit_price = last_price
            open_trade.net_pnl = gross - open_trade.costs
            open_trade.pnl = gross
            trades.append(open_trade)
            capital += gross - open_trade.costs

        return self._compute_metrics(
            trades=trades,
            equity_curve=equity_curve,
            monthly=monthly,
            start_capital=self._capital,
            end_capital=capital,
            df=df,
        )

    def _compute_metrics(
        self, trades, equity_curve, monthly, start_capital, end_capital, df
    ) -> BacktestResult:
        """Compute all performance metrics."""
        returns = pd.Series(equity_curve).pct_change().dropna()

        # Sharpe (annualized, assuming ~252 trading days, but using candle frequency)
        if returns.std() > 0:
            sharpe = (returns.mean() / returns.std()) * np.sqrt(252)
        else:
            sharpe = 0.0

        # Sortino (downside deviation only)
        downside = returns[returns < 0]
        if len(downside) > 0 and downside.std() > 0:
            sortino = (returns.mean() / downside.std()) * np.sqrt(252)
        else:
            sortino = 0.0

        # Max Drawdown
        eq = pd.Series(equity_curve)
        roll_max = eq.cummax()
        drawdown = (eq - roll_max) / roll_max * 100
        max_dd = abs(drawdown.min())

        # Trade stats
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        win_rate = len(wins) / len(trades) * 100 if trades else 0

        gross_profit = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

        total_return = (end_capital - start_capital) / start_capital * 100
        avg_trade = np.mean([t.net_pnl for t in trades]) if trades else 0

        log.info(
            f"✅ Backtest done | Return={total_return:.1f}% | Sharpe={sharpe:.2f} | "
            f"MaxDD={max_dd:.1f}% | WinRate={win_rate:.1f}% | Trades={len(trades)}"
        )

        return BacktestResult(
            total_return_pct=round(total_return, 2),
            sharpe_ratio=round(sharpe, 3),
            sortino_ratio=round(sortino, 3),
            max_drawdown_pct=round(max_dd, 2),
            win_rate=round(win_rate, 2),
            profit_factor=round(profit_factor, 3),
            total_trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            avg_trade_pnl=round(avg_trade, 2),
            equity_curve=equity_curve,
            trades=trades,
            monthly_returns=monthly,
            start_capital=start_capital,
            end_capital=round(end_capital, 2),
            start_date=str(df.index[0])[:10] if not df.empty else "",
            end_date=str(df.index[-1])[:10] if not df.empty else "",
        )
