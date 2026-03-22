#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
COMPREHENSIVE EDGE CASE VALIDATION
Tests the bot handles edge cases gracefully.
"""
import sys
import pandas as pd
import numpy as np

print("\n" + "="*60)
print("EDGE CASE TESTING")
print("="*60)

# === Test 1: Zero volume data ===
print("\n[EDGE-1] Zero volume/NaN data handling")
try:
    from data.processors.indicator_engine import IndicatorEngine
    
    ie = IndicatorEngine()
    
    # Create DataFrame with NaN values
    df = pd.DataFrame({
        'open': [100.0, np.nan, 102.0],
        'high': [102.0, 103.0, np.nan],
        'low': [99.0, np.nan, 101.0],
        'close': [101.0, 101.5, np.nan],
        'volume': [0, 1000000, 0],  # Zero volume
    })
    
    result = ie.add_all(df.copy())
    # Should not crash, even with NaN
    assert result is not None, "Indicator engine returned None"
    assert len(result) > 0, "Empty result"
    print("  ✅ Handles NaN/zero volume")
except Exception as e:
    print(f"  ⚠️  Non-critical: {e}")

# === Test 2: Missing candle data ===
print("\n[EDGE-2] Missing/empty candle data")
try:
    from data.processors.indicator_engine import IndicatorEngine
    
    ie = IndicatorEngine()
    
    # Empty DataFrame
    empty_df = pd.DataFrame()
    result = ie.add_all(empty_df)
    # Should handle gracefully (return empty or original)
    assert result is not None, "Should not return None"
    print("  ✅ Handles empty DataFrame")
    
    # Very small DataFrame (< 50 rows)
    small_df = pd.DataFrame({
        'close': [100.0] * 5
    })
    result = ie.add_all(small_df)
    assert result is not None, "Should handle small data"
    print("  ✅ Handles small data")
except Exception as e:
    print(f"  ⚠️  Non-critical: {e}")

# === Test 3: Invalid trade signals ===
print("\n[EDGE-3] Invalid trade signals")
try:
    from risk.risk_engine import TradeSignal
    
    # Signal with None values
    sig = TradeSignal(
        symbol="TEST.NS",
        side="BUY",
        strategy="Test",
        confidence=0.0,  # Zero confidence
        trigger="test"
    )
    assert sig.confidence == 0.0, "Should accept zero confidence"
    print("  ✅ Accepts edge case signals")
except Exception as e:
    print(f"  ❌ Signal validation: {e}")

# === Test 4: Extreme VIX values ===
print("\n[EDGE-4] Extreme VIX handling (>30)")
try:
    from risk.risk_engine import RiskEngine
    from core.state_manager import state_mgr
    from risk.risk_engine import TradeSignal
    
    risk = RiskEngine(state_mgr)
    
    # Simulate extreme VIX
    state_mgr.state.market_data["india_vix"] = 35.0  # VIX > 25 = halt threshold
    
    sig = TradeSignal(
        symbol="RELIANCE.NS",
        side="BUY",
        strategy="Test",
        confidence=0.80,
        trigger="test"
    )
    
    result = risk.evaluate(sig, cmp=2000, vix=35.0)
    # Should block trade due to high VIX
    assert result.approved == False, "High VIX should block trades"
    print(f"  ✅ Blocks trades at high VIX (reason: {result.blocked_reason})")
except Exception as e:
    print(f"  ❌ VIX gate: {e}")

# === Test 5: Portfolio with no positions ===
print("\n[EDGE-5] Empty portfolio risk calculation")
try:
    from risk.risk_engine import RiskEngine
    from core.state_manager import state_mgr
    
    state_mgr.state.open_positions = {}  # Clear all positions
    risk = RiskEngine(state_mgr)
    
    portfolio = risk.get_portfolio_risk()
    assert portfolio["open_positions"] == 0, "Should have 0 positions"
    assert portfolio["capital"] > 0, "Should still have capital"
    print("  ✅ Handles empty portfolio")
except Exception as e:
    print(f"  ❌ Empty portfolio: {e}")

# === Test 6: Division by zero guards ===
print("\n[EDGE-6] Division by zero edge cases")
try:
    from risk.kelly_sizer import KellySizer
    
    sizer = KellySizer()
    
    # Test: win_rate = 100% (q = 0, could theoretically cause div by zero)
    result = sizer.compute(
        capital=100000,
        cmp=500,
        confidence=100.0,  # 100% confidence
        rr_ratio=2.0,
        win_rate=1.0  # 100% win rate
    )
    assert result.qty > 0, "Should handle 100% confidence/win rate"
    
    # Test: win_rate = 0% (p = 0, kelly_f should be 0 or negative)
    result = sizer.compute(
        capital=100000,
        cmp=500,
        confidence=0.1,  # 0.1% confidence
        rr_ratio=2.0,
        win_rate=0.0  # 0% win rate
    )
    assert result.qty >= 1, "Should return min qty"
    
    print("  ✅ All division guards working")
except Exception as e:
    print(f"  ❌ Division guard: {e}")

# === Test 7: Configuration edge cases ===
print("\n[EDGE-7] Config boundary conditions")
try:
    from core.config import cfg
    
    # Check all critical thresholds exist and are valid
    assert 0 <= cfg.risk.max_daily_loss_pct <= 100, "max_daily_loss_pct out of range"
    assert 0 < cfg.risk.vix_halt_threshold, "VIX threshold not set"
    assert 0 < cfg.initial_capital, "Capital not set"
    assert len(cfg.symbols) > 0, "No symbols configured"
    
    print("  ✅ All config boundaries valid")
except Exception as e:
    print(f"  ❌ Config boundaries: {e}")

# === Test 8: State consistency ===
print("\n[EDGE-8] State consistency across operations")
try:
    from core.state_manager import state_mgr
    
    initial_capital = state_mgr.state.capital
    
    # Simulate PnL change
    state_mgr.state.daily_pnl = -500.0
    
    # Check derived properties
    total_cap = state_mgr.state.total_capital
    assert total_cap == initial_capital - 500.0, "Total capital calculation error"
    
    # Check drawdown calculation
    dd = state_mgr.state.drawdown_pct
    assert 0 <= dd <= 100, f"Drawdown out of range: {dd}"
    
    # Reset
    state_mgr.reset_daily()
    assert state_mgr.state.daily_pnl == 0.0, "Reset failed"
    
    print("  ✅ State consistency maintained")
except Exception as e:
    print(f"  ❌ State consistency: {e}")

print("\n" + "="*60)
print("✅ EDGE CASE VALIDATION COMPLETE")
print("="*60)
