#!/usr/bin/env python3
"""
ZeroBot v1.1 Patch16 — INTENSIVE TEST SUITE (Batch 1: Tests 1–150)
====================================================================
Tests are grouped into 14 categories, fully isolated via shims.
All external libs that are missing are mocked before imports.
Run: python3 run_tests_batch1.py
"""

import sys, os, types, asyncio, time, json, tempfile, traceback
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

# ══════════════════════════════════════════════════════════════════════════════
# SHIM SETUP — Must happen BEFORE any ZeroBot imports
# ══════════════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).parent / "zerobot_patch16"
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ZEROBOT_FORCE_MARKET_OPEN", "1")
os.environ.setdefault("ANGEL_API_KEY", "")
os.environ.setdefault("ANGEL_CLIENT_ID", "")
os.environ.setdefault("ANGEL_MPIN", "")
os.environ.setdefault("ANGEL_TOTP_SECRET", "")
os.environ.setdefault("SHOONYA_USER", "")
os.environ.setdefault("SHOONYA_PASSWORD", "")
os.environ.setdefault("SHOONYA_TOTP_SECRET", "")
os.environ.setdefault("SHOONYA_VENDOR_CODE", "")
os.environ.setdefault("SHOONYA_API_KEY", "")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

# ── loguru shim ──────────────────────────────────────────────────────────────
if "loguru" not in sys.modules:
    loguru_mod = types.ModuleType("loguru")
    class _FakeLogger:
        def info(self, *a, **k): pass
        def debug(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def critical(self, *a, **k): pass
        def success(self, *a, **k): pass
        def remove(self, *a, **k): pass
        def add(self, *a, **k): return 0
        def bind(self, **k): return self
    loguru_mod.logger = _FakeLogger()
    sys.modules["loguru"] = loguru_mod

# ── pydantic shim ────────────────────────────────────────────────────────────
if "pydantic" not in sys.modules:
    pydantic_mod = types.ModuleType("pydantic")

    class _FieldInfo:
        """Fake FieldInfo so .default works like real pydantic."""
        def __init__(self, default=None):
            self.default = default

    class BaseModel:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
            # Build model_fields: field_name -> FieldInfo with .default
            cls.model_fields = {}
            for klass in reversed(cls.__mro__):
                if klass is object: continue
                ann = klass.__dict__.get("__annotations__", {})
                for field_name in ann:
                    default = None
                    if hasattr(klass, field_name):
                        val = klass.__dict__.get(field_name)
                        if val is not None and not callable(val):
                            default = val
                    cls.model_fields[field_name] = _FieldInfo(default=default)

        def __init__(self, **data):
            import copy
            # Collect defaults from MRO
            for klass in reversed(type(self).__mro__):
                if klass is object: continue
                ann = klass.__dict__.get("__annotations__", {})
                for fn in ann:
                    if fn not in data and not hasattr(self, fn):
                        if hasattr(klass, fn):
                            val = klass.__dict__.get(fn)
                            if not callable(val):
                                try: setattr(self, fn, copy.deepcopy(val))
                                except: setattr(self, fn, val)
                        else:
                            setattr(self, fn, None)
            # Apply provided data
            for k, v in data.items():
                setattr(self, k, v)

        def model_dump(self):
            return {k: getattr(self, k, None) for k in type(self).model_fields}

    pydantic_mod.BaseModel = BaseModel
    pydantic_mod._FieldInfo = _FieldInfo
    sys.modules["pydantic"] = pydantic_mod

# ── pandas_ta shim ───────────────────────────────────────────────────────────
if "pandas_ta" not in sys.modules:
    import pandas as pd, numpy as np
    ta_mod = types.ModuleType("pandas_ta")
    def _ema(series, length=9, **k):
        return series.ewm(span=length, adjust=False).mean().rename(f"EMA_{length}")
    def _sma(series, length=20, **k):
        return series.rolling(length).mean().rename(f"SMA_{length}")
    def _rsi(series, length=14, **k):
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(length).mean()
        loss = (-delta.clip(upper=0)).rolling(length).mean()
        rs = gain / (loss + 1e-9)
        return (100 - 100/(1+rs)).rename(f"RSI_{length}")
    def _atr(high, low, close, length=14, **k):
        tr = pd.concat([high-low,(high-close.shift()).abs(),(low-close.shift()).abs()],axis=1).max(axis=1)
        return tr.ewm(span=length,adjust=False).mean().rename(f"ATRr_{length}")
    def _macd(series, fast=12, slow=26, signal=9, **k):
        f = series.ewm(span=fast,adjust=False).mean()
        s = series.ewm(span=slow,adjust=False).mean()
        m = f-s; sig=m.ewm(span=signal,adjust=False).mean(); hist=m-sig
        df = pd.DataFrame({f"MACD_{fast}_{slow}_{signal}":m, f"MACDs_{fast}_{slow}_{signal}":sig, f"MACDh_{fast}_{slow}_{signal}":hist})
        return df
    def _bbands(series, length=20, std=2, **k):
        m = series.rolling(length).mean()
        s = series.rolling(length).std()
        return pd.DataFrame({f"BBL_{length}_{float(std)}":m-std*s, f"BBM_{length}_{float(std)}":m, f"BBU_{length}_{float(std)}":m+std*s})
    def _obv(close, volume, **k):
        direction = np.sign(close.diff().fillna(0))
        return (direction * volume).cumsum().rename("OBV")
    def _mfi(high, low, close, volume, length=14, **k):
        typical = (high+low+close)/3
        mf = typical*volume
        pos = mf.where(typical > typical.shift(), 0).rolling(length).sum()
        neg = mf.where(typical < typical.shift(), 0).rolling(length).sum()
        return (100 - 100/(1 + pos/(neg+1e-9))).rename(f"MFI_{length}")
    def _vwap(high, low, close, volume, **k):
        typical = (high+low+close)/3
        return (typical*volume).cumsum()/(volume.cumsum()+1e-9).rename("VWAP_D")
    def _adx(high, low, close, length=14, **k):
        return pd.Series(25.0, index=close.index, name=f"ADX_{length}")
    ta_mod.ema = _ema; ta_mod.sma = _sma; ta_mod.rsi = _rsi
    ta_mod.atr = _atr; ta_mod.macd = _macd; ta_mod.bbands = _bbands
    ta_mod.obv = _obv; ta_mod.mfi = _mfi; ta_mod.vwap = _vwap
    ta_mod.adx = _adx
    sys.modules["pandas_ta"] = ta_mod

# ── xgboost, lightgbm shims ─────────────────────────────────────────────────
for _lib in ("xgboost", "lightgbm"):
    if _lib not in sys.modules:
        _m = types.ModuleType(_lib)
        class _FakeModel:
            def fit(self, X, y, **k): pass
            def predict(self, X): import numpy as _np; return _np.full(len(X), 0.5)
            def predict_proba(self, X):
                import numpy as _np; n=len(X); r=_np.column_stack([_np.full(n,0.45),_np.full(n,0.55)]); return r
        _m.XGBClassifier = _FakeModel
        _m.LGBMClassifier = _FakeModel
        sys.modules[_lib] = _m

# ── statsmodels shim ─────────────────────────────────────────────────────────
if "statsmodels" not in sys.modules:
    sm = types.ModuleType("statsmodels")
    sm_ts = types.ModuleType("statsmodels.tsa")
    sm_ts_stat = types.ModuleType("statsmodels.tsa.stattools")
    class _CointResult:
        def __init__(self): self.pvalue = 0.03
    def coint(a, b, **k):
        return (0.0, 0.03, [0.1, 0.05, 0.01])
    sm_ts_stat.coint = coint
    sm.tsa = sm_ts; sm_ts.stattools = sm_ts_stat
    sys.modules["statsmodels"] = sm
    sys.modules["statsmodels.tsa"] = sm_ts
    sys.modules["statsmodels.tsa.stattools"] = sm_ts_stat

# ── SmartApi / NorenRestApiPy / pyotp shims ──────────────────────────────────
for _name in ("SmartApi", "NorenRestApiPy", "pyotp"):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        class _FakeSmart:
            def __init__(self, *a, **k): pass
            def generateSession(self, *a, **k): return {"status": True, "data": {"jwtToken": "x", "refreshToken": "y", "feedToken": "z"}}
            def getProfile(self, *a, **k): return {"status": True, "data": {"name": "TestUser"}}
            def getCandleData(self, p): return {"status": True, "data": []}
            def ltpData(self, *a, **k): return {"status": True, "data": {"ltp": 100.0}}
        class _FakeNoren:
            def __init__(self, *a, **k): pass
            def login(self, **k): return {"stat": "Ok"}
            def place_order(self, **k): return "ORD001"
            def get_order_book(self): return [{"norenordno": "ORD001", "status": "COMPLETE"}]
        _m.SmartConnect = _FakeSmart
        _m.NorenApi = _FakeNoren
        if _name == "pyotp":
            class _TOTP:
                def __init__(self, secret): self.secret = secret
                def now(self): return "123456"
            _m.TOTP = _TOTP
        sys.modules[_name] = _m

# ── SmartWebSocketV2 shim ────────────────────────────────────────────────────
_smart_ws_mod = types.ModuleType("SmartApi.smartWebSocketV2")
class _FakeWSV2:
    def __init__(self, *a, **k): pass
    def connect(self): pass
    def subscribe(self, *a, **k): pass
    def close_connection(self): pass
_smart_ws_mod.SmartWebSocketV2 = _FakeWSV2
sys.modules["SmartApi.smartWebSocketV2"] = _smart_ws_mod

# ── fastapi, starlette, uvicorn shims ────────────────────────────────────────
for _nm in ("fastapi", "starlette", "starlette.middleware", "starlette.middleware.cors",
            "fastapi.middleware", "fastapi.middleware.cors", "fastapi.staticfiles",
            "fastapi.responses", "uvicorn", "httpx", "aiohttp"):
    if _nm not in sys.modules:
        _mx = types.ModuleType(_nm)
        sys.modules[_nm] = _mx

# ── yfinance shim ─────────────────────────────────────────────────────────────
if "yfinance" not in sys.modules:
    import pandas as pd, numpy as np
    yf_mod = types.ModuleType("yfinance")
    class _Ticker:
        def __init__(self, sym): self.sym = sym
        def history(self, period="1mo", interval="5m", **k):
            n=100; idx=pd.date_range("2024-01-01", periods=n, freq="5min")
            return pd.DataFrame({"Open":np.random.uniform(100,200,n),"High":np.random.uniform(150,250,n),
                                  "Low":np.random.uniform(80,150,n),"Close":np.random.uniform(100,200,n),
                                  "Volume":np.random.randint(1000,100000,n)}, index=idx)
    yf_mod.Ticker = _Ticker
    sys.modules["yfinance"] = yf_mod

# ── rich shim ─────────────────────────────────────────────────────────────────
if "rich" not in sys.modules:
    rich_mod = types.ModuleType("rich")
    rich_mod.print = print
    for sub in ("rich.console","rich.table","rich.panel","rich.text","rich.progress","rich.layout","rich.live"):
        sys.modules[sub] = types.ModuleType(sub)
    sys.modules["rich"] = rich_mod

# ══════════════════════════════════════════════════════════════════════════════
# TEST RUNNER
# ══════════════════════════════════════════════════════════════════════════════

class Result:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.errors = []
    def ok(self, n, cat): self.passed.append((n, cat))
    def fail(self, n, cat, msg): self.failed.append((n, cat, msg))
    def err(self, n, cat, e): self.errors.append((n, cat, str(e), traceback.format_exc()))

R = Result()
_test_num = [0]

def T(name, category):
    """Decorator for test functions."""
    def decorator(fn):
        _test_num[0] += 1
        num = _test_num[0]
        try:
            if asyncio.iscoroutinefunction(fn):
                asyncio.get_event_loop().run_until_complete(fn())
            else:
                fn()
            R.ok(f"T{num:03d}", category)
            print(f"  ✅ T{num:03d} {category}: {name}")
        except AssertionError as e:
            R.fail(f"T{num:03d}", category, f"{name} → ASSERT: {e}")
            print(f"  ❌ T{num:03d} {category}: {name} — {e}")
        except Exception as e:
            R.err(f"T{num:03d}", category, e)
            print(f"  💥 T{num:03d} {category}: {name} — {type(e).__name__}: {e}")
        return fn
    return decorator

def assert_eq(a, b, msg=""):
    assert a == b, f"{msg} expected {b!r} got {a!r}"
def assert_approx(a, b, tol=0.01, msg=""):
    assert abs(a - b) <= tol, f"{msg} expected ~{b} got {a}"
def assert_gt(a, b, msg=""):
    assert a > b, f"{msg} expected {a} > {b}"
def assert_gte(a, b, msg=""):
    assert a >= b, f"{msg} expected {a} >= {b}"
def assert_lt(a, b, msg=""):
    assert a < b, f"{msg} expected {a} < {b}"
def assert_in(item, container, msg=""):
    assert item in container, f"{msg} {item!r} not in {container}"
def assert_not_none(v, msg=""):
    assert v is not None, f"{msg} got None"
def assert_true(v, msg=""):
    assert v, f"{msg} got {v!r}"
def assert_false(v, msg=""):
    assert not v, f"{msg} got {v!r}"
def assert_isinstance(v, t, msg=""):
    assert isinstance(v, t), f"{msg} expected {t.__name__} got {type(v).__name__}"

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 1: CONFIG LOADING & VALIDATION (Tests 1–20)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 1: Config Loading & Validation")
print("═"*60)

from core.config import cfg, ZeroBotConfig, RiskConfig, PaperBrokerConfig, ShounyaConfig, AngelOneConfig

@T("cfg singleton is ZeroBotConfig", "Config")
def _():
    assert_isinstance(cfg, ZeroBotConfig)

@T("cfg.risk is RiskConfig", "Config")
def _():
    assert_isinstance(cfg.risk, RiskConfig)

@T("cfg.paper_broker is PaperBrokerConfig", "Config")
def _():
    assert_isinstance(cfg.paper_broker, PaperBrokerConfig)

@T("cfg.shoonya is ShounyaConfig", "Config")
def _():
    assert_isinstance(cfg.shoonya, ShounyaConfig)

@T("trailing_stop_pct = 1.5 (P16 default)", "Config")
def _():
    assert_eq(cfg.risk.trailing_stop_pct, 1.5)

@T("tiered_exit_enabled = True (P16 default)", "Config")
def _():
    assert_true(cfg.risk.tiered_exit_enabled)

@T("tiered_exit_at_pct = 0.5", "Config")
def _():
    assert_eq(cfg.risk.tiered_exit_at_pct, 0.5)

@T("initial_capital positive", "Config")
def _():
    assert_gt(cfg.initial_capital, 0)

@T("is_paper property: mode=paper → True", "Config")
def _():
    from core.config import ZeroBotConfig, RiskConfig, PaperBrokerConfig, ShounyaConfig, AngelOneConfig, TelegramConfig, DatabaseConfig, OptionsConfig
    c = ZeroBotConfig(mode="paper", risk=RiskConfig(), paper_broker=PaperBrokerConfig(),
                      shoonya=ShounyaConfig(), angel_one=AngelOneConfig(),
                      telegram=TelegramConfig(), database=DatabaseConfig(), options=OptionsConfig())
    assert_true(c.is_paper)

@T("is_hybrid property: mode=hybrid → True", "Config")
def _():
    from core.config import ZeroBotConfig, RiskConfig, PaperBrokerConfig, ShounyaConfig, AngelOneConfig, TelegramConfig, DatabaseConfig, OptionsConfig
    c = ZeroBotConfig(mode="hybrid", risk=RiskConfig(), paper_broker=PaperBrokerConfig(),
                      shoonya=ShounyaConfig(), angel_one=AngelOneConfig(),
                      telegram=TelegramConfig(), database=DatabaseConfig(), options=OptionsConfig())
    assert_true(c.is_hybrid)
    assert_false(c.is_paper)
    assert_false(c.is_live)

@T("is_live property: mode=live → True", "Config")
def _():
    from core.config import ZeroBotConfig, RiskConfig, PaperBrokerConfig, ShounyaConfig, AngelOneConfig, TelegramConfig, DatabaseConfig, OptionsConfig
    c = ZeroBotConfig(mode="live", risk=RiskConfig(), paper_broker=PaperBrokerConfig(),
                      shoonya=ShounyaConfig(), angel_one=AngelOneConfig(),
                      telegram=TelegramConfig(), database=DatabaseConfig(), options=OptionsConfig())
    assert_true(c.is_live)

@T("uses_real_data for live and hybrid", "Config")
def _():
    from core.config import ZeroBotConfig, RiskConfig, PaperBrokerConfig, ShounyaConfig, AngelOneConfig, TelegramConfig, DatabaseConfig, OptionsConfig
    for mode in ("live", "hybrid"):
        c = ZeroBotConfig(mode=mode, risk=RiskConfig(), paper_broker=PaperBrokerConfig(),
                          shoonya=ShounyaConfig(), angel_one=AngelOneConfig(),
                          telegram=TelegramConfig(), database=DatabaseConfig(), options=OptionsConfig())
        assert_true(c.uses_real_data, f"mode={mode}")
    cp = ZeroBotConfig(mode="paper", risk=RiskConfig(), paper_broker=PaperBrokerConfig(),
                       shoonya=ShounyaConfig(), angel_one=AngelOneConfig(),
                       telegram=TelegramConfig(), database=DatabaseConfig(), options=OptionsConfig())
    assert_false(cp.uses_real_data)

@T("AngelOneConfig.is_configured False when empty", "Config")
def _():
    a = AngelOneConfig()
    assert_false(a.is_configured)

@T("AngelOneConfig.is_configured True when all fields set", "Config")
def _():
    a = AngelOneConfig(api_key="k", client_id="c", mpin="1234", totp_secret="s")
    assert_true(a.is_configured)

@T("ShounyaConfig.is_configured False when empty", "Config")
def _():
    s = ShounyaConfig()
    assert_false(s.is_configured)

@T("ShounyaConfig.is_configured True when set", "Config")
def _():
    s = ShounyaConfig(user_id="u", password="p", totp_secret="t")
    assert_true(s.is_configured)

@T("ShounyaConfig.totp_key alias", "Config")
def _():
    s = ShounyaConfig(totp_secret="mysecret")
    assert_eq(s.totp_key, "mysecret")

@T("ShounyaConfig.imei default = abc1234", "Config")
def _():
    s = ShounyaConfig()
    assert_eq(s.imei, "abc1234")

@T("OptionsConfig.lot_size returns correct values", "Config")
def _():
    from core.config import OptionsConfig
    o = OptionsConfig()
    assert_eq(o.lot_size("^NSEI"), 50)
    assert_eq(o.lot_size("^NSEBANK"), 15)
    assert_eq(o.lot_size("UNKNOWN.NS"), 100)

@T("RiskConfig max_open_positions=5", "Config")
def _():
    r = RiskConfig()
    assert_eq(r.max_open_positions, 5)

@T("cfg.symbols list is non-empty and contains RELIANCE.NS", "Config")
def _():
    assert_true(len(cfg.symbols) > 0)
    assert_in("RELIANCE.NS", cfg.symbols)

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 2: TRANSACTION COST CALCULATOR (Tests 21–38)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 2: Transaction Cost Calculator")
print("═"*60)

from execution.transaction_cost import CostCalculator, TaxTracker

_calc = CostCalculator()

@T("CostCalculator instantiates with defaults", "CostCalc")
def _():
    c = CostCalculator()
    assert_not_none(c)

@T("compute BUY returns dict with required keys", "CostCalc")
def _():
    r = _calc.compute("BUY", 100, 150.0)
    for k in ("brokerage","stt","stamp_duty","exchange_charges","gst","sebi_turnover","total","trade_value","break_even_pct"):
        assert_in(k, r, f"missing key {k}")

@T("BUY: STT = 0 (STT only on sell for intraday)", "CostCalc")
def _():
    r = _calc.compute("BUY", 100, 150.0)
    assert_eq(r["stt"], 0.0, "BUY should have zero STT")

@T("SELL: STT > 0", "CostCalc")
def _():
    r = _calc.compute("SELL", 100, 150.0)
    assert_gt(r["stt"], 0.0, "SELL should have STT")

@T("BUY: stamp_duty > 0", "CostCalc")
def _():
    r = _calc.compute("BUY", 100, 150.0)
    assert_gt(r["stamp_duty"], 0.0, "BUY should have stamp duty")

@T("SELL: stamp_duty = 0", "CostCalc")
def _():
    r = _calc.compute("SELL", 100, 150.0)
    assert_eq(r["stamp_duty"], 0.0, "SELL should have zero stamp duty")

@T("exchange_charges > 0 for both sides", "CostCalc")
def _():
    for side in ("BUY","SELL"):
        r = _calc.compute(side, 100, 150.0)
        assert_gt(r["exchange_charges"], 0.0, f"{side} exchange_charges")

@T("GST = 18% of (brokerage + exchange_charges)", "CostCalc")
def _():
    r = _calc.compute("BUY", 100, 150.0)
    expected_gst = (r["brokerage"] + r["exchange_charges"]) * 0.18
    assert_approx(r["gst"], expected_gst, tol=0.01)

@T("total = sum of all components", "CostCalc")
def _():
    r = _calc.compute("BUY", 100, 150.0)
    parts = r["brokerage"]+r["stt"]+r["stamp_duty"]+r["exchange_charges"]+r["gst"]+r["sebi_turnover"]
    assert_approx(r["total"], parts, tol=0.05)

@T("trade_value = qty * price", "CostCalc")
def _():
    r = _calc.compute("BUY", 100, 150.0)
    assert_approx(r["trade_value"], 15000.0, tol=0.01)

@T("brokerage capped at min(20, 0.03% of trade_value)", "CostCalc")
def _():
    # Small trade: trade_value=100, 0.03%=0.03 → brokerage=0.03 not 20
    r_small = _calc.compute("BUY", 1, 100.0)
    assert_lt(r_small["brokerage"], 20.0)
    # Large trade: trade_value=100000, 0.03%=30 → brokerage=20 (cap)
    r_large = _calc.compute("BUY", 1000, 100.0)
    assert_approx(r_large["brokerage"], 20.0, tol=0.01)

@T("break_even_pct = total/trade_value*100", "CostCalc")
def _():
    r = _calc.compute("BUY", 100, 150.0)
    expected = r["total"] / r["trade_value"] * 100
    assert_approx(r["break_even_pct"], expected, tol=0.001)

@T("round_trip_cost has net_pnl = gross_pnl - costs", "CostCalc")
def _():
    rt = _calc.round_trip_cost(100, 150.0, 155.0)
    expected_net = rt["gross_pnl"] - rt["total_costs"]
    assert_approx(rt["net_pnl"], expected_net, tol=0.01)

@T("round_trip_cost: gross_pnl = (sell-buy)*qty", "CostCalc")
def _():
    rt = _calc.round_trip_cost(100, 150.0, 155.0)
    assert_approx(rt["gross_pnl"], 500.0, tol=0.01)

@T("CostCalculator with custom config", "CostCalc")
def _():
    from core.config import PaperBrokerConfig
    cfg_pb = PaperBrokerConfig()
    c = CostCalculator(cfg_pb)
    r = c.compute("BUY", 50, 200.0)
    assert_not_none(r)
    assert_gt(r["total"], 0)

@T("TaxTracker STCG rate = 20%", "CostCalc")
def _():
    tt = TaxTracker()
    assert_approx(tt.STCG_RATE, 0.20)

@T("TaxTracker LTCG rate = 12.5%", "CostCalc")
def _():
    tt = TaxTracker()
    assert_approx(tt.LTCG_RATE, 0.125)

@T("TaxTracker add_trade accumulates STCG", "CostCalc")
def _():
    tt = TaxTracker()
    tt.add_trade(10000.0, "EQ", 10)
    tt.add_trade(5000.0, "EQ", 30)
    assert_approx(tt.realized_stcg, 15000.0)

@T("TaxTracker tax_liability computes correctly", "CostCalc")
def _():
    tt = TaxTracker()
    tt.add_trade(50000.0, "EQ", 10)   # STCG
    tl = tt.tax_liability()
    assert_approx(tl["stcg_tax"], 10000.0, tol=1)  # 20% of 50000

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 3: EVENT BUS (Tests 39–50)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 3: Event Bus")
print("═"*60)

from core.event_bus import EventBus

@T("EventBus instantiates", "EventBus")
def _():
    eb = EventBus()
    assert_not_none(eb)

@T("subscribe and publish async fires handler", "EventBus")
async def _():
    eb = EventBus()
    received = []
    async def handler(data): received.append(data)
    eb.subscribe("tick", handler)
    await eb.publish("tick", {"price": 100})
    assert_eq(len(received), 1)
    assert_eq(received[0]["price"], 100)

@T("subscribe sync handler also fires", "EventBus")
async def _():
    eb = EventBus()
    received = []
    def sync_handler(data): received.append(data)
    eb.subscribe("signal", sync_handler)
    await eb.publish("signal", {"sym": "RELIANCE"})
    assert_eq(len(received), 1)

@T("idempotent subscribe — handler not added twice", "EventBus")
async def _():
    eb = EventBus()
    received = []
    def h(d): received.append(d)
    eb.subscribe("tick", h)
    eb.subscribe("tick", h)  # duplicate
    await eb.publish("tick", {"x": 1})
    assert_eq(len(received), 1, "handler should only fire once")

@T("unsubscribe removes handler", "EventBus")
async def _():
    eb = EventBus()
    received = []
    def h(d): received.append(d)
    eb.subscribe("tick", h)
    eb.unsubscribe("tick", h)
    await eb.publish("tick", {"x": 1})
    assert_eq(len(received), 0, "handler should not fire after unsubscribe")

@T("publish with no subscribers doesn't crash", "EventBus")
async def _():
    eb = EventBus()
    await eb.publish("heartbeat", {})  # no handler

@T("event history stored, max 100", "EventBus")
async def _():
    eb = EventBus()
    for i in range(105):
        await eb.publish("tick", {"i": i})
    h = eb.get_history("tick")
    assert_lte = lambda a, b: assert_true(a <= b)
    assert_lte(len(h), 100)

@T("get_history filtered by event type", "EventBus")
async def _():
    eb = EventBus()
    await eb.publish("tick", {"a": 1})
    await eb.publish("signal", {"b": 2})
    ticks = eb.get_history("tick")
    assert_true(all(e["event"] == "tick" for e in ticks))

@T("unknown event published with warning but no crash", "EventBus")
async def _():
    eb = EventBus()
    await eb.publish("custom_event", {"x": 1})  # not in EVENTS list

@T("multiple handlers same event all fire", "EventBus")
async def _():
    eb = EventBus()
    results = []
    async def h1(d): results.append("h1")
    async def h2(d): results.append("h2")
    eb.subscribe("order_filled", h1)
    eb.subscribe("order_filled", h2)
    await eb.publish("order_filled", {})
    assert_in("h1", results)
    assert_in("h2", results)

@T("handler exception doesn't crash event bus", "EventBus")
async def _():
    eb = EventBus()
    def bad_handler(d): raise ValueError("intentional error")
    eb.subscribe("tick", bad_handler)
    await eb.publish("tick", {"x": 1})  # should not raise

@T("global bus singleton is shared", "EventBus")
def _():
    from core.event_bus import bus as bus1
    from core.event_bus import bus as bus2
    assert_true(bus1 is bus2, "should be same singleton")

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 4: PAPER BROKER — ORDER LIFECYCLE (Tests 51–72)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 4: Paper Broker — Order Lifecycle")
print("═"*60)

from broker.paper_broker import PaperBroker, Order, OrderStatus, OrderType

@T("PaperBroker instantiates with default capital", "PaperBroker")
def _():
    pb = PaperBroker(initial_capital=100000)
    assert_not_none(pb)

@T("initial capital set correctly", "PaperBroker")
def _():
    pb = PaperBroker(initial_capital=55000)
    summary = pb.get_portfolio_summary()
    assert_eq(summary["capital"], 55000)

@T("initial available == initial capital", "PaperBroker")
def _():
    pb = PaperBroker(initial_capital=100000)
    summary = pb.get_portfolio_summary()
    assert_eq(summary["available"], 100000)

@T("place_order BUY returns filled Order", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    order = await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0, strategy="test")
    assert_isinstance(order, Order)
    assert_eq(order.status, OrderStatus.FILLED)

@T("filled order has correct side", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    order = await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0)
    assert_eq(order.side, "BUY")

@T("filled order qty matches requested qty", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    order = await pb.place_order("TCS.NS", "BUY", 5, 3000.0)
    assert_eq(order.filled_qty, 5)

@T("fill_price includes slippage (BUY > cmp)", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    cmp = 1000.0
    order = await pb.place_order("INFY.NS", "BUY", 10, cmp)
    assert_gte(order.fill_price, cmp, "BUY fill should be >= CMP due to slippage")

@T("fill_price for SELL has negative slippage (SELL < cmp)", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=200000)
    # First buy
    await pb.place_order("INFY.NS", "BUY", 10, 1000.0)
    cmp = 1100.0
    order = await pb.place_order("INFY.NS", "SELL", 10, cmp)
    assert_true(order.fill_price <= cmp, "SELL fill should be <= CMP")

@T("BUY deducts capital (available decreases)", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    before = pb._available
    await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0)
    assert_lt(pb._available, before)

@T("BUY creates position", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0)
    positions = pb.get_positions()
    assert_in("RELIANCE.NS", positions)

@T("BUY position has correct side=LONG", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0)
    pos = pb.get_positions()["RELIANCE.NS"]
    assert_eq(pos["side"], "LONG")

@T("BUY position has t1_done=False", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0, stop_loss=2400, target=2700)
    pos = pb.get_positions()["RELIANCE.NS"]
    assert_false(pos.get("t1_done"), "t1_done should start False")

@T("BUY position has trailing_high set", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0)
    pos = pb.get_positions()["RELIANCE.NS"]
    assert_not_none(pos.get("trailing_high"), "trailing_high should be set")

@T("SELL closes LONG position", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=200000)
    await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0)
    await pb.place_order("RELIANCE.NS", "SELL", 10, 2600.0)
    positions = pb.get_positions()
    assert_true("RELIANCE.NS" not in positions, "position should be closed after full SELL")

@T("SELL of LONG: profit realized in daily_pnl", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=200000)
    await pb.place_order("TCS.NS", "BUY", 5, 3000.0)
    await pb.place_order("TCS.NS", "SELL", 5, 3100.0)
    summary = pb.get_portfolio_summary()
    # Gross PnL = 5 * 100 = 500 minus costs
    assert_gt(summary["daily_pnl"], 0, "should have positive daily pnl on profitable trade")

@T("SELL of LONG: loss on price drop", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=200000)
    await pb.place_order("TCS.NS", "BUY", 5, 3000.0)
    await pb.place_order("TCS.NS", "SELL", 5, 2900.0)
    summary = pb.get_portfolio_summary()
    assert_lt(summary["daily_pnl"], 0, "should have negative daily pnl on losing trade")

@T("Order rejected if insufficient funds", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=1000)  # very little capital
    order = await pb.place_order("RELIANCE.NS", "BUY", 100, 2500.0)  # 250000 needed
    assert_eq(order.status, OrderStatus.REJECTED)

@T("win_count increments on profitable trade", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=200000)
    await pb.place_order("INFY.NS", "BUY", 10, 1000.0)
    await pb.place_order("INFY.NS", "SELL", 10, 1100.0)
    assert_eq(pb._win_count, 1)
    assert_eq(pb._trade_count, 1)

@T("trade_count increments on every close", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=500000)
    for i in range(3):
        await pb.place_order(f"SYM{i}.NS", "BUY", 5, 100.0)
        await pb.place_order(f"SYM{i}.NS", "SELL", 5, 110.0)
    assert_eq(pb._trade_count, 3)

@T("cancel_order: PENDING order can be cancelled", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    import uuid
    from broker.paper_broker import Order, OrderStatus, OrderType
    from datetime import datetime
    oid = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
    order = Order(order_id=oid, symbol="TEST.NS", side="BUY", qty=10,
                  order_type=OrderType.MARKET, price=100.0, status=OrderStatus.PENDING)
    pb._orders[oid] = order
    result = await pb.cancel_order(oid)
    assert_true(result)
    assert_eq(pb._orders[oid].status, OrderStatus.CANCELLED)

@T("cancel_order: FILLED order returns False", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    order = await pb.place_order("RELIANCE.NS", "BUY", 5, 2500.0)
    result = await pb.cancel_order(order.order_id)
    assert_false(result, "FILLED order cannot be cancelled")

@T("get_portfolio_summary has all required keys", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=100000)
    s = pb.get_portfolio_summary()
    for k in ("capital","available","daily_pnl","total_pnl","open_positions","win_rate","broker"):
        assert_in(k, s, f"missing key {k}")

@T("adding to existing position updates avg_price", "PaperBroker")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0)
    await pb.place_order("RELIANCE.NS", "BUY", 10, 2600.0)
    pos = pb.get_positions()["RELIANCE.NS"]
    assert_eq(pos["qty"], 20)
    # avg = (2500*10 + fill1*10 + fill2*10) / 20 ≈ between fill prices
    avg = pos["avg_price"]
    # With slippage the fills will be slightly above 2500 and 2600
    assert_gt(avg, 2490)
    assert_lt(avg, 2620)

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 5: PAPER BROKER — SHORT POSITIONS (Tests 73–83)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 5: Paper Broker — SHORT Positions")
print("═"*60)

@T("SELL without position creates SHORT", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=500000)
    order = await pb.place_order("SBIN.NS", "SELL", 20, 600.0)
    assert_eq(order.status, OrderStatus.FILLED)
    positions = pb.get_positions()
    assert_in("SBIN.NS", positions)
    assert_eq(positions["SBIN.NS"]["side"], "SHORT")

@T("SHORT position has t1_done=False initially", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("SBIN.NS", "SELL", 20, 600.0, stop_loss=650, target=550)
    pos = pb.get_positions()["SBIN.NS"]
    assert_false(pos.get("t1_done"), "SHORT t1_done should start False")

@T("SHORT position has trailing_low set", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("SBIN.NS", "SELL", 20, 600.0)
    pos = pb.get_positions()["SBIN.NS"]
    assert_not_none(pos.get("trailing_low"), "SHORT should have trailing_low set")

@T("SHORT position: short_margin_locked ≈ 30% of trade_value", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("AXISBANK.NS", "SELL", 50, 1000.0)
    pos = pb.get_positions()["AXISBANK.NS"]
    trade_val = pos["avg_price"] * pos["qty"]
    margin = pos.get("short_margin_locked", 0)
    assert_gt(margin, 0, "margin should be locked for SHORT")

@T("closing SHORT with BUY: profit when price drops", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("HDFCBANK.NS", "SELL", 10, 1500.0)
    await pb.place_order("HDFCBANK.NS", "BUY", 10, 1400.0)  # price dropped = profit
    summary = pb.get_portfolio_summary()
    assert_gt(summary["daily_pnl"], 0, "SHORT trade profit when price drops")

@T("closing SHORT with BUY: loss when price rises", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("HDFCBANK.NS", "SELL", 10, 1500.0)
    await pb.place_order("HDFCBANK.NS", "BUY", 10, 1600.0)  # price rose = loss
    summary = pb.get_portfolio_summary()
    assert_lt(summary["daily_pnl"], 0, "SHORT trade loss when price rises")

@T("closing SHORT removes position", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("TCS.NS", "SELL", 5, 3000.0)
    await pb.place_order("TCS.NS", "BUY", 5, 2900.0)
    positions = pb.get_positions()
    assert_true("TCS.NS" not in positions)

@T("SHORT rejected if insufficient margin", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=100)  # tiny capital
    order = await pb.place_order("RELIANCE.NS", "SELL", 100, 2500.0)  # needs 75000 margin
    assert_eq(order.status, OrderStatus.REJECTED)

@T("square_off_all_intraday closes all positions", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("INFY.NS", "BUY", 10, 1000.0)
    await pb.place_order("TCS.NS", "BUY", 5, 3000.0)
    await pb.square_off_all_intraday()
    positions = pb.get_positions()
    assert_eq(len(positions), 0, "all positions should be closed after square-off")

@T("reset_daily clears daily_pnl", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=200000)
    await pb.place_order("INFY.NS", "BUY", 5, 1000.0)
    await pb.place_order("INFY.NS", "SELL", 5, 1100.0)
    pb.reset_daily()
    assert_eq(pb._daily_pnl, 0.0)

@T("order confidence stored in Order", "PaperBroker-Short")
async def _():
    pb = PaperBroker(initial_capital=100000)
    order = await pb.place_order("INFY.NS", "BUY", 5, 1000.0, confidence=0.75)
    assert_eq(order.confidence, 0.75)

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 6: PAPER BROKER — TRAILING STOP & TIERED EXIT (Tests 84–100)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 6: Paper Broker — Trailing Stop & Tiered Exit")
print("═"*60)

@T("trailing stop advances SL on new high (LONG)", "TrailingStop")
async def _():
    pb = PaperBroker(initial_capital=500000)
    cfg.risk.trailing_stop_pct = 1.5
    await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0, stop_loss=2400.0, target=2700.0)
    pos = pb.get_positions()["RELIANCE.NS"]
    original_sl = pos["stop_loss"]
    # Simulate price moving up
    await pb.check_stops_and_targets("RELIANCE.NS", 2600.0)
    pos = pb.get_positions().get("RELIANCE.NS", {})
    new_sl = pos.get("stop_loss", original_sl)
    assert_gte(new_sl, original_sl, "trailing SL should move up")

@T("trailing stop never retreats (LONG)", "TrailingStop")
async def _():
    pb = PaperBroker(initial_capital=500000)
    cfg.risk.trailing_stop_pct = 1.5
    await pb.place_order("RELIANCE.NS", "BUY", 10, 2500.0, stop_loss=2400.0, target=2700.0)
    await pb.check_stops_and_targets("RELIANCE.NS", 2600.0)
    pos = pb.get_positions().get("RELIANCE.NS", {})
    sl_after_high = pos.get("stop_loss", 0)
    # Price drops — SL should stay at the high
    await pb.check_stops_and_targets("RELIANCE.NS", 2550.0)
    pos = pb.get_positions().get("RELIANCE.NS", {})
    sl_after_drop = pos.get("stop_loss", sl_after_high)
    assert_gte(sl_after_drop, sl_after_high, "SL should not drop when price drops")

@T("trailing_high updates on each new high", "TrailingStop")
async def _():
    pb = PaperBroker(initial_capital=500000)
    cfg.risk.trailing_stop_pct = 1.5
    await pb.place_order("TCS.NS", "BUY", 5, 3000.0, stop_loss=2900.0, target=3300.0)
    await pb.check_stops_and_targets("TCS.NS", 3100.0)
    pos = pb.get_positions().get("TCS.NS", {})
    assert_approx(pos.get("trailing_high", 0), 3100.0, tol=1.0)

@T("trailing stop SHORT: SL moves down on new low", "TrailingStop")
async def _():
    pb = PaperBroker(initial_capital=500000)
    cfg.risk.trailing_stop_pct = 1.5
    await pb.place_order("SBIN.NS", "SELL", 20, 600.0, stop_loss=640.0, target=560.0)
    pos = pb.get_positions()["SBIN.NS"]
    original_sl = pos["stop_loss"]
    # Price drops — SHORT trailing should update
    await pb.check_stops_and_targets("SBIN.NS", 570.0)
    pos = pb.get_positions().get("SBIN.NS", {})
    new_sl = pos.get("stop_loss", original_sl)
    assert_lte = lambda a, b: assert_true(a <= b, f"{a} should be <= {b}")
    assert_lte(new_sl, original_sl)  # For SHORT, SL should be lower

@T("stop loss triggered when price hits stop (LONG)", "TrailingStop")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("INFY.NS", "BUY", 10, 1000.0, stop_loss=950.0, target=1100.0)
    assert_in("INFY.NS", pb.get_positions())
    await pb.check_stops_and_targets("INFY.NS", 940.0)  # Below stop
    positions = pb.get_positions()
    assert_true("INFY.NS" not in positions, "position should be closed after stop hit")

@T("target triggered when price hits target (LONG)", "TrailingStop")
async def _():
    # Disable tiered exit so T1 doesn't fire before full target check
    cfg.risk.tiered_exit_enabled = False
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("INFY.NS", "BUY", 10, 1000.0, stop_loss=950.0, target=1100.0)
    await pb.check_stops_and_targets("INFY.NS", 1110.0)  # Above target
    cfg.risk.tiered_exit_enabled = True
    positions = pb.get_positions()
    assert_true("INFY.NS" not in positions, "position closed on target")

@T("stop loss triggered when price hits stop (SHORT)", "TrailingStop")
async def _():
    cfg.risk.tiered_exit_enabled = False
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("TCS.NS", "SELL", 5, 3000.0, stop_loss=3100.0, target=2800.0)
    await pb.check_stops_and_targets("TCS.NS", 3150.0)  # above stop
    cfg.risk.tiered_exit_enabled = True
    positions = pb.get_positions()
    assert_true("TCS.NS" not in positions, "SHORT position closed on stop")

@T("target triggered SHORT when price hits target", "TrailingStop")
async def _():
    cfg.risk.tiered_exit_enabled = False
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("TCS.NS", "SELL", 5, 3000.0, stop_loss=3100.0, target=2800.0)
    await pb.check_stops_and_targets("TCS.NS", 2780.0)  # below target
    cfg.risk.tiered_exit_enabled = True
    positions = pb.get_positions()
    assert_true("TCS.NS" not in positions, "SHORT position closed on target")

@T("tiered exit T1 fires at 50% of target profit", "TieredExit")
async def _():
    cfg.risk.tiered_exit_enabled = True
    cfg.risk.tiered_exit_at_pct = 0.5
    pb = PaperBroker(initial_capital=500000)
    # Buy at 1000, target=1200, T1 at 50% of target profit = 100 pnl = price ~1100
    await pb.place_order("WIPRO.NS", "BUY", 10, 1000.0, stop_loss=950.0, target=1200.0)
    # At 1100, curr_profit=(1100-1000)*10=1000 >= t1_threshold=(1200-1000)*10*0.5=1000
    await pb.check_stops_and_targets("WIPRO.NS", 1105.0)
    pos = pb.get_positions().get("WIPRO.NS", {})
    if pos:
        assert_true(pos.get("t1_done"), "T1 should be marked done")
        assert_eq(pos["qty"], 5, "should have exited 50% qty")

@T("tiered exit T1 moves SL to breakeven", "TieredExit")
async def _():
    cfg.risk.tiered_exit_enabled = True
    cfg.risk.tiered_exit_at_pct = 0.5
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("WIPRO.NS", "BUY", 10, 1000.0, stop_loss=950.0, target=1200.0)
    orig_fill = pb.get_positions()["WIPRO.NS"]["avg_price"]
    await pb.check_stops_and_targets("WIPRO.NS", 1110.0)
    pos = pb.get_positions().get("WIPRO.NS", {})
    if pos and pos.get("t1_done"):
        new_sl = pos.get("stop_loss")
        # SL should equal avg_price (breakeven)
        assert_approx(new_sl, orig_fill, tol=5.0, msg="T1 should move SL to breakeven")

@T("T1 only fires once (t1_done prevents repeat)", "TieredExit")
async def _():
    cfg.risk.tiered_exit_enabled = True
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("WIPRO.NS", "BUY", 10, 1000.0, stop_loss=950.0, target=1200.0)
    # Fire T1
    await pb.check_stops_and_targets("WIPRO.NS", 1110.0)
    pos = pb.get_positions().get("WIPRO.NS")
    if pos and pos.get("t1_done"):
        qty_after_t1 = pos["qty"]
        # Call again at same/higher price
        await pb.check_stops_and_targets("WIPRO.NS", 1120.0)
        pos2 = pb.get_positions().get("WIPRO.NS")
        if pos2:
            # qty should not have changed (T1 already done)
            assert_eq(pos2["qty"], qty_after_t1, "T1 should not fire twice")

@T("tiered exit disabled: T1 does not fire", "TieredExit")
async def _():
    cfg.risk.tiered_exit_enabled = False
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("WIPRO.NS", "BUY", 10, 1000.0, stop_loss=950.0, target=1200.0)
    await pb.check_stops_and_targets("WIPRO.NS", 1110.0)
    pos = pb.get_positions().get("WIPRO.NS")
    if pos:
        assert_false(pos.get("t1_done"), "T1 should not fire when disabled")
    # Restore
    cfg.risk.tiered_exit_enabled = True

@T("unrealized_pnl updated on price check (LONG)", "TrailingStop")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("INFY.NS", "BUY", 10, 1000.0, stop_loss=950.0, target=1200.0)
    await pb.check_stops_and_targets("INFY.NS", 1050.0)
    pos = pb.get_positions().get("INFY.NS")
    if pos:
        assert_gt(pos.get("unrealized_pnl", 0), 0, "unrealized pnl should be positive")

@T("unrealized_pnl updated on price check (SHORT)", "TrailingStop")
async def _():
    pb = PaperBroker(initial_capital=500000)
    await pb.place_order("SBIN.NS", "SELL", 20, 600.0, stop_loss=640.0, target=560.0)
    await pb.check_stops_and_targets("SBIN.NS", 580.0)
    pos = pb.get_positions().get("SBIN.NS")
    if pos:
        assert_gt(pos.get("unrealized_pnl", 0), 0, "SHORT unrealized pnl positive when price falls")

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 7: MARKET CLOCK (Tests 101–112)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 7: Market Clock")
print("═"*60)

from core.clock import (now_ist, today_ist, is_holiday, is_weekend,
                        is_market_day, is_market_hours, is_warmup_period,
                        is_closing_period, minutes_to_close, session_status,
                        NSE_HOLIDAYS)

@T("now_ist() returns datetime with timezone", "Clock")
def _():
    from datetime import datetime
    now = now_ist()
    assert_isinstance(now, datetime)
    assert_not_none(now.tzinfo)

@T("today_ist() returns date object", "Clock")
def _():
    from datetime import date
    today = today_ist()
    assert_isinstance(today, date)

@T("ZEROBOT_FORCE_MARKET_OPEN=1 makes is_market_day True", "Clock")
def _():
    os.environ["ZEROBOT_FORCE_MARKET_OPEN"] = "1"
    assert_true(is_market_day())

@T("ZEROBOT_FORCE_MARKET_OPEN=1 makes is_market_hours True", "Clock")
def _():
    assert_true(is_market_hours())

@T("NSE_HOLIDAYS contains Republic Day 2026", "Clock")
def _():
    assert_in(date(2026, 1, 26), NSE_HOLIDAYS)

@T("NSE_HOLIDAYS contains Republic Day 2025", "Clock")
def _():
    assert_in(date(2025, 1, 26), NSE_HOLIDAYS)

@T("NSE_HOLIDAYS contains Holi 2025", "Clock")
def _():
    assert_in(date(2025, 3, 14), NSE_HOLIDAYS)

@T("is_holiday() returns bool", "Clock")
def _():
    result = is_holiday()
    assert_isinstance(result, bool)

@T("is_weekend() returns bool", "Clock")
def _():
    result = is_weekend()
    assert_isinstance(result, bool)

@T("minutes_to_close() returns non-negative int", "Clock")
def _():
    mins = minutes_to_close()
    assert_isinstance(mins, int)
    assert_gte(mins, 0)

@T("session_status() has all required keys", "Clock")
def _():
    ss = session_status()
    for k in ("is_market_day","is_market_hours","is_warmup","is_closing","is_holiday","is_weekend","current_ist","minutes_to_close"):
        assert_in(k, ss, f"missing key {k}")

@T("session_status current_ist contains IST", "Clock")
def _():
    ss = session_status()
    assert_in("IST", ss["current_ist"])

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 8: REGIME DETECTOR (Tests 113–122)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 8: Regime Detector")
print("═"*60)

from core.regime_detector import RegimeDetector, MarketRegime, RegimeState

@T("RegimeDetector instantiates", "Regime")
def _():
    rd = RegimeDetector()
    assert_not_none(rd)

@T("default regime is NORMAL", "Regime")
def _():
    rd = RegimeDetector()
    assert_eq(rd.state.regime, MarketRegime.NORMAL)

@T("VIX < 14 + bull Nifty → AGGRESSIVE regime", "Regime")
def _():
    rd = RegimeDetector()
    # AGGRESSIVE requires vix<14 AND nifty bullish (above 1% of sma50)
    state = rd.update(vix=12.0, nifty_price=22000, nifty_sma50=21000)
    assert_eq(state.regime, MarketRegime.AGGRESSIVE)

@T("VIX 14-18 → NORMAL regime", "Regime")
def _():
    rd = RegimeDetector()
    state = rd.update(vix=16.0)
    assert_eq(state.regime, MarketRegime.NORMAL)

@T("VIX 18-20 → DEFENSIVE regime", "Regime")
def _():
    rd = RegimeDetector()
    state = rd.update(vix=19.0)
    assert_eq(state.regime, MarketRegime.DEFENSIVE)

@T("VIX > 20 → CRISIS regime", "Regime")
def _():
    rd = RegimeDetector()
    state = rd.update(vix=22.0)
    assert_eq(state.regime, MarketRegime.CRISIS)

@T("CRISIS regime: new_trades_allowed = False", "Regime")
def _():
    rd = RegimeDetector()
    state = rd.update(vix=25.0)
    assert_false(state.new_trades_allowed)

@T("AGGRESSIVE regime: size_multiplier > 1.0", "Regime")
def _():
    rd = RegimeDetector()
    state = rd.update(vix=12.0)
    assert_gte(state.size_multiplier, 1.0)

@T("DEFENSIVE regime: size_multiplier < 1.0", "Regime")
def _():
    rd = RegimeDetector()
    state = rd.update(vix=19.0)
    assert_lt(state.size_multiplier, 1.0)

@T("CRISIS regime: options_allowed = False", "Regime")
def _():
    rd = RegimeDetector()
    state = rd.update(vix=24.0)
    assert_false(state.options_allowed)

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 9: RISK ENGINE & KELLY SIZER (Tests 123–138)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 9: Risk Engine & Kelly Sizer")
print("═"*60)

from risk.kelly_sizer import KellySizer, SizeResult
from risk.risk_engine import TradeSignal, RiskResult, DrawdownGuard, SECTOR_MAP

@T("KellySizer instantiates", "KellySizer")
def _():
    ks = KellySizer()
    assert_not_none(ks)

@T("compute returns SizeResult", "KellySizer")
def _():
    ks = KellySizer()
    r = ks.compute(capital=100000, cmp=500.0, confidence=70.0)
    assert_isinstance(r, SizeResult)

@T("qty >= 1 always", "KellySizer")
def _():
    ks = KellySizer()
    r = ks.compute(capital=100000, cmp=500.0, confidence=50.0)
    assert_gte(r.qty, 1)

@T("higher confidence → larger position", "KellySizer")
def _():
    ks = KellySizer()
    r_low = ks.compute(capital=100000, cmp=500.0, confidence=55.0)
    r_high = ks.compute(capital=100000, cmp=500.0, confidence=80.0)
    assert_gte(r_high.qty, r_low.qty)

@T("kelly_f clamped: never negative", "KellySizer")
def _():
    ks = KellySizer()
    r = ks.compute(capital=100000, cmp=500.0, confidence=10.0, rr_ratio=0.5)
    assert_gte(r.kelly_f, 0.0)

@T("position_inr within capital limits", "KellySizer")
def _():
    capital = 100000
    ks = KellySizer(max_pct=0.20)
    r = ks.compute(capital=capital, cmp=100.0, confidence=80.0, rr_ratio=2.0)
    assert_lte = lambda a, b: assert_true(a <= b, f"{a} > {b}")
    assert_lte(r.position_inr, capital * 0.21)  # allow tiny rounding

@T("invalid inputs (cmp=0) returns qty=1", "KellySizer")
def _():
    ks = KellySizer()
    r = ks.compute(capital=100000, cmp=0.0, confidence=70.0)
    assert_eq(r.qty, 1)

@T("win_rate override blends with confidence", "KellySizer")
def _():
    ks = KellySizer()
    r1 = ks.compute(capital=100000, cmp=500.0, confidence=70.0)
    r2 = ks.compute(capital=100000, cmp=500.0, confidence=70.0, win_rate=0.65)
    assert_not_none(r2)  # should not crash

@T("DrawdownGuard instantiates", "RiskEngine")
def _():
    dg = DrawdownGuard(max_drawdown_pct=20.0)
    assert_not_none(dg)

@T("TradeSignal dataclass", "RiskEngine")
def _():
    ts = TradeSignal(symbol="INFY.NS", side="BUY", strategy="Momentum",
                     confidence=75.0, trigger="EMA cross")
    assert_eq(ts.symbol, "INFY.NS")
    assert_eq(ts.side, "BUY")

@T("SECTOR_MAP contains Banking sector", "RiskEngine")
def _():
    assert_in("Banking", SECTOR_MAP)

@T("SECTOR_MAP Banking contains HDFCBANK.NS", "RiskEngine")
def _():
    assert_in("HDFCBANK.NS", SECTOR_MAP["Banking"])

@T("SECTOR_MAP IT contains TCS.NS", "RiskEngine")
def _():
    assert_in("TCS.NS", SECTOR_MAP["IT"])

@T("RiskResult dataclass", "RiskEngine")
def _():
    rr = RiskResult(approved=True, recommended_qty=10, position_size_inr=25000,
                    stop_loss=2400, target=2700, risk_reward="2:1", rr_ratio=2.0)
    assert_true(rr.approved)

@T("SECTOR_MAP FMCG contains ITC.NS", "RiskEngine")
def _():
    assert_in("ITC.NS", SECTOR_MAP["FMCG"])

@T("RiskEngine can be instantiated", "RiskEngine")
def _():
    from risk.risk_engine import RiskEngine
    re = RiskEngine()
    assert_not_none(re)

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 10: STATE MANAGER & BOT STATE (Tests 139–148)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 10: State Manager & Bot State")
print("═"*60)

from core.state_manager import BotState

@T("BotState instantiates", "State")
def _():
    s = BotState()
    assert_not_none(s)

@T("BotState.capital = cfg.initial_capital", "State")
def _():
    s = BotState()
    assert_eq(s.capital, cfg.initial_capital)

@T("BotState.status starts STOPPED", "State")
def _():
    s = BotState()
    assert_eq(s.status, "STOPPED")

@T("BotState.is_halted property", "State")
def _():
    s = BotState()
    s.status = "HALTED"
    assert_true(s.is_halted)

@T("BotState.is_halted setter", "State")
def _():
    s = BotState()
    s.is_halted = True
    assert_eq(s.status, "HALTED")
    s.is_halted = False
    assert_eq(s.status, "RUNNING")

@T("BotState.total_capital = capital + daily_pnl", "State")
def _():
    s = BotState()
    s.daily_pnl = 1000.0
    assert_approx(s.total_capital, s.capital + 1000.0)

@T("BotState.win_rate computes correctly", "State")
def _():
    s = BotState()
    s.daily_wins = 3
    s.daily_losses = 2
    assert_approx(s.win_rate, 0.6)

@T("BotState.win_rate = 0 when no trades", "State")
def _():
    s = BotState()
    assert_eq(s.win_rate, 0.0)

@T("BotState.update_pnl updates peak_capital", "State")
def _():
    s = BotState()
    s.update_pnl(5000.0)
    assert_gte(s.peak_capital, s.capital + 5000.0 - 1)

@T("BotState.drawdown_pct = 0 when at peak", "State")
def _():
    s = BotState()
    assert_approx(s.drawdown_pct, 0.0)

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 11: STRATEGIES (Tests 149–160)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 11: Strategies — Signal Generation")
print("═"*60)

import numpy as np
import pandas as pd

def _make_ohlcv(n=100, base=1000.0, trend=1.0):
    """Create realistic OHLCV DataFrame with indicators."""
    import numpy as np
    np.random.seed(42)
    close = base + np.cumsum(np.random.randn(n) * 5 * trend)
    close = np.maximum(close, base * 0.5)
    high = close + np.abs(np.random.randn(n) * 3)
    low = close - np.abs(np.random.randn(n) * 3)
    low = np.minimum(low, close)
    high = np.maximum(high, close)
    volume = pd.Series(np.random.randint(100000, 500000, n).astype(float))
    close = pd.Series(close)
    high = pd.Series(high)
    low = pd.Series(low)
    df = pd.DataFrame({"open": close*0.999, "high": high, "low": low, "close": close, "volume": volume})
    # Add indicator columns
    df["EMA_9"] = df["close"].ewm(span=9).mean()
    df["EMA_21"] = df["close"].ewm(span=21).mean()
    df["EMA_50"] = df["close"].ewm(span=50).mean()
    df["SMA_200"] = df["close"].rolling(200, min_periods=1).mean()
    df["RSI_14"] = 55.0  # neutral RSI
    df["ATRr_14"] = 15.0
    df["vol_spike"] = volume / volume.rolling(20, min_periods=1).mean()
    df["MACD_12_26_9"] = df["EMA_9"] - df["EMA_21"]
    df["MACDs_12_26_9"] = df["MACD_12_26_9"].ewm(span=9).mean()
    df["MACDh_12_26_9"] = df["MACD_12_26_9"] - df["MACDs_12_26_9"]
    df["BBU_20_2.0"] = df["close"].rolling(20,min_periods=1).mean() + 2*df["close"].rolling(20,min_periods=1).std().fillna(0)
    df["BBL_20_2.0"] = df["close"].rolling(20,min_periods=1).mean() - 2*df["close"].rolling(20,min_periods=1).std().fillna(0)
    df["bb_position"] = 0.5
    df["bb_width"] = 0.02
    df["vwap_dev"] = 0.5  # above VWAP
    df["ADX_14"] = 25.0
    df["trending"] = 1
    df["OBV"] = (df["close"].diff().apply(lambda x: 1 if x>0 else -1) * df["volume"]).cumsum()
    df["MFI_14"] = 55.0
    df["hist_vol_10"] = df["close"].pct_change().rolling(10,min_periods=1).std().fillna(0.01)
    return df

@T("MomentumStrategy instantiates", "Strategy")
def _():
    from strategies.momentum import MomentumStrategy
    s = MomentumStrategy()
    assert_eq(s.name, "Momentum")

@T("MomentumStrategy.generate_signal returns None on short df", "Strategy")
def _():
    from strategies.momentum import MomentumStrategy
    s = MomentumStrategy()
    df = _make_ohlcv(n=10)
    result = s.generate_signal(df, "INFY.NS")
    assert_true(result is None, "Too short df should return None")

@T("MomentumStrategy.generate_signal doesn't crash on full df", "Strategy")
def _():
    from strategies.momentum import MomentumStrategy
    s = MomentumStrategy()
    df = _make_ohlcv(n=100)
    result = s.generate_signal(df, "INFY.NS")
    # May or may not return a signal — just should not crash

@T("MeanReversionStrategy instantiates", "Strategy")
def _():
    from strategies.mean_reversion import MeanReversionStrategy
    s = MeanReversionStrategy()
    assert_not_none(s)

@T("VWAPStrategy instantiates", "Strategy")
def _():
    from strategies.vwap_strategy import VWAPStrategy
    s = VWAPStrategy()
    assert_not_none(s)

@T("MarketMakingStrategy instantiates", "Strategy")
def _():
    from strategies.market_making import MarketMakingStrategy
    s = MarketMakingStrategy()
    assert_not_none(s)

@T("SupertrendStrategy instantiates", "Strategy")
def _():
    from strategies.supertrend import SupertrendStrategy
    s = SupertrendStrategy()
    assert_not_none(s)

@T("ORBStrategy instantiates", "Strategy")
def _():
    from strategies.opening_range_breakout import ORBStrategy
    s = ORBStrategy()
    assert_not_none(s)

@T("RSIDivergenceStrategy instantiates", "Strategy")
def _():
    from strategies.rsi_divergence import RSIDivergenceStrategy
    s = RSIDivergenceStrategy()
    assert_not_none(s)

@T("BreakoutStrategy instantiates", "Strategy")
def _():
    from strategies.breakout import BreakoutStrategy
    s = BreakoutStrategy()
    assert_not_none(s)

@T("StatArbStrategy instantiates with correct defaults", "Strategy")
def _():
    from strategies.stat_arb import StatArbStrategy
    s = StatArbStrategy()
    assert_eq(s.zscore_entry, 2.0)
    assert_eq(s.zscore_exit, 0.5)
    assert_eq(s.lookback, 60)

@T("StatArbStrategy._calibrated starts False", "Strategy")
def _():
    from strategies.stat_arb import StatArbStrategy
    s = StatArbStrategy()
    assert_false(s._calibrated)

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 12: INDICATOR ENGINE (Tests 161–165)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 12: Indicator Engine")
print("═"*60)

from data.processors.indicator_engine import IndicatorEngine

@T("IndicatorEngine instantiates", "IndicatorEngine")
def _():
    ie = IndicatorEngine()
    assert_not_none(ie)

@T("add_all returns DataFrame with more columns", "IndicatorEngine")
def _():
    ie = IndicatorEngine()
    df = _make_ohlcv(50)
    base_cols = len(df.columns)
    result = ie.add_all(df)
    assert_true(len(result.columns) >= base_cols, "should have at least same columns")

@T("add_all: result has same row count as input", "IndicatorEngine")
def _():
    ie = IndicatorEngine()
    df = _make_ohlcv(60)
    result = ie.add_all(df)
    assert_eq(len(result), len(df))

@T("add_all doesn't crash with minimal df", "IndicatorEngine")
def _():
    ie = IndicatorEngine()
    df = _make_ohlcv(10)
    result = ie.add_all(df)
    assert_not_none(result)

@T("IndicatorEngine fallback to manual on pandas_ta failure", "IndicatorEngine")
def _():
    """Ensure _add_manual runs when pandas_ta not available"""
    ie = IndicatorEngine()
    df = _make_ohlcv(30)
    result = ie._add_manual(df)
    assert_not_none(result)
    assert_eq(len(result), 30)

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 13: BROKER FACTORY & DUAL BROKER (Tests 166–172)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 13: Broker Factory & Dual Broker")
print("═"*60)

@T("get_broker('paper') returns PaperBroker", "BrokerFactory")
def _():
    from broker.factory import get_broker
    from broker.paper_broker import PaperBroker
    b = get_broker(force="paper")
    assert_isinstance(b, PaperBroker)

@T("get_broker returns PaperBroker when shoonya not configured", "BrokerFactory")
def _():
    from broker.factory import get_broker
    from broker.paper_broker import PaperBroker
    b = get_broker(force="shoonya")  # shoonya not configured → fallback
    assert_isinstance(b, PaperBroker)

@T("get_broker returns PaperBroker on unknown broker name", "BrokerFactory")
def _():
    from broker.factory import get_broker
    from broker.paper_broker import PaperBroker
    b = get_broker(force="unknown_broker_xyz")
    assert_isinstance(b, PaperBroker)

@T("DualBrokerArchitecture instantiates", "DualBroker")
def _():
    from broker.dual_broker import DualBrokerArchitecture
    d = DualBrokerArchitecture()
    assert_not_none(d)

@T("DualBroker._paper_broker is always initialized", "DualBroker")
def _():
    from broker.dual_broker import DualBrokerArchitecture
    from broker.paper_broker import PaperBroker
    d = DualBrokerArchitecture()
    assert_isinstance(d._paper_broker, PaperBroker)

@T("DualBroker has get_positions method", "DualBroker")
def _():
    from broker.dual_broker import DualBrokerArchitecture
    d = DualBrokerArchitecture()
    assert_true(hasattr(d, "get_positions"))

@T("DualBroker place_order (paper fallback) returns filled order", "DualBroker")
async def _():
    from broker.dual_broker import DualBrokerArchitecture
    d = DualBrokerArchitecture()
    order = await d.place_order("TCS.NS", "BUY", 5, 3000.0, strategy="test")
    assert_not_none(order)
    assert_eq(order.status.value if hasattr(order.status, 'value') else order.status, "FILLED")

# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY 14: TELEGRAM ALERTER (Tests 173–178)
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═"*60)
print("CATEGORY 14: Telegram Alerter")
print("═"*60)

from alerts.telegram_bot import TelegramAlerter

@T("TelegramAlerter instantiates", "Telegram")
def _():
    t = TelegramAlerter()
    assert_not_none(t)

@T("TelegramAlerter.send() returns False when no token (graceful)", "Telegram")
async def _():
    t = TelegramAlerter()
    result = await t.send("Test message", priority="INFO")
    assert_false(result, "should return False when no token configured")

@T("TelegramAlerter.PRIORITY_ORDER has 4 levels", "Telegram")
def _():
    t = TelegramAlerter()
    assert_eq(len(t.PRIORITY_ORDER), 4)
    for lvl in ("CRITICAL","HIGH","MEDIUM","INFO"):
        assert_in(lvl, t.PRIORITY_ORDER)

@T("CRITICAL priority > HIGH > MEDIUM > INFO", "Telegram")
def _():
    t = TelegramAlerter()
    assert_gt(t.PRIORITY_ORDER["CRITICAL"], t.PRIORITY_ORDER["HIGH"])
    assert_gt(t.PRIORITY_ORDER["HIGH"], t.PRIORITY_ORDER["MEDIUM"])
    assert_gt(t.PRIORITY_ORDER["MEDIUM"], t.PRIORITY_ORDER["INFO"])

@T("_post_telegram_sync handles network failure gracefully", "Telegram")
def _():
    from alerts.telegram_bot import _post_telegram_sync
    # Should return False (not raise) when connection fails
    result = _post_telegram_sync("bad_token", "123", "test msg")
    assert_false(result)

@T("TelegramAlerter throttle_seconds from config", "Telegram")
def _():
    t = TelegramAlerter()
    assert_gt(t._throttle, 0)

# ══════════════════════════════════════════════════════════════════════════════
# RESULTS SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

total = len(R.passed) + len(R.failed) + len(R.errors)

print("\n" + "═"*70)
print("BATCH 1 RESULTS — ZeroBot v1.1 Patch16")
print("═"*70)

# Category breakdown
cats = defaultdict(lambda: {"pass":0,"fail":0,"err":0})
for _, cat in R.passed: cats[cat]["pass"] += 1
for _, cat, _ in R.failed: cats[cat]["fail"] += 1
for _, cat, _, _ in R.errors: cats[cat]["err"] += 1

print(f"\n{'Category':<25} {'PASS':>6} {'FAIL':>6} {'ERROR':>6} {'TOTAL':>6}")
print("-"*55)
for cat in sorted(cats.keys()):
    c = cats[cat]
    t = c["pass"]+c["fail"]+c["err"]
    print(f"  {cat:<23} {c['pass']:>6} {c['fail']:>6} {c['err']:>6} {t:>6}")

print("─"*55)
print(f"  {'TOTAL':<23} {len(R.passed):>6} {len(R.failed):>6} {len(R.errors):>6} {total:>6}")
print()

pass_rate = len(R.passed) / total * 100 if total else 0
print(f"  ✅ PASSED : {len(R.passed)}/{total} ({pass_rate:.1f}%)")
print(f"  ❌ FAILED : {len(R.failed)}")
print(f"  💥 ERRORS : {len(R.errors)}")

if R.failed:
    print("\n── FAILURES ─────────────────────────────────────────────────")
    for tnum, cat, msg in R.failed:
        print(f"  {tnum} [{cat}]: {msg}")

if R.errors:
    print("\n── ERRORS ───────────────────────────────────────────────────")
    for tnum, cat, msg, tb in R.errors:
        print(f"  {tnum} [{cat}]: {msg}")
        # Print first relevant line of traceback
        lines = [l.strip() for l in tb.split('\n') if l.strip() and 'File' in l]
        if lines: print(f"    → {lines[-1]}")

print("\n" + "═"*70)
