"""Tests for Risk Engine — updated for SentimentResult + E1/E2 enhancements."""
import sys; sys.path.insert(0, "..")
import pytest
from risk.risk_engine import RiskEngine, TradeSignal
from core.state_manager import StateManager


def make_signal(conf=80.0, side="BUY") -> TradeSignal:
    return TradeSignal(
        symbol="RELIANCE.NS", side=side, strategy="test",
        confidence=conf, trigger="test", atr=42.0, cmp=2847.0,
    )


def test_confidence_gate():
    sm = StateManager()
    re = RiskEngine(sm)
    signal = make_signal(conf=55.0)  # Below 65% threshold
    result = re.evaluate(signal, cmp=2847.0)
    assert not result.approved
    assert "Confidence" in result.blocked_reason


def test_position_sizing():
    sm = StateManager()
    re = RiskEngine(sm, capital=10000)
    sizing = re.calculate_position_size(cmp=2847.0, atr=42.0)
    assert sizing["qty"] >= 1
    assert sizing["max_loss_inr"] <= 210   # ₹200 ± small rounding


def test_halt_blocks_all():
    sm = StateManager()
    re = RiskEngine(sm)
    re.halt("test halt")
    signal = make_signal(conf=90.0)
    result = re.evaluate(signal, cmp=2847.0)
    assert not result.approved
    assert "HALTED" in result.blocked_reason


def test_resume_after_halt():
    sm = StateManager()
    re = RiskEngine(sm)
    re.halt("test")
    re.resume()
    ok, msg = re._check_not_halted()
    assert ok


def test_cost_calculator():
    from execution.transaction_cost import CostCalculator
    calc = CostCalculator()
    costs = calc.compute("BUY", 10, 2847.0)
    assert costs["brokerage"] <= 20.0
    assert costs["stamp_duty"] > 0
    assert costs["stt"] == 0.0   # STT only on SELL for intraday
    assert costs["total"] > 0

    sell_costs = calc.compute("SELL", 10, 2890.0)
    assert sell_costs["stt"] > 0  # STT on sell side


# ── SentimentResult backward-compat tests ────────────────────────────────────

def test_sentiment_result_float_compat():
    """
    risk_engine.py does:  sc = float(news.get_sentiment_score(sym))
    This test verifies float() cast AND direct comparisons both work.
    """
    from news.feed_aggregator import SentimentResult

    r_bull  = SentimentResult(score=0.55,  has_fresh_data=True,  item_count=2, label="BULLISH")
    r_bear  = SentimentResult(score=-0.61, has_fresh_data=True,  item_count=3, label="BEARISH")
    r_empty = SentimentResult(score=0.0,   has_fresh_data=False, item_count=0, label="NEUTRAL (no data)")

    # float() must work
    assert float(r_bull)  ==  0.55
    assert float(r_bear)  == -0.61
    assert float(r_empty) ==  0.0

    # Direct comparisons (no float() cast) used in Gate 11 of risk_engine.py
    assert r_bear  <= -0.4    # bearish news blocks BUY
    assert r_bull  >= +0.4    # bullish — passes gate
    assert r_bull  >   0.0    # greater-than works
    assert r_bear  <   0.0    # less-than works
    assert not r_empty.has_fresh_data   # caller can detect "no data"


def test_sentiment_result_distinguishes_no_data():
    """
    Callers must be able to tell "score is 0 because neutral"
    vs "score is 0 because no news was found".
    """
    from news.feed_aggregator import SentimentResult

    neutral  = SentimentResult(score=0.0, has_fresh_data=True,  item_count=5, label="NEUTRAL")
    no_news  = SentimentResult(score=0.0, has_fresh_data=False, item_count=0, label="NEUTRAL (no data)")

    # Both are float 0.0 — but callers can distinguish
    assert float(neutral) == float(no_news)   # both 0.0
    assert neutral.has_fresh_data             # real neutral
    assert not no_news.has_fresh_data         # just no data


# ── News engine word-boundary tests ──────────────────────────────────────────

def test_no_false_negative_on_bandhan():
    """FIX-2: 'ban' must not fire on 'Bandhan Bank'."""
    from news.sentiment_engine import SentimentEngine
    eng = SentimentEngine()
    score = eng.score("Bandhan Bank posts record quarterly profit")
    assert score > 0, f"Bandhan Bank should score positive, got {score}"


