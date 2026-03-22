# -*- coding: utf-8 -*-
"""
ZeroBot G2 — Risk Engine [G2-Enhanced / 13-Gate]
═════════════════════════════════════════════════
ENHANCEMENTS vs patch11:
  Gate 12 (NEW): Per-strategy circuit breaker
  Gate 13 (NEW): Portfolio heat — blocks when total VaR > limit

  Multi-factor VaR: Historical + Parametric + Monte Carlo
  CVaR / Expected Shortfall
  Liquidity-adjusted VaR
  Greeks tracking for option positions
  Portfolio stress tests (3 NSE scenarios)
  Scenario analysis (bull/base/bear)
  get_portfolio_risk() extended for EOD report

  All 11 original gates preserved and unchanged.
"""
from __future__ import annotations
import math, random, statistics, time as _time
from dataclasses import dataclass, field
from datetime import time
from typing import Dict, List, Optional, Tuple

from core.config import cfg
from core.logger import log
from core.clock import now_ist

SESSION_MULTIPLIERS = {
    "discovery": 0.6, "trending": 1.0,
    "consolidation": 0.8, "closing": 0.5, "after_hours": 0.0,
}

_SECTOR_MAP_BASE = {
    "Banking":   {"HDFCBANK.NS","ICICIBANK.NS","SBIN.NS","AXISBANK.NS",
                  "KOTAKBANK.NS","BANDHANBNK.NS","INDUSINDBK.NS"},
    "IT":        {"TCS.NS","INFY.NS","WIPRO.NS","HCLTECH.NS","TECHM.NS"},
    "FMCG":      {"HINDUNILVR.NS","NESTLEIND.NS","ITC.NS"},
    "Finance":   {"BAJFINANCE.NS","BAJAJFINSV.NS"},
    "Infra":     {"LT.NS","NTPC.NS","POWERGRID.NS"},
    "Commodity": {"TATASTEEL.NS","ONGC.NS","TATAMOTORS.NS"},
    "Consumer":  {"ASIANPAINT.NS","ULTRACEMCO.NS","MARUTI.NS","TITAN.NS"},
    "Energy":    {"SUNPHARMA.NS","ADANIENT.NS"},   # [MEDIUM#13] Added new symbols
    "Telecom":   {"BHARTIARTL.NS"},                # [MEDIUM#13] Added telecom sector
}

def _build_sector_map() -> dict:
    result = {k: set(v) for k, v in _SECTOR_MAP_BASE.items()}
    try:
        assigned = {s for syms in result.values() for s in syms}
        for sym in cfg.symbols:
            if sym.startswith("^"): continue
            if sym not in assigned:
                result.setdefault("Other", set()).add(sym)
    except Exception:
        pass
    return result

SECTOR_MAP = _build_sector_map()

def _get_session() -> Tuple[str, float]:
    now = now_ist().time()
    if time(9,30)  <= now < time(10,30):  return "discovery",     0.6
    if time(10,30) <= now < time(13,30):  return "trending",      1.0
    if time(13,30) <= now < time(14,30):  return "consolidation", 0.8
    if time(14,30) <= now < time(15,15):  return "closing",       0.5
    return "after_hours", 0.0


@dataclass
class TradeSignal:
    symbol: str; side: str; strategy: str; confidence: float; trigger: str
    atr: Optional[float] = None; cmp: Optional[float] = None
    suggested_qty: Optional[int] = None


@dataclass
class RiskResult:
    approved: bool; recommended_qty: int; position_size_inr: float
    stop_loss: float; target: float; risk_reward: str; rr_ratio: float
    blocked_reason: str = ""; sentiment_score: float = 0.0
    session: str = "trending"; gates_passed: int = 0


@dataclass
class VaRResult:
    """Multi-factor VaR snapshot."""
    historical_var:   float = 0.0
    parametric_var:   float = 0.0
    montecarlo_var:   float = 0.0
    cvar:             float = 0.0   # Expected Shortfall
    liquidity_adj:    float = 0.0   # Liquidity-adjusted VaR
    final_var:        float = 0.0   # max(H, P, MC)
    confidence_level: float = 0.95
    method:           str   = "multi-factor"


@dataclass
class StressResult:
    scenario:      str
    shock_pct:     float
    estimated_pnl: float
    var_breach:    bool


