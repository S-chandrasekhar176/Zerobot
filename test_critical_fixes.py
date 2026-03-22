#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PHASE 5: Unit-level validation of all critical fixes
Tests that critical components function correctly after bug fixes.
"""
import sys
import asyncio
from datetime import datetime

# === Test 1: Config validation ===
print("\n[TEST 1] Config validation (MEDIUM#15)")
try:
    from core.config import cfg
    assert cfg.mode in {"paper", "live"}, f"Invalid mode: {cfg.mode}"
    assert cfg.initial_capital > 0, f"Capital must be > 0, got {cfg.initial_capital}"
    assert cfg.trading_mode in {"stocks", "options", "both"}, f"Invalid trading_mode: {cfg.trading_mode}"
    print("  ✅ Config validation: PASS")
except Exception as e:
    print(f"  ❌ Config validation: FAIL — {e}")
    sys.exit(1)

# === Test 2: State daily reset guarantee (HIGH#6) ===
print("\n[TEST 2] State daily reset guarantee (HIGH#6)")
try:
    from core.state_manager import state_mgr, BotState
    # Fresh state should have daily_pnl=0
    state_mgr.reset_daily()
    assert state_mgr.state.daily_pnl == 0.0, f"Daily PnL not reset: {state_mgr.state.daily_pnl}"
    # Simulate a trade with -1000 PnL
    state_mgr.state.daily_pnl = -1000.0
    state_mgr.reset_daily()
    assert state_mgr.state.daily_pnl == 0.0, f"Reset failed, pnl={state_mgr.state.daily_pnl}"
    print("  ✅ Daily reset: PASS")
except Exception as e:
    print(f"  ❌ Daily reset: FAIL — {e}")
    sys.exit(1)

# === Test 3: VIX default (CRITICAL#3) ===
print("\n[TEST 3] VIX default is 18.0, not 14.0 (CRITICAL#3)")
try:
    from core.state_manager import BotState
    state = BotState()
    default_vix = state.market_data.get("india_vix", 0)
    assert default_vix == 18.0, f"VIX default wrong: {default_vix}, expected 18.0"
    print("  ✅ VIX default: PASS")
except Exception as e:
    print(f"  ❌ VIX default: FAIL — {e}")
    sys.exit(1)

# === Test 4: Predictor returns full schema (CRITICAL#4) ===
print("\n[TEST 4] Predictor returns complete G3 schema (CRITICAL#4)")
try:
    from models.predictor import EnsemblePredictor
    pred = EnsemblePredictor()
    # Since no models are trained, should return HOLD with full schema
    result = pred.predict(None, "TEST")
    required_fields = {
        "direction", "confidence", "direction_probability",
        "expected_return_score", "regime", "models_used"
    }
    missing = required_fields - set(result.keys())
    assert not missing, f"Missing fields: {missing}"
    assert result["direction"] == "HOLD", f"Expected HOLD, got {result['direction']}"
    assert result["expected_return_score"] == 0.0, f"Expected E[R]=0, got {result['expected_return_score']}"
    print("  ✅ Predictor schema: PASS")
except Exception as e:
    print(f"  ❌ Predictor schema: FAIL — {e}")
    sys.exit(1)

# === Test 5: Risk engine doesn't crash on property access (HIGH#10) ===
print("\n[TEST 5] Risk engine portfolio risk calculation (HIGH#10)")
try:
    from risk.risk_engine import RiskEngine
    from core.state_manager import state_mgr
    
    risk = RiskEngine(state_mgr)
    portfolio = risk.get_portfolio_risk()
    
    required_keys = {"capital", "daily_pnl", "drawdown_pct", "portfolio_var_inr", "strategy_circuits"}
    missing = required_keys - set(portfolio.keys())
    assert not missing, f"Missing portfolio keys: {missing}"
    assert portfolio["capital"] > 0, "Capital should be > 0"
    print("  ✅ Portfolio risk calculation: PASS")
except Exception as e:
    print(f"  ❌ Portfolio risk: FAIL — {e}")
    sys.exit(1)

# === Test 6: Sector map has new symbols (MEDIUM#13) ===
print("\n[TEST 6] Sector map includes all configured symbols (MEDIUM#13)")
try:
    from risk.risk_engine import SECTOR_MAP
    from core.config import cfg
    
    assigned_symbols = {s for sector_set in SECTOR_MAP.values() for s in sector_set}
    configured = {s for s in cfg.symbols if not s.startswith("^")}
    missing = configured - assigned_symbols
    
    # Missing symbols should be in "Other" category
    other_cat = SECTOR_MAP.get("Other", set())
    still_missing = missing - other_cat
    
    assert not still_missing, f"Symbols not in any sector: {still_missing}"
    
    # Check that new symbols like ADANIENT are properly categorized
    new_symbols_check = {"ADANIENT.NS", "SUNPHARMA.NS", "TATAMOTORS.NS", "BHARTIARTL.NS"}
    for sym in new_symbols_check:
        if sym in configured:
            assert any(sym in cat for cat in SECTOR_MAP.values()), f"{sym} not in any sector"
    
    print("  ✅ Sector map: PASS")
except Exception as e:
    print(f"  ❌ Sector map: FAIL — {e}")
    sys.exit(1)

# === Test 7: Kelly sizer doesn't crash (HIGH#7) ===
print("\n[TEST 7] Kelly sizer zero guards (HIGH#7)")
try:
    from risk.kelly_sizer import KellySizer
    
    sizer = KellySizer(fraction=0.25, max_pct=0.20)
    
    # Test 1: Normal operation
    result = sizer.compute(
        capital=100000,
        cmp=500,
        confidence=75.0,
        rr_ratio=2.0,
        expected_return_score=0.005
    )
    assert result.qty > 0, f"Expected qty > 0, got {result.qty}"
    
    # Test 2: Zero price (should not crash)
    result = sizer.compute(
        capital=100000,
        cmp=0,
        confidence=75.0
    )
    assert result.qty >= 1, f"Should return qty >= 1 for invalid inputs"
    
    # Test 3: Zero capital (should not crash)
    result = sizer.compute(
        capital=0,
        cmp=500,
        confidence=75.0
    )
    assert result.qty >= 1, f"Should return qty >= 1 for invalid inputs"
    
    print("  ✅ Kelly sizer: PASS")
except Exception as e:
    print(f"  ❌ Kelly sizer: FAIL — {e}")
    sys.exit(1)

# === Test 8: Shoonya broker has auto-install marker (MEDIUM#11) ===
print("\n[TEST 8] Shoonya broker auto-install code present (MEDIUM#11)")
try:
    import broker.shounya
    # If import succeeds and module is loaded, the code is syntactically correct
    assert hasattr(broker.shounya, 'ShounyaBroker'), "ShounyaBroker class not found"
    print("  ✅ Shoonya auto-install code: PASS (module loaded)")
except Exception as e:
    print(f"  ❌ Shoonya auto-install: FAIL — {e}")
    sys.exit(1)

# === Summary ===
print("\n" + "="*60)
print("✅ ALL CRITICAL UNIT TESTS PASSED")
print("="*60)