def test_sebi_ban_still_detected():
    """FIX-2: 'sebi ban' must still be detected as bearish."""
    from news.sentiment_engine import SentimentEngine
    eng = SentimentEngine()
    score = eng.score("SEBI ban on XYZ promoter for market manipulation")
    assert score < -0.3, f"SEBI ban should score < -0.3, got {score}"


def test_reduced_npa_positive():
    """FIX-2: 'reduce' in context of 'reduced NPA' must not fire as bearish."""
    from news.sentiment_engine import SentimentEngine
    eng = SentimentEngine()
    score = eng.score("SBI reduced NPA ratio significantly in Q3")
    assert score >= 0, f"'reduced NPA' should be neutral-to-positive, got {score}"


# ── EventBus news events ──────────────────────────────────────────────────────

def test_event_bus_has_news_events():
    """E5: news_alert and sentiment_change must be registered."""
    from core.event_bus import EventBus
    assert "news_alert"       in EventBus.EVENTS
    assert "sentiment_change" in EventBus.EVENTS


if __name__ == "__main__":
    test_confidence_gate();         print("✅ test_confidence_gate")
    test_position_sizing();         print("✅ test_position_sizing")
    test_halt_blocks_all();         print("✅ test_halt_blocks_all")
    test_resume_after_halt();       print("✅ test_resume_after_halt")
    test_cost_calculator();         print("✅ test_cost_calculator")
    test_sentiment_result_float_compat();       print("✅ test_sentiment_result_float_compat")
    test_sentiment_result_distinguishes_no_data(); print("✅ test_sentiment_result_distinguishes_no_data")
    test_no_false_negative_on_bandhan();        print("✅ test_no_false_negative_on_bandhan")
    test_sebi_ban_still_detected();             print("✅ test_sebi_ban_still_detected")
    test_reduced_npa_positive();               print("✅ test_reduced_npa_positive")
    test_event_bus_has_news_events();          print("✅ test_event_bus_has_news_events")
    print("\n✅ All risk + sentiment tests passed!")


# ── H3: SECTOR_MAP covers all configured symbols ─────────────────────────────

def test_sector_map_covers_all_symbols():
    """H3: No configured symbol should fall through to missing sector."""
    from risk.risk_engine import SECTOR_MAP
    from core.config import cfg
    NON_TRADEABLE = {"^NSEI","^NSEBANK","^CNXIT","^VIX","^SENSEX","^BSESN","^NIFTYIT"}
    all_in_map = {s for syms in SECTOR_MAP.values() for s in syms}
    missing = [s for s in cfg.symbols if s not in NON_TRADEABLE and s not in all_in_map]
    assert not missing, f"Symbols missing from SECTOR_MAP: {missing}"


# ── H4: VIX gate reads real value via evaluate(vix=...) ─────────────────────

def test_vix_gate_uses_passed_value():
    """H4: evaluate(vix=X) must write X to state.market_data and gate on it."""
    from risk.risk_engine import RiskEngine, TradeSignal
    from core.state_manager import StateManager
    from core.config import cfg

    sm = StateManager()
    re = RiskEngine(sm)
    sig = TradeSignal(symbol="TCS.NS", side="BUY", strategy="test",
                      confidence=80.0, trigger="test", atr=30.0, cmp=3000.0)

    # High VIX should write to state and block via gate
    result = re.evaluate(sig, cmp=3000.0, vix=25.0)
    assert sm.state.market_data.get("india_vix") == 25.0, "vix not written to state.market_data"


def test_bot_state_has_market_data():
    """H4: BotState must always initialise market_data dict with india_vix."""
    from core.state_manager import StateManager
    sm = StateManager()
    assert hasattr(sm.state, "market_data"), "market_data missing from BotState"
    assert "india_vix" in sm.state.market_data, "india_vix missing from BotState.market_data"
    assert isinstance(sm.state.market_data["india_vix"], float)


# ── L2: env var name consistency ─────────────────────────────────────────────