@dataclass
class GreeksExposure:
    total_delta: float = 0.0
    total_gamma: float = 0.0
    total_vega:  float = 0.0
    total_theta: float = 0.0
    net_premium: float = 0.0
    positions:   int   = 0


@dataclass
class StrategyCircuitState:
    daily_losses:     int   = 0
    daily_pnl:        float = 0.0
    halted:           bool  = False
    halt_reason:      str   = ""
    max_daily_losses: int   = 3


class DrawdownGuard:
    def __init__(self, max_drawdown_pct: float = 20.0):
        self.max_drawdown_pct = max_drawdown_pct

    def check(self, state) -> tuple:
        dd = getattr(state, "drawdown_pct", 0.0)
        if dd >= self.max_drawdown_pct:
            return False, f"Drawdown {dd:.1f}% >= limit {self.max_drawdown_pct:.1f}%"
        return True, f"Drawdown OK ({dd:.1f}% / {self.max_drawdown_pct:.1f}%)"

    def is_breached(self, state) -> bool:
        ok, _ = self.check(state)
        return not ok

    def update(self, total_capital: float, risk_engine=None):
        pass  # no-op shim


class RiskEngine:

    # ── PORTFOLIO_VAR_LIMIT: halt new trades if total book VaR > X% of capital
    PORTFOLIO_VAR_LIMIT_PCT: float = 5.0

    def __init__(self, state_manager=None, news_aggregator=None):
        from core.state_manager import state_mgr as _sm
        self._sm   = state_manager or _sm
        self._news = news_aggregator
        self._returns_cache: Dict[str, List[float]] = {}

        # Per-strategy circuit breakers
        self._strategy_circuits: Dict[str, StrategyCircuitState] = {}

        # Portfolio VaR cache (refreshed every 5-min candle)
        self._pvar_cache: Optional[float] = None
        self._pvar_ts:    float = 0.0

        # Options Greeks registry
        self._option_positions: Dict[str, dict] = {}

        log.info("RiskEngine initialized (13 gates | multi-VaR | CVaR | stress | Greeks)")

    @property
    def _capital(self): return self._sm.state.capital

    def set_news_aggregator(self, agg):
        self._news = agg
        log.info("RiskEngine: Gate 11 (News) connected")

    # ─────────────────────────────────────────────────────────────────────────
    # NEW: PER-STRATEGY CIRCUIT BREAKERS
    # ─────────────────────────────────────────────────────────────────────────
    def record_strategy_trade(self, strategy: str, pnl: float):
        """Call after every trade close. Halts strategy after max_daily_losses."""
        if strategy not in self._strategy_circuits:
            self._strategy_circuits[strategy] = StrategyCircuitState()
        sc = self._strategy_circuits[strategy]
        sc.daily_pnl += pnl
        if pnl < 0:
            sc.daily_losses += 1
            if sc.daily_losses >= sc.max_daily_losses and not sc.halted:
                sc.halted = True
                sc.halt_reason = (
                    f"{strategy} circuit open: {sc.daily_losses} losses "
                    f"(Rs{sc.daily_pnl:+.0f} today)"
                )
                log.warning(f"[CIRCUIT] 🔴 {sc.halt_reason}")
        else:
            sc.daily_losses = 0  # win resets streak

    def reset_strategy_circuits(self):
        self._strategy_circuits.clear()
        log.info("[CIRCUIT] All strategy circuit breakers reset for new session")

    def get_strategy_circuit_state(self, strategy: str) -> StrategyCircuitState:
        if strategy not in self._strategy_circuits:
            self._strategy_circuits[strategy] = StrategyCircuitState()
        return self._strategy_circuits[strategy]

    # ─────────────────────────────────────────────────────────────────────────
    # NEW: OPTIONS GREEKS REGISTRY
    # ─────────────────────────────────────────────────────────────────────────
    def register_option_position(self, symbol: str, delta: float, gamma: float,
                                  vega: float, theta: float, premium: float):
        self._option_positions[symbol] = {
            "delta": delta, "gamma": gamma, "vega": vega,
            "theta": theta, "premium": premium,
        }

    def remove_option_position(self, symbol: str):
        self._option_positions.pop(symbol, None)

    def get_greeks_exposure(self) -> GreeksExposure:
        exp = GreeksExposure()
        for pos in self._option_positions.values():
            exp.total_delta += pos.get("delta", 0.0)
            exp.total_gamma += pos.get("gamma", 0.0)
            exp.total_vega  += pos.get("vega",  0.0)
            exp.total_theta += pos.get("theta", 0.0)
            exp.net_premium += pos.get("premium", 0.0)
            exp.positions   += 1
        return exp

    # ─────────────────────────────────────────────────────────────────────────
    # NEW: MULTI-FACTOR VAR
    # ─────────────────────────────────────────────────────────────────────────
    def compute_var_multifactor(self, symbol: str, pos_inr: float,
                                 confidence: float = 0.95,
                                 volume_adv: Optional[float] = None) -> VaRResult:
        """
        Three-method VaR; final = max(Historical, Parametric, Monte Carlo).

        Historical  — empirical percentile, no distributional assumption
        Parametric  — Gaussian: pos_inr * (μ - z·σ)  z=1.645 @ 95%
        Monte Carlo — 1 000 random normal draws using empirical μ/σ
        CVaR        — mean loss in worst (1-confidence) tail (Expected Shortfall)
        Liquidity   — scales up when position > 2% of average daily volume
        """
        rets = self._returns_cache.get(symbol, [])
        z    = 1.645  # 95% one-tailed

        if len(rets) < 10:
            base = pos_inr * 0.015  # 1.5% conservative default
            hvar = pvar = mcvar = round(base * z, 2)
            cvar_val = round(hvar * 1.25, 2)
        else:
            arr  = rets[-252:]
            n    = len(arr)
            mu   = sum(arr) / n
            var_ = sum((r - mu) ** 2 for r in arr) / max(n - 1, 1)
            sig  = math.sqrt(var_) if var_ > 0 else 0.01
            sorted_rets = sorted(arr)

            # Historical
            idx  = max(0, int(n * (1 - confidence)) - 1)
            hvar = round(abs(min(sorted_rets[idx], 0)) * pos_inr, 2)

            # Parametric
            pvar = round(abs(mu - z * sig) * pos_inr, 2)

            # Monte Carlo (seeded for reproducibility within session)
            rng  = random.Random(42)
            losses = sorted(
                [-(mu + sig * rng.gauss(0, 1)) * pos_inr for _ in range(1000)],
                reverse=True
            )
            mc_idx = max(0, int(1000 * (1 - confidence)) - 1)
            mcvar  = round(max(0.0, losses[mc_idx]), 2)

            # CVaR (Expected Shortfall)
            tail_n = max(1, int(n * (1 - confidence)))
            cvar_val = round(
                sum(abs(r) * pos_inr for r in sorted_rets[:tail_n]) / tail_n, 2
            )

        final_var = max(hvar, pvar, mcvar)

        # Liquidity adjustment
        liq_var = final_var
        if volume_adv and volume_adv > 0:
            pos_pct = (pos_inr / max(volume_adv, 1)) * 100
            if pos_pct > 2.0:
                premium = 1.0 + (pos_pct - 2.0) * 0.05
                liq_var = round(final_var * premium, 2)

        return VaRResult(
            historical_var=hvar, parametric_var=pvar, montecarlo_var=mcvar,
            cvar=cvar_val, liquidity_adj=liq_var, final_var=final_var,
            confidence_level=confidence
        )

    def compute_var(self, symbol: str, pos_inr: float) -> Tuple[float, float]:
        """Legacy 2-tuple interface: (VaR, CVaR). Uses multi-factor internally."""
        r = self.compute_var_multifactor(symbol, pos_inr)
        return r.final_var, r.cvar

    # ─────────────────────────────────────────────────────────────────────────
    # NEW: STRESS TESTS
    # ─────────────────────────────────────────────────────────────────────────
    def run_stress_tests(self) -> List[StressResult]:
        """
        Three NSE-specific shock scenarios on current open book.
        Scenarios: Market Crash (-5%), VIX Spike (-3%), Bank Sector (-4%)
        """
        s        = self._sm.state
        open_pos = dict(getattr(s, "open_positions", {}))
        capital  = float(getattr(s, "capital", 1.0))
        loss_lim = capital * cfg.risk.max_daily_loss_pct / 100

        scenarios = [
            ("Market Crash (-5%)",       -0.05, None),
            ("VIX Spike (-3% all)",      -0.03, None),
            ("Bank Sector Shock (-4%)",  -0.04, "Banking"),
        ]
        results: List[StressResult] = []
        for name, shock, sector_filter in scenarios:
            pnl = 0.0
            for sym, pos in open_pos.items():
                pos_inr   = float(pos.get("position_inr", 0) or pos.get("value", 0))
                side      = pos.get("side", "BUY")
                if sector_filter:
                    sym_sec = next(
                        (sn for sn, syms in SECTOR_MAP.items() if sym in syms), "Other"
                    )
                    if sym_sec != sector_filter:
                        continue
                direction = 1 if side == "BUY" else -1
                pnl += direction * pos_inr * shock
            breach = abs(pnl) > loss_lim
            if breach:
                log.warning(
                    f"[STRESS] ⚠️  {name}: Rs{pnl:+.0f} breaches "
                    f"daily limit Rs{-loss_lim:.0f}"
                )
            results.append(StressResult(
                scenario=name, shock_pct=shock * 100,
                estimated_pnl=round(pnl, 2), var_breach=breach
            ))
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # NEW: SCENARIO ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────
    def scenario_analysis(self, symbol: str, pos_inr: float,
                           side: str = "BUY") -> dict:
        """Bull / Base / Bear P&L estimate for a potential new position."""
        rets = self._returns_cache.get(symbol, [])
        vol  = statistics.stdev(rets) if len(rets) > 5 else 0.015
        mult = 1 if side == "BUY" else -1
        return {
            "bull_pnl":  round(mult * pos_inr *  1.5 * vol, 2),
            "base_pnl":  0.0,
            "bear_pnl":  round(mult * pos_inr * -1.5 * vol, 2),
            "daily_vol_pct": round(vol * 100, 2),
            "bull_prob": 0.16,
            "bear_prob": 0.16,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # NEW: PORTFOLIO HEAT
    # ─────────────────────────────────────────────────────────────────────────
    def _portfolio_heat_pct(self) -> float:
        """Total portfolio VaR as % of capital. Cached 5 minutes."""
        now_ts = _time.time()
        if self._pvar_cache is not None and (now_ts - self._pvar_ts) < 300:
            return self._pvar_cache

        s        = self._sm.state
        open_pos = dict(getattr(s, "open_positions", {}))
        capital  = float(getattr(s, "capital", 1.0))
        total_var = sum(
            self.compute_var_multifactor(sym,
                float(pos.get("position_inr", 0) or pos.get("value", 0))).final_var
            for sym, pos in open_pos.items()
            if float(pos.get("position_inr", 0) or pos.get("value", 0)) > 0
        )
        heat = (total_var / max(capital, 1)) * 100
        self._pvar_cache = heat
        self._pvar_ts    = now_ts
        return heat

    # ─────────────────────────────────────────────────────────────────────────
    # GATES 1–13
    # ─────────────────────────────────────────────────────────────────────────
    def _g(self, name, ok, msg):
        return name, (ok, msg)

    def _run_gates(self, signal, cmp, est_pos_inr):
        sym  = signal.symbol
        side = signal.side
        s    = self._sm.state
        news = self._news

        def halted():
            return not s.is_halted, ("Running" if not s.is_halted else "Halted")

        def mkt_hours():
            from core.clock import session_status
            ss = session_status()
            if not ss["is_market_hours"]: return False, "Market closed"
            if ss.get("is_warmup"):       return False, "Warmup 9:15-9:30"
            return True, "Market open"

        def daily_loss():
            lim = self._capital * cfg.risk.max_daily_loss_pct / 100
            ok  = s.daily_pnl >= -lim
            return ok, f"Daily PnL Rs{s.daily_pnl:+.0f} (limit Rs{-lim:.0f})"

        def pos_count():
            c = len(s.open_positions); cap = self._capital
            if cap < 25_000:     dyn = 3
            elif cap < 50_000:   dyn = 5
            elif cap < 75_000:   dyn = 8
            elif cap < 1_50_000: dyn = 10
            elif cap < 3_00_000: dyn = 12
            elif cap < 7_50_000: dyn = 15
            else:                dyn = 20
            conf_boost = max(0, int((signal.confidence - 70) / 10))
            dyn = min(dyn + conf_boost, dyn + 3)
            return c < dyn, f"{c}/{dyn}"

        def loss_streak():
            st = s.consecutive_losses
            return st < cfg.risk.consecutive_loss_limit, f"Streak {st}"

        def ml_conf():
            if _groq_result is not None:
                return _groq_result.gate_6_pass, f"Groq ML {_groq_result.ml_conf:.2f}"
            return signal.confidence >= 62.0, f"Conf {signal.confidence:.1f}%"

        def vix():
            v = s.market_data.get("india_vix", 15.0)
            hard_halt = cfg.risk.vix_halt_threshold   # yaml default 25 — emergency stop
            soft_warn  = hard_halt * 0.80             # 80% of hard_halt = caution zone
            if v > hard_halt:
                return False, f"VIX {v:.1f} > {hard_halt:.0f} HALT"
            if v > soft_warn:
                # Elevated VIX: gate passes but confidence is penalised so Kelly sizes down
                penalty = int((v - soft_warn) / (hard_halt - soft_warn) * 20)  # 0-20 pts
                signal.confidence = max(30, signal.confidence - penalty)
                return True, f"VIX {v:.1f} elevated (conf-{penalty}%)"
            return True, f"VIX {v:.1f}"

        def margin():
            needed = est_pos_inr * (1 + cfg.risk.margin_buffer_pct / 100)
            return self._capital >= needed, f"Rs{self._capital:.0f} >= Rs{needed:.0f}"

        def sector():
            cap = self._capital * cfg.risk.max_sector_exposure_pct / 100
            sec = next((sn for sn, syms in SECTOR_MAP.items() if sym in syms), "Other")
            exp = sum(p.get("position_inr", 0)
                      for s2, p in s.open_positions.items()
                      if s2 in SECTOR_MAP.get(sec, set()))
            return exp + est_pos_inr <= cap, f"{sec} Rs{exp:.0f}+Rs{est_pos_inr:.0f}"

        def correlation():
            if sym in s.open_positions: return False, f"Already in {sym}"
            sec   = next((sn for sn, syms in SECTOR_MAP.items() if sym in syms), "Other")
            count = sum(1 for s2 in s.open_positions
                        if s2 in SECTOR_MAP.get(sec, set()))
            limit = max(3, int(cfg.risk.max_open_positions * 0.40))
            return count < limit, f"{count}/{limit} in {sec}"

        def news_gate():
            if _groq_result is not None:
                return _groq_result.gate_11_pass, f"Groq Sent {_groq_result.sentiment_score:+.2f}"
            if not news: return True, "No feed"
            blocked, reason = news.has_breaking_negative_news(sym)
            if blocked: return False, f"Hard block: {reason}"
            sc = float(news.get_sentiment_score(sym))
            if side == "BUY"  and sc <= -0.4: return False, f"Bearish news {sc:+.2f}"
            if side == "SELL" and sc >= +0.4: return False, f"Bullish news {sc:+.2f}"
            return True, f"News {sc:+.2f}"

        # ── Gate 12: Per-strategy circuit breaker ─────────────────────────────
        def strategy_circuit():
            sc = self.get_strategy_circuit_state(signal.strategy)
            if sc.halted:
                return False, sc.halt_reason
            return True, f"{signal.strategy} OK ({sc.daily_losses}/{sc.max_daily_losses} losses)"

        # ── Gate 13: Portfolio heat ────────────────────────────────────────────
        def portfolio_heat():
            heat  = self._portfolio_heat_pct()
            limit = self.PORTFOLIO_VAR_LIMIT_PCT
            return heat <= limit, f"PortVaR {heat:.1f}%/{limit:.1f}%"

        # ── GROQ: evaluate gates 6 & 11 ───────────────────────────────────────
        _groq_result = None
        try:
            from risk.groq_gates import get_groq_evaluator
            _groq_ev = get_groq_evaluator()
            if _groq_ev and _groq_ev.is_available:
                _headline = ""
                if news and hasattr(news, "get_headlines_for_symbol"):
                    _hl = news.get_headlines_for_symbol(sym)
                    _headline = _hl[0]["title"] if _hl else ""
                elif news and hasattr(news, "get_latest_headline"):
                    _headline = news.get_latest_headline(sym) or ""
                _vix_now = self._sm.state.market_data.get("india_vix", 16.0)
                log.info(
                    f"[GROQ] 🔮 {sym} {side} | conf={signal.confidence:.1f}% | "
                    f"vix={_vix_now:.1f} | headline={_headline[:60]!r}"
                )
                import concurrent.futures as _cf, functools as _ft
                _fn = _ft.partial(
                    _groq_ev.evaluate_sync, symbol=sym, signal=side,
                    base_conf=signal.confidence / 100.0,
                    vix=_vix_now, news_headline=_headline,
                )
                with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
                    _groq_result = _ex.submit(_fn).result(timeout=4.0)
                signal.confidence = _groq_result.ml_conf * 100.0
                log.info(
                    f"[GROQ] ✅ {sym} {side} | "
                    f"G6={'✅' if _groq_result.gate_6_pass else '❌'} | "
                    f"G11={'✅' if _groq_result.gate_11_pass else '❌'} | "
                    f"conf={_groq_result.ml_conf:.2f} | {_groq_result.latency_ms}ms"
                )
            else:
                log.debug(f"[GROQ] Skipped for {sym} — using local gates")
        except Exception as _ge:
            # [HIGH#5] Escalate permanent auth failures, keep transient as warnings
            if any(code in str(_ge) for code in ["401", "403", "403", "Unauthorized", "Forbidden"]):
                log.error(f"[GROQ] ❌ PERMANENT AUTH FAILURE: {_ge}")
            else:
                log.warning(f"[GROQ] ⚠️ Temporary failure, using local gates: {_ge}")
            _groq_result = None

        return [
            ("Halted",        halted()),
            ("Mkt Hours",     mkt_hours()),
            ("Daily Loss",    daily_loss()),
            ("Pos Count",     pos_count()),
            ("Loss Streak",   loss_streak()),
            ("ML Conf",       ml_conf()),
            ("VIX",           vix()),
            ("Margin",        margin()),
            ("Sector",        sector()),
            ("Correlation",   correlation()),
            ("News",          news_gate()),
            ("StratCircuit",  strategy_circuit()),   # Gate 12 NEW
            ("PortfolioHeat", portfolio_heat()),     # Gate 13 NEW
        ]

    # ─────────────────────────────────────────────────────────────────────────
    # SIZING (unchanged from patch11)
    # ─────────────────────────────────────────────────────────────────────────
    def _dynamic_rr(self, adx):
        if adx and adx > 30:  return 3.0, "3:1"
        if adx and adx >= 20: return 2.0, "2:1"
        return 1.5, "1.5:1"

    def _win_rate(self, symbol, strategy):
        try:
            trades = self._sm.get_closed_trades(symbol=symbol, strategy=strategy, limit=100)
            if len(trades) >= 20:
                return round(sum(1 for t in trades if t.get("net_pnl", 0) > 0) / len(trades), 3)
        except Exception:
            pass
        return 0.55

    def calculate_position_size(self, cmp, atr=None, win_rate=0.55,
                                  adx=None, sentiment=0.0, session="trending",
                                  stop_pct=1.5, volume_adv=None):
        risk_inr  = self._capital * cfg.risk.max_per_trade_risk_pct / 100
        stop_dist = (atr * stop_pct) if (atr and atr > 0) else (cmp * 0.015)
        rr, rr_lbl = self._dynamic_rr(adx)
        kelly_f   = max(0.01, min(0.25, win_rate - (1 - win_rate) / rr))
        qty = min(
            max(1, int(risk_inr / stop_dist)),
            max(1, int(self._capital * (kelly_f / 4) / cmp)),
            max(1, int(self._capital * cfg.risk.max_single_stock_pct / 100 / cmp))
        )
        sess_mult = SESSION_MULTIPLIERS.get(session, 1.0)
        qty = max(1, int(qty * sess_mult))
        if sentiment >= 0.5:    qty = max(1, int(qty * 1.20))
        elif sentiment <= -0.5: qty = max(1, int(qty * 0.50))
        if volume_adv and volume_adv > 0:
            adv_cap = max(1, int(volume_adv * 0.05))
            if qty > adv_cap:
                log.debug(f"ADV cap: qty {qty}→{adv_cap}")
                qty = adv_cap
        return {
            "qty": qty, "position_inr": round(qty * cmp, 2),
            "stop_loss":      round(cmp - stop_dist, 2),
            "target":         round(cmp + stop_dist * rr, 2),
            "stop_distance":  round(stop_dist, 2),
            "max_loss_inr":   round(qty * stop_dist, 2),
            "risk_reward": rr_lbl, "rr_ratio": rr,
            "kelly_fraction": round(kelly_f / 4, 4),
            "session": session, "session_mult": sess_mult,
        }

    def update_returns_cache(self, symbol: str, daily_returns: List[float]):
        self._returns_cache[symbol] = daily_returns
        self._pvar_cache = None  # invalidate portfolio VaR cache

    def update_after_trade(self, pnl: float, strategy: str = ""):
        s = self._sm.state
        if pnl > 0:
            s.daily_wins = getattr(s, "daily_wins", 0) + 1
            s.consecutive_losses = 0
        else:
            s.daily_losses = getattr(s, "daily_losses", 0) + 1
            s.consecutive_losses = getattr(s, "consecutive_losses", 0) + 1
        s.update_pnl(pnl)
        if strategy:
            self.record_strategy_trade(strategy, pnl)

    # ─────────────────────────────────────────────────────────────────────────
    # get_portfolio_risk — EXTENDED for EOD report
    # ─────────────────────────────────────────────────────────────────────────
    def get_portfolio_risk(self) -> dict:
        """
        Full EOD risk snapshot.
        Now includes: multi-factor VaR, CVaR, stress tests, Greeks, circuit states.
        """
        s             = self._sm.state
        capital       = float(getattr(s, "capital", 0))
        daily_pnl     = float(getattr(s, "daily_pnl", 0))
        
        # [HIGH#10] Wrap property access in try/except to prevent crashes
        try:
            drawdown_pct  = float(s.drawdown_pct) if hasattr(s, "drawdown_pct") else 0.0
        except Exception:
            drawdown_pct  = 0.0
        
        open_pos      = dict(getattr(s, "open_positions", {}))
        daily_trades  = int(getattr(s, "daily_trades", 0))
        daily_wins    = int(getattr(s, "daily_wins", 0))
        daily_losses  = int(getattr(s, "daily_losses", 0))
        consec_losses = int(getattr(s, "consecutive_losses", 0))
        vix           = float((getattr(s, "market_data", {}) or {}).get("india_vix", 0.0))

        win_rate = (daily_wins / daily_trades * 100) if daily_trades > 0 else 0.0
        pnl_pct  = (daily_pnl / capital * 100) if capital > 0 else 0.0

        # Aggregate VaR & CVaR across open positions
        total_var = total_cvar = 0.0
        for sym, pos in open_pos.items():
            pos_inr = float(pos.get("position_inr", 0) or pos.get("value", 0))
            if pos_inr > 0:
                vr = self.compute_var_multifactor(sym, pos_inr)
                total_var  += vr.final_var
                total_cvar += vr.cvar

        stress = self.run_stress_tests()
        greeks = self.get_greeks_exposure()
        circuit_summary = {
            st: {"losses": sc.daily_losses, "pnl": round(sc.daily_pnl, 2),
                 "halted": sc.halted}
            for st, sc in self._strategy_circuits.items()
        }

        return {
            # ── core ──────────────────────────────────────────────────────────
            "capital":            capital,
            "daily_pnl":          daily_pnl,
            "daily_pnl_pct":      round(pnl_pct, 2),
            "drawdown_pct":       round(drawdown_pct, 2),
            "open_positions":     len(open_pos),
            "open_symbols":       list(open_pos.keys()),
            "daily_trades":       daily_trades,
            "daily_wins":         daily_wins,
            "daily_losses":       daily_losses,
            "win_rate":           round(win_rate, 1),
            "consecutive_losses": consec_losses,
            "india_vix":          vix,
            # ── NEW: VaR / CVaR ───────────────────────────────────────────────
            "portfolio_var_inr":  round(total_var, 2),
            "portfolio_var_pct":  round(total_var / max(capital, 1) * 100, 2),
            "portfolio_cvar_inr": round(total_cvar, 2),
            # ── NEW: Stress tests ─────────────────────────────────────────────
            "stress_tests": [
                {"scenario": r.scenario, "shock_pct": r.shock_pct,
                 "estimated_pnl": r.estimated_pnl, "var_breach": r.var_breach}
                for r in stress
            ],
            # ── NEW: Greeks ───────────────────────────────────────────────────
            "greeks": {
                "delta": round(greeks.total_delta, 4),
                "gamma": round(greeks.total_gamma, 4),
                "vega":  round(greeks.total_vega,  4),
                "theta": round(greeks.total_theta, 4),
                "option_positions": greeks.positions,
            },
            # ── NEW: Per-strategy circuit states ──────────────────────────────
            "strategy_circuits": circuit_summary,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # evaluate — 13 gates
    # ─────────────────────────────────────────────────────────────────────────
    def evaluate(self, signal: TradeSignal, cmp: float,
                 adx: Optional[float] = None,
                 vix: Optional[float] = None) -> RiskResult:
        if vix is not None:
            if not hasattr(self._sm.state, "market_data"):
                self._sm.state.market_data = {}
            self._sm.state.market_data["india_vix"] = float(vix)

        sym       = signal.symbol
        sentiment = float(self._news.get_sentiment_score(sym)) if self._news else 0.0
        session, sess_mult = _get_session()
        win_rate  = self._win_rate(sym, signal.strategy)

        try:
            from core.events_calendar import events_calendar
            event_mult, event_reason = events_calendar.get_event_risk(sym)
            if event_mult == 0.0:
                return RiskResult(False, 0, 0, 0, 0, "N/A", 0,
                                  blocked_reason=f"[EventBlock] {event_reason}",
                                  sentiment_score=sentiment, session=session, gates_passed=0)
        except Exception:
            event_mult, event_reason = 1.0, None

        est   = self.calculate_position_size(cmp=cmp, atr=signal.atr,
                    win_rate=win_rate, adx=adx, sentiment=sentiment, session=session)
        gates  = self._run_gates(signal, cmp, est["position_inr"])
        failed = [(n, m) for n, (ok, m) in gates if not ok]
        passed = len(gates) - len(failed)

        if failed:
            n, m = failed[0]
            log.warning(f"RISK BLOCK {sym} {signal.side} -- [{n}] {m}")
            try:
                from core.state_manager import state_mgr
                import asyncio
                asyncio.get_event_loop().call_soon(
                    lambda: state_mgr._risk_blocks_mem.insert(0, {
                        "timestamp":  __import__("datetime").datetime.now().isoformat(),
                        "event_type": "RISK_BLOCK",
                        "symbol":     sym, "severity": "WARN",
                        "reason":     f"{signal.side} blocked: [{n}] {m}",
                    })
                )
            except Exception:
                pass
            return RiskResult(False, 0, 0, 0, 0, "N/A", 0,
                              blocked_reason=f"[{n}] {m}",
                              sentiment_score=sentiment, session=session,
                              gates_passed=passed)

        sizing = self.calculate_position_size(cmp=cmp, atr=signal.atr,
                    win_rate=win_rate, adx=adx, sentiment=sentiment, session=session)
        if event_mult < 1.0:
            sizing["qty"] = max(1, int(sizing["qty"] * event_mult))
            sizing["position_inr"] = round(sizing["qty"] * cmp, 2)
            log.info(f"EventCalendar: {sym} size reduced {event_mult:.0%} — {event_reason}")

        var_r = self.compute_var_multifactor(sym, sizing["position_inr"])

        log.info(
            f"RISK OK {sym} {signal.side} | {sizing['qty']}qty @Rs{cmp:.2f} "
            f"| RR {sizing['risk_reward']} | {session}({sess_mult:.0%}) "
            f"| Stop:Rs{sizing['stop_loss']:.2f} Tgt:Rs{sizing['target']:.2f} "
            f"| VaR(H/P/MC) Rs{var_r.historical_var:.0f}/{var_r.parametric_var:.0f}/"
            f"{var_r.montecarlo_var:.0f} CVaR:Rs{var_r.cvar:.0f} "
            f"| News:{sentiment:+.2f} | {passed}/13 gates ✅"
        )
        return RiskResult(True, sizing["qty"], sizing["position_inr"],
                          sizing["stop_loss"], sizing["target"],
                          sizing["risk_reward"], sizing["rr_ratio"],
                          sentiment_score=sentiment, session=session,
                          gates_passed=passed)
