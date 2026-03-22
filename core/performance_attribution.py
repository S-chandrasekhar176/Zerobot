# -*- coding: utf-8 -*-
"""
ZeroBot G1 — Performance Attribution Engine
============================================
Tracks per-strategy Sharpe, alpha, win-rate, drawdown, and P&L by time-slot.
Answers: "Which strategies generate alpha vs which drag performance?"

METRICS (per strategy):
  • trades, wins, losses, win_rate
  • gross_pnl, net_pnl (after costs), max_win, max_loss
  • Sharpe ratio (annualised), max drawdown %
  • P&L by time-slot: morning (9:15-11:00), midday (11:00-13:30), afternoon (13:30-15:15)
  • avg holding time in minutes
"""
import math, datetime, logging
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class StrategyMetrics:
    strategy:      str
    trades:        int   = 0
    wins:          int   = 0
    losses:        int   = 0
    gross_pnl:     float = 0.0
    net_pnl:       float = 0.0
    max_win:       float = 0.0
    max_loss:      float = 0.0
    total_holding: float = 0.0   # sum of holding mins for avg
    morning_pnl:   float = 0.0
    midday_pnl:    float = 0.0
    afternoon_pnl: float = 0.0
    daily_returns: List[float] = field(default_factory=list)
    equity_curve:  List[float] = field(default_factory=lambda: [0.0])

    @property
    def win_rate(self) -> float:
        return self.wins / max(self.trades, 1)

    @property
    def avg_holding_mins(self) -> float:
        return self.total_holding / max(self.trades, 1)

    @property
    def sharpe(self) -> float:
        if len(self.daily_returns) < 3:
            return 0.0
        n    = len(self.daily_returns)
        mean = sum(self.daily_returns) / n
        var  = sum((r - mean)**2 for r in self.daily_returns) / max(n - 1, 1)
        std  = math.sqrt(var) if var > 0 else 0.0001
        return round((mean / std) * math.sqrt(252), 2)

    @property
    def max_drawdown(self) -> float:
        peak = dd = max_dd = 0.0
        for v in self.equity_curve:
            peak  = max(peak, v)
            dd    = (peak - v) / max(abs(peak), 1) * 100
            max_dd = max(max_dd, dd)
        return round(max_dd, 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy":         self.strategy,
            "trades":           self.trades,
            "wins":             self.wins,
            "losses":           self.losses,
            "win_rate_pct":     round(self.win_rate * 100, 1),
            "gross_pnl":        round(self.gross_pnl, 2),
            "net_pnl":          round(self.net_pnl, 2),
            "max_win":          round(self.max_win, 2),
            "max_loss":         round(self.max_loss, 2),
            "avg_holding_mins": round(self.avg_holding_mins, 1),
            "sharpe":           self.sharpe,
            "max_drawdown_pct": self.max_drawdown,
            "morning_pnl":      round(self.morning_pnl, 2),
            "midday_pnl":       round(self.midday_pnl, 2),
            "afternoon_pnl":    round(self.afternoon_pnl, 2),
        }


class PerformanceAttributionEngine:

    def __init__(self):
        self._metrics: Dict[str, StrategyMetrics] = {}
        self._trade_count = 0

    def _get(self, strategy: str) -> StrategyMetrics:
        if strategy not in self._metrics:
            self._metrics[strategy] = StrategyMetrics(strategy=strategy)
        return self._metrics[strategy]

    @staticmethod
    def _slot(t: datetime.datetime) -> str:
        h, m = t.hour, t.minute
        if (h, m) < (11, 0):  return "morning"
        if (h, m) < (13, 30): return "midday"
        return "afternoon"

    def record_trade(
        self,
        strategy: str,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        qty: int,
        gross_pnl: float,
        net_pnl: float,
        entry_time: Optional[datetime.datetime] = None,
        exit_time:  Optional[datetime.datetime] = None,
        confidence: float = 0.0,
    ):
        now = datetime.datetime.now()
        exit_time  = exit_time  or now
        entry_time = entry_time or now
        holding    = (exit_time - entry_time).total_seconds() / 60
        slot       = self._slot(exit_time)

        m = self._get(strategy)
        m.trades      += 1
        m.gross_pnl   += gross_pnl
        m.net_pnl     += net_pnl
        m.total_holding += holding
        m.equity_curve.append(m.equity_curve[-1] + net_pnl)
        if len(m.equity_curve) > 2000: m.equity_curve = m.equity_curve[-2000:]

        if net_pnl > 0:
            m.wins    += 1
            m.max_win  = max(m.max_win, net_pnl)
        else:
            m.losses  += 1
            m.max_loss = min(m.max_loss, net_pnl)

        if slot == "morning":   m.morning_pnl   += net_pnl
        elif slot == "midday":  m.midday_pnl    += net_pnl
        else:                   m.afternoon_pnl += net_pnl

        self._trade_count += 1
        log.debug(f"[ATTR] {strategy} | {symbol} {side} | ₹{net_pnl:+.2f} | WR={m.win_rate:.0%}")

    def add_daily_return(self, strategy: str, daily_pnl: float, capital: float):
        if capital > 0:
            m = self._get(strategy)
            m.daily_returns.append(daily_pnl / capital)
            if len(m.daily_returns) > 252: m.daily_returns = m.daily_returns[-252:]

    def get_report(self) -> List[Dict]:
        report = [m.to_dict() for m in self._metrics.values()]
        report.sort(key=lambda x: x["net_pnl"], reverse=True)
        return report

    def get_best_strategy(self) -> str:
        if not self._metrics: return "Unknown"
        return max(self._metrics.values(), key=lambda m: m.net_pnl).strategy

    def get_worst_strategy(self) -> str:
        if not self._metrics: return "Unknown"
        return min(self._metrics.values(), key=lambda m: m.net_pnl).strategy

    def get_strategy_stats(self, strategy: str) -> Dict[str, Any]:
        m = self._metrics.get(strategy)
        if m: return m.to_dict()
        return {"strategy": strategy, "trades": 0, "wins": 0, "losses": 0,
                "win_rate_pct": 0, "net_pnl": 0, "sharpe": 0}

    def get_summary(self) -> Dict[str, Any]:
        total_t = sum(m.trades for m in self._metrics.values())
        total_w = sum(m.wins   for m in self._metrics.values())
        total_p = sum(m.net_pnl for m in self._metrics.values())
        return {
            "total_trades":   total_t,
            "total_wins":     total_w,
            "overall_wr_pct": round(total_w/max(total_t,1)*100,1),
            "total_net_pnl":  round(total_p,2),
            "strategies":     len(self._metrics),
            "best":  self.get_best_strategy(),
            "worst": self.get_worst_strategy(),
        }

    def get_time_slot_analysis(self) -> Dict[str, Any]:
        m_pnl = a_pnl = n_pnl = 0.0
        for m in self._metrics.values():
            m_pnl += m.morning_pnl
            n_pnl += m.midday_pnl
            a_pnl += m.afternoon_pnl
        best = max([("morning",m_pnl),("midday",n_pnl),("afternoon",a_pnl)],
                   key=lambda x: x[1])[0]
        return {
            "morning_pnl":    round(m_pnl,2),
            "midday_pnl":     round(n_pnl,2),
            "afternoon_pnl":  round(a_pnl,2),
            "best_time_slot": best,
        }

# ── Singleton ─────────────────────────────────────────────────────────────────
attribution = PerformanceAttributionEngine()