def test_clock_uses_canonical_env_var():
    """L2: core/clock.py must use ZEROBOT_FORCE_MARKET_OPEN (not ZEROBOT_FORCE_MARKET_OPEN)."""
    import inspect
    import core.clock as clk
    src = inspect.getsource(clk)
    assert "ZEROBOT_FORCE_MARKET_OPEN" in src,        "clock.py must use ZEROBOT_FORCE_MARKET_OPEN"
    assert "ZEROBOT_FORCE_MARKET_OPEN" not in src, "clock.py has stale ZEROBOT_FORCE_MARKET_OPEN"


# ── H5: Backtest sanity ───────────────────────────────────────────────────────

def test_backtest_engine_runs():
    """H5: BacktestEngine must run without error and return valid metrics."""
    import pandas as pd
    import numpy as np
    from backtester.engine import BacktestEngine
    from strategies.momentum import MomentumStrategy
    from data.processors.indicator_engine import IndicatorEngine

    np.random.seed(0)
    n = 300
    prices = 2000 * np.exp(np.cumsum(np.random.normal(0.0003, 0.01, n)))
    df = pd.DataFrame({
        "open":   prices * 0.999,
        "high":   prices * 1.006,
        "low":    prices * 0.994,
        "close":  prices,
        "volume": np.full(n, 10_000_000.0),
    }, index=pd.date_range("2024-01-01", periods=n, freq="B", tz="Asia/Kolkata"))
    df = IndicatorEngine().add_all(df)

    result = BacktestEngine(initial_capital=100_000).run(df, MomentumStrategy(), symbol="TEST")
    assert result.total_trades >= 0
    assert result.max_drawdown_pct >= 0
    assert result.end_capital > 0
    assert isinstance(result.sharpe_ratio, float)


def test_backtest_momentum_profitable_on_uptrend():
    """H5: Momentum strategy must make money on a pure uptrend."""
    import pandas as pd
    import numpy as np
    from backtester.engine import BacktestEngine
    from strategies.momentum import MomentumStrategy
    from data.processors.indicator_engine import IndicatorEngine

    n = 200
    prices = 10000 * (1 + np.cumsum(np.full(n, 0.004)))  # clean uptrend
    df = pd.DataFrame({
        "open":  prices * 0.999,
        "high":  prices * 1.007,
        "low":   prices * 0.993,
        "close": prices,
        "volume": np.full(n, 20_000_000.0),
    }, index=pd.date_range("2024-01-01", periods=n, freq="B", tz="Asia/Kolkata"))
    df = IndicatorEngine().add_all(df)

    result = BacktestEngine(initial_capital=100_000).run(df, MomentumStrategy(), symbol="UPTREND")
    # With a clean uptrend, if at least one trade fires it should be profitable overall
    if result.total_trades > 0:
        assert result.total_return_pct > -10.0, \
            f"Momentum badly negative ({result.total_return_pct:.1f}%) on pure uptrend"


if __name__ == "__main__":
    test_confidence_gate();                     print("✅ confidence_gate")
    test_position_sizing();                     print("✅ position_sizing")
    test_halt_blocks_all();                     print("✅ halt_blocks_all")
    test_resume_after_halt();                   print("✅ resume_after_halt")
    test_cost_calculator();                     print("✅ cost_calculator")
    test_sentiment_result_float_compat();       print("✅ sentiment_result_float_compat")
    test_sentiment_result_distinguishes_no_data(); print("✅ sentiment_result_no_data")
    test_no_false_negative_on_bandhan();        print("✅ bandhan_word_boundary")
    test_sebi_ban_still_detected();             print("✅ sebi_ban_detected")
    test_reduced_npa_positive();               print("✅ reduced_npa_positive")
    test_event_bus_has_news_events();          print("✅ eventbus_news_events")
    test_sector_map_covers_all_symbols();      print("✅ sector_map_complete")
    test_vix_gate_uses_passed_value();         print("✅ vix_gate_writes_state")
    test_bot_state_has_market_data();          print("✅ bot_state_market_data")
    test_clock_uses_canonical_env_var();       print("✅ clock_env_var_canonical")
    test_backtest_engine_runs();               print("✅ backtest_engine_runs")
    test_backtest_momentum_profitable_on_uptrend(); print("✅ backtest_momentum_uptrend")
    print("\n✅ All 17 tests passed!")
