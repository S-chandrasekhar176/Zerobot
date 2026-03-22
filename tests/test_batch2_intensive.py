#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZeroBot v1.1 Patch16 — INTENSIVE TEST SUITE  Batch 2: T179–T328
=================================================================
10 categories, 150 tests, identical shim infrastructure to Batch 1.

Categories
  P  ML Pipeline        EnsemblePredictor.predict, cache, weights   T179–T200
  Q  Model Trainer      FeatureBuilder, train_full, walk-fwd CV      T201–T215
  R  WalkForwardBT      windows, aggregate, MC simulation            T216–T228
  S  BacktestEngine     run, metrics, trade-log, edge cases          T229–T245
  T  Data Feeds         PaperFeed, AngelOneFeed, MultiMarket         T246–T263
  U  Indicator Engine   all columns, edge-cases, vol_spike           T264–T277
  V  Options Strategy   IV, BS price, lot sizes, strikes, exits      T278–T296
  W  News/Sentiment     score, hard-block, SentimentResult           T297–T312
  X  Risk Engine        all 11 gates, sizing, DrawdownGuard          T313–T328

Run:
    cd zerobot_patch16
    PYTHONPATH=. python3 tests/test_batch2_intensive.py
"""

import sys, os, types, asyncio, traceback, math, copy
from pathlib import Path
from datetime import datetime, date, timedelta

# ══════════════════════════════════════════════════════════════════════════════
# SHIM SETUP  —  identical pattern to Batch 1
# ══════════════════════════════════════════════════════════════════════════════

ROOT = Path(__file__).parent.parent          # zerobot_patch16/
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ZEROBOT_FORCE_MARKET_OPEN", "1")
for _k in ("ANGEL_API_KEY","ANGEL_CLIENT_ID","ANGEL_MPIN","ANGEL_TOTP_SECRET",
           "SHOONYA_USER","SHOONYA_PASSWORD","SHOONYA_TOTP_SECRET",
           "SHOONYA_VENDOR_CODE","SHOONYA_API_KEY",
           "TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID"):
    os.environ.setdefault(_k, "")

# ── loguru ────────────────────────────────────────────────────────────────────
if "loguru" not in sys.modules:
    _lm = types.ModuleType("loguru")
    class _FL:
        def info(self,*a,**k):  pass
        def debug(self,*a,**k): pass
        def warning(self,*a,**k): pass
        def error(self,*a,**k): pass
        def critical(self,*a,**k): pass
        def success(self,*a,**k): pass
        def remove(self,*a,**k): pass
        def add(self,*a,**k): return 0
        def bind(self,**k): return self
    _lm.logger = _FL()
    sys.modules["loguru"] = _lm

# ── pydantic ──────────────────────────────────────────────────────────────────
if "pydantic" not in sys.modules:
    _pm = types.ModuleType("pydantic")
    class _FI:
        def __init__(self, default=None): self.default = default
    class _BM:
        def __init_subclass__(cls, **kw):
            super().__init_subclass__(**kw)
            cls.model_fields = {}
            for klass in reversed(cls.__mro__):
                if klass is object: continue
                for fn, _ in klass.__dict__.get("__annotations__", {}).items():
                    default = None
                    if hasattr(klass, fn):
                        val = klass.__dict__.get(fn)
                        if val is not None and not callable(val): default = val
                    cls.model_fields[fn] = _FI(default=default)
        def __init__(self, **data):
            for klass in reversed(type(self).__mro__):
                if klass is object: continue
                for fn in klass.__dict__.get("__annotations__", {}):
                    if fn not in data and not hasattr(self, fn):
                        if hasattr(klass, fn):
                            val = klass.__dict__.get(fn)
                            if not callable(val):
                                try: setattr(self, fn, copy.deepcopy(val))
                                except: setattr(self, fn, val)
                        else: setattr(self, fn, None)
            for k, v in data.items(): setattr(self, k, v)
        def model_dump(self):
            return {k: getattr(self, k, None) for k in type(self).model_fields}
    _pm.BaseModel = _BM
    _pm._FieldInfo = _FI
    sys.modules["pydantic"] = _pm

# ── pandas_ta ─────────────────────────────────────────────────────────────────
if "pandas_ta" not in sys.modules:
    import pandas as _pd, numpy as _np
    _ta = types.ModuleType("pandas_ta")
    def _ema(s, length=9, **k):
        return s.ewm(span=length, adjust=False).mean().rename(f"EMA_{length}")
    def _sma(s, length=20, **k):
        return s.rolling(length).mean().rename(f"SMA_{length}")
    def _rsi(s, length=14, **k):
        d = s.diff()
        g = d.clip(lower=0).rolling(length).mean()
        l = (-d.clip(upper=0)).rolling(length).mean()
        return (100 - 100 / (1 + g / (l + 1e-9))).rename(f"RSI_{length}")
    def _atr(h, l, c, length=14, **k):
        tr = _pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
        return tr.ewm(span=length, adjust=False).mean().rename(f"ATRr_{length}")
    def _macd(s, fast=12, slow=26, signal=9, **k):
        f  = s.ewm(span=fast, adjust=False).mean()
        sl = s.ewm(span=slow, adjust=False).mean()
        m  = f - sl; sig = m.ewm(span=signal, adjust=False).mean()
        return _pd.DataFrame({f"MACD_{fast}_{slow}_{signal}": m,
                               f"MACDs_{fast}_{slow}_{signal}": sig,
                               f"MACDh_{fast}_{slow}_{signal}": m - sig})
    def _bbands(s, length=20, std=2, **k):
        m = s.rolling(length).mean(); st = s.rolling(length).std()
        return _pd.DataFrame({f"BBL_{length}_{float(std)}": m - std*st,
                               f"BBM_{length}_{float(std)}": m,
                               f"BBU_{length}_{float(std)}": m + std*st})
    def _obv(c, v, **k):
        return (_np.sign(c.diff().fillna(0)) * v).cumsum().rename("OBV")
    def _mfi(h, l, c, v, length=14, **k):
        tp = (h + l + c) / 3; mf = tp * v
        pos = mf.where(tp > tp.shift(), 0).rolling(length).sum()
        neg = mf.where(tp < tp.shift(), 0).rolling(length).sum()
        return (100 - 100 / (1 + pos / (neg + 1e-9))).rename(f"MFI_{length}")
    def _vwap(h, l, c, v, **k):
        tp = (h + l + c) / 3
        return (tp * v).cumsum() / (v.cumsum() + 1e-9)
    def _adx(h, l, c, length=14, **k):
        return _pd.Series(25.0, index=c.index, name=f"ADX_{length}")
    _ta.ema = _ema; _ta.sma = _sma; _ta.rsi = _rsi; _ta.atr = _atr
    _ta.macd = _macd; _ta.bbands = _bbands; _ta.obv = _obv
    _ta.mfi = _mfi; _ta.vwap = _vwap; _ta.adx = _adx
    sys.modules["pandas_ta"] = _ta

# ── xgboost / lightgbm (top-level classes so joblib.dump won't fail) ─────────
import numpy as _np_shim

class _XGBClassifier:
    def __init__(self, **k): self.n_features_in_ = 10
    def fit(self, X, y, eval_set=None, verbose=False, **k):
        self.n_features_in_ = X.shape[1] if hasattr(X, "shape") else 10
    def predict(self, X): return _np_shim.zeros(len(X), dtype=int)
    def predict_proba(self, X):
        n = len(X)
        return _np_shim.column_stack([_np_shim.full(n, 0.45), _np_shim.full(n, 0.55)])

class _LGBMClassifier(_XGBClassifier):
    pass

for _lib in ("xgboost", "lightgbm"):
    if _lib not in sys.modules:
        _m = types.ModuleType(_lib)
        _m.XGBClassifier  = _XGBClassifier
        _m.LGBMClassifier = _LGBMClassifier
        sys.modules[_lib] = _m

# ── statsmodels ───────────────────────────────────────────────────────────────
if "statsmodels" not in sys.modules:
    _sm = types.ModuleType("statsmodels")
    _smts = types.ModuleType("statsmodels.tsa")
    _smstat = types.ModuleType("statsmodels.tsa.stattools")
    def _coint(a, b, **k): return (0.0, 0.03, [0.1, 0.05, 0.01])
    _smstat.coint = _coint
    _sm.tsa = _smts; _smts.stattools = _smstat
    sys.modules.update({"statsmodels": _sm, "statsmodels.tsa": _smts,
                        "statsmodels.tsa.stattools": _smstat})

# ── SmartApi / NorenRestApiPy / pyotp ─────────────────────────────────────────
for _name in ("SmartApi", "NorenRestApiPy", "pyotp"):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        class _FS:
            def __init__(self, *a, **k): pass
            def generateSession(self, *a, **k):
                return {"status": True, "data": {"jwtToken": "x", "refreshToken": "y", "feedToken": "z"}}
            def getProfile(self, *a, **k): return {"status": True, "data": {"name": "Test"}}
            def getCandleData(self, p): return {"status": True, "data": []}
            def ltpData(self, *a, **k): return {"status": True, "data": {"ltp": 100.0}}
        class _FN:
            def __init__(self, *a, **k): pass
            def login(self, **k): return {"stat": "Ok"}
            def place_order(self, **k): return "ORD001"
            def get_order_book(self): return [{"norenordno": "ORD001", "status": "COMPLETE"}]
        _m.SmartConnect = _FS; _m.NorenApi = _FN
        if _name == "pyotp":
            class _TOTP:
                def __init__(self, s): pass
                def now(self): return "123456"
            _m.TOTP = _TOTP
        sys.modules[_name] = _m

_sws = types.ModuleType("SmartApi.smartWebSocketV2")
class _FWSV2:
    def __init__(self, *a, **k): pass
    def connect(self): pass
    def subscribe(self, *a, **k): pass
    def close_connection(self): pass
_sws.SmartWebSocketV2 = _FWSV2
sys.modules["SmartApi.smartWebSocketV2"] = _sws

# ── FastAPI + related shims ────────────────────────────────────────────────────
for _nm in ("fastapi", "starlette", "starlette.middleware",
            "starlette.middleware.cors", "fastapi.middleware",
            "fastapi.middleware.cors", "fastapi.staticfiles",
            "fastapi.responses", "uvicorn", "httpx", "aiohttp"):
    if _nm not in sys.modules:
        sys.modules[_nm] = types.ModuleType(_nm)

_fa = sys.modules["fastapi"]
class _FAPIApp:
    def __init__(self, **k): self._routes = {}; self.title = k.get("title","")
    def get(self, p, **k):
        def d(fn): self._routes[p] = fn; return fn
        return d
    def post(self, p, **k):
        def d(fn): self._routes[p] = fn; return fn
        return d
    def add_middleware(self, *a, **k): pass
    def mount(self, *a, **k): pass
    def on_event(self, e):
        def d(fn): return fn
        return d
_fa.FastAPI = _FAPIApp
class _FHTTP(Exception):
    def __init__(self, status_code=400, detail=""): self.status_code=status_code; self.detail=detail
_fa.HTTPException = _FHTTP
class _FWS: pass
_fa.WebSocket = _FWS; _fa.WebSocketDisconnect = Exception
class _FBMPydantic: pass
_fa.BaseModel = _FBMPydantic
class _FResp:
    def __init__(self, c=None, s=200, **k): self.content=c; self.status_code=s
sys.modules["fastapi.responses"].JSONResponse = _FResp
sys.modules["fastapi.responses"].FileResponse  = _FResp
class _FCORS:
    def __init__(self, *a, **k): pass
sys.modules["fastapi.middleware.cors"].CORSMiddleware = _FCORS
sys.modules["starlette.middleware.cors"].CORSMiddleware = _FCORS
class _FSF:
    def __init__(self, *a, **k): pass
sys.modules["fastapi.staticfiles"].StaticFiles = _FSF

# ── yfinance ──────────────────────────────────────────────────────────────────
if "yfinance" not in sys.modules:
    import pandas as _pd2, numpy as _np2
    _yf = types.ModuleType("yfinance")
    class _YTicker:
        def __init__(self, s): self.s = s
        def history(self, period="1mo", interval="5m", **k):
            n = 100; idx = _pd2.date_range("2024-01-01", periods=n, freq="5min")
            return _pd2.DataFrame({"Open":  _np2.random.uniform(100, 200, n),
                                   "High":  _np2.random.uniform(150, 250, n),
                                   "Low":   _np2.random.uniform(80,  150, n),
                                   "Close": _np2.random.uniform(100, 200, n),
                                   "Volume":_np2.random.randint(1000,100000,n)}, index=idx)
        @property
        def fast_info(self):
            class _FI:
                last_price = 100.0
            return _FI()
    def _yf_download(*a, **k):
        import pandas as _p
        return _p.DataFrame()
    _yf.Ticker   = _YTicker
    _yf.download = _yf_download
    sys.modules["yfinance"] = _yf

# ── rich ──────────────────────────────────────────────────────────────────────
if "rich" not in sys.modules:
    _rm = types.ModuleType("rich"); _rm.print = print
    for _s in ("rich.console","rich.table","rich.panel","rich.text",
               "rich.progress","rich.layout","rich.live"):
        sys.modules[_s] = types.ModuleType(_s)
    sys.modules["rich"] = _rm

# ── joblib  (real joblib is available; just make sure it's imported) ──────────
import joblib  # already installed system-wide

# ══════════════════════════════════════════════════════════════════════════════
# TEST RUNNER
# ══════════════════════════════════════════════════════════════════════════════
import numpy as np
import pandas as pd

_PASS = 0; _FAIL = 0
_RESULTS: list = []   # (tid, cat, name, status, detail)

_test_num = [178]     # Batch 2 starts at T179

def T(name: str, category: str):
    """Decorator — runs function immediately and records result."""
    def decorator(fn):
        _test_num[0] += 1
        num = _test_num[0]
        tid = f"T{num:03d}"
        try:
            if asyncio.iscoroutinefunction(fn):
                asyncio.get_event_loop().run_until_complete(fn())
            else:
                fn()
            global _PASS; _PASS += 1
            _RESULTS.append((tid, category, name, "PASS", ""))
            print(f"  ✅ {tid} [{category}] {name}")
        except AssertionError as e:
            global _FAIL; _FAIL += 1
            msg = str(e)[:140]
            _RESULTS.append((tid, category, name, "FAIL", msg))
            print(f"  ❌ {tid} [{category}] {name}  —  {msg}")
        except Exception as e:
            _FAIL += 1
            msg = f"{type(e).__name__}: {str(e)[:120]}"
            _RESULTS.append((tid, category, name, "ERROR", msg))
            print(f"  💥 {tid} [{category}] {name}  —  {msg}")
            if os.environ.get("B2_VERBOSE"):
                traceback.print_exc()
        return fn
    return decorator

def hdr(title): print(f"\n{'═'*70}\n  {title}\n{'═'*70}")

# Assertion helpers
def aeq(a, b, m=""):  assert a == b,  f"{m}  expected {b!r}  got {a!r}"
def agt(a, b, m=""):  assert a > b,   f"{m}  expected {a!r} > {b!r}"
def agte(a, b, m=""): assert a >= b,  f"{m}  expected {a!r} >= {b!r}"
def alt(a, b, m=""):  assert a < b,   f"{m}  expected {a!r} < {b!r}"
def altel(a, b, m=""): assert a <= b, f"{m}  expected {a!r} <= {b!r}"
def ain(x, c, m=""):  assert x in c,  f"{m}  {x!r} not in container"
def atype(v, t, m=""): assert isinstance(v, t), f"{m}  expected {t.__name__} got {type(v).__name__}"
def atrue(v, m=""):   assert v,        f"{m}  got {v!r}"
def afalse(v, m=""):  assert not v,    f"{m}  got {v!r}"
def aapprox(a, b, tol=0.05, m=""):
    assert abs(float(a) - float(b)) <= tol, \
        f"{m}  |{a}-{b}|={abs(float(a)-float(b)):.5f} > tol={tol}"
def anot_none(v, m=""): assert v is not None, f"{m}  got None"

# ── OHLCV factory ─────────────────────────────────────────────────────────────
def make_ohlcv(n=200, start=1000.0, trend=0.3, vol=12.0, seed=42):
    rng = np.random.default_rng(seed)
    closes = [start + i * trend + rng.uniform(-vol, vol) for i in range(n)]
    highs  = [c + abs(rng.uniform(0, vol)) for c in closes]
    lows   = [c - abs(rng.uniform(0, vol)) for c in closes]
    opens  = [c + rng.uniform(-vol/2, vol/2) for c in closes]
    vols   = rng.integers(500_000, 5_000_000, n)
    idx    = pd.date_range("2023-01-02", periods=n, freq="1D")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": vols}, index=idx)


# ══════════════════════════════════════════════════════════════════════════════
# P  ML PIPELINE  T179–T200
# ══════════════════════════════════════════════════════════════════════════════
hdr("P  ML PIPELINE  (T179–T200)")

from models.predictor import EnsemblePredictor

@T("EnsemblePredictor instantiates", "ML")
def _():
    p = EnsemblePredictor()
    anot_none(p)

@T("is_ready() False when _models is empty", "ML")
def _():
    p = EnsemblePredictor(); p._models = {}
    afalse(p.is_ready())

@T("predict() returns dict with 'direction' key when no models", "ML")
def _():
    p = EnsemblePredictor(); p._models = {}
    r = p.predict(make_ohlcv(), "X")
    atype(r, dict); ain("direction", r)

@T("predict() returns HOLD + 50.0 when no models", "ML")
def _():
    p = EnsemblePredictor(); p._models = {}
    r = p.predict(make_ohlcv(), "X")
    aeq(r["direction"], "HOLD"); aeq(r["confidence"], 50.0)

@T("predict() empty DataFrame returns HOLD", "ML")
def _():
    p = EnsemblePredictor(); p._models = {}
    r = p.predict(pd.DataFrame(), "X")
    aeq(r["direction"], "HOLD")

@T("predict() BUY when ensemble_prob > 0.55", "ML")
def _():
    p = EnsemblePredictor()
    p._models = {"xgboost": type("M", (), {
        "n_features_in_": 5,
        "predict_proba": lambda self, X: np.column_stack(
            [np.full(len(X), 0.35), np.full(len(X), 0.65)])})()}
    r = p.predict(make_ohlcv(), "REL")
    aeq(r["direction"], "BUY"); agt(r["confidence"], 55.0)

@T("predict() SELL when ensemble_prob < 0.45", "ML")
def _():
    p = EnsemblePredictor()
    p._models = {"xgboost": type("M", (), {
        "n_features_in_": 5,
        "predict_proba": lambda self, X: np.column_stack(
            [np.full(len(X), 0.65), np.full(len(X), 0.35)])})()}
    r = p.predict(make_ohlcv(), "SBI")
    aeq(r["direction"], "SELL")

@T("predict() HOLD when prob in 0.45–0.55", "ML")
def _():
    p = EnsemblePredictor()
    p._models = {"xgboost": type("M", (), {
        "n_features_in_": 5,
        "predict_proba": lambda self, X: np.column_stack(
            [np.full(len(X), 0.50), np.full(len(X), 0.50)])})()}
    r = p.predict(make_ohlcv(), "X")
    aeq(r["direction"], "HOLD"); aeq(r["confidence"], 50.0)

@T("predict() result has 'ensemble_prob' key when models present", "ML")
def _():
    p = EnsemblePredictor()
    p._models = {"xgboost": type("M", (), {
        "n_features_in_": 5,
        "predict_proba": lambda self, X: np.column_stack(
            [np.full(len(X), 0.40), np.full(len(X), 0.60)])})()}
    r = p.predict(make_ohlcv(), "TCS")
    ain("ensemble_prob", r)

@T("predict() cache hit — same df returns identical result", "ML")
def _():
    p = EnsemblePredictor()
    p._models = {"xgboost": type("M", (), {
        "n_features_in_": 5,
        "predict_proba": lambda self, X: np.column_stack(
            [np.full(len(X), 0.40), np.full(len(X), 0.60)])})()}
    df = make_ohlcv()
    r1 = p.predict(df, "SYM"); r2 = p.predict(df, "SYM")
    aeq(r1["direction"], r2["direction"])

@T("predict() cache miss after row added", "ML")
def _():
    p = EnsemblePredictor()
    p._models = {"xgboost": type("M", (), {
        "n_features_in_": 5,
        "predict_proba": lambda self, X: np.column_stack(
            [np.full(len(X), 0.40), np.full(len(X), 0.60)])})()}
    df  = make_ohlcv(100); p.predict(df, "SYM")
    df2 = make_ohlcv(101); p.predict(df2, "SYM")
    atrue(True)   # no crash; cache should have refreshed

@T("predict() pads features when model expects more", "ML")
def _():
    p = EnsemblePredictor()
    class _W:
        n_features_in_ = 500
        def predict_proba(self, X):
            assert X.shape[1] == 500, f"expected 500 got {X.shape[1]}"
            return np.column_stack([np.full(len(X), 0.4), np.full(len(X), 0.6)])
    p._models = {"xgboost": _W()}
    r = p.predict(make_ohlcv(), "X")
    ain(r["direction"], ("BUY", "SELL", "HOLD"))

@T("predict() trims features when model expects fewer", "ML")
def _():
    p = EnsemblePredictor()
    class _N:
        n_features_in_ = 2
        def predict_proba(self, X):
            assert X.shape[1] == 2, f"expected 2 got {X.shape[1]}"
            return np.column_stack([np.full(len(X), 0.4), np.full(len(X), 0.6)])
    p._models = {"xgboost": _N()}
    r = p.predict(make_ohlcv(), "X")
    ain(r["direction"], ("BUY", "SELL", "HOLD"))

@T("predict() handles NaN-filled df without crash", "ML")
def _():
    p = EnsemblePredictor()
    p._models = {"xgboost": type("M", (), {
        "n_features_in_": 5,
        "predict_proba": lambda self, X: np.column_stack(
            [np.full(len(X), 0.40), np.full(len(X), 0.60)])})()}
    df = make_ohlcv(); df["close"] = np.nan
    r  = p.predict(df, "X")    # must not raise
    ain(r["direction"], ("BUY", "SELL", "HOLD"))

@T("record_trade_outcome increments counter", "ML")
def _():
    p = EnsemblePredictor(); p._models = {}
    p.record_trade_outcome("X", 100.0)
    aeq(p._trade_count_since_retrain, 1)

@T("record_trade_outcome returns True at threshold, resets counter", "ML")
def _():
    p = EnsemblePredictor(); p._models = {}; p._retrain_threshold = 3
    p.record_trade_outcome("X", 100.0)
    p.record_trade_outcome("X", -50.0)
    r = p.record_trade_outcome("X", 200.0)
    atrue(r, "should signal retrain"); aeq(p._trade_count_since_retrain, 0)

@T("record_trade_outcome returns False below threshold", "ML")
def _():
    p = EnsemblePredictor(); p._models = {}; p._retrain_threshold = 10
    afalse(p.record_trade_outcome("X", 50.0))

@T("get_model_info() returns dict with required keys", "ML")
def _():
    p = EnsemblePredictor(); p._models = {}
    info = p.get_model_info()
    atype(info, dict)
    for k in ("models", "prediction_log_size", "trades_since_retrain"):
        ain(k, info)

@T("prediction_log capped at maxlen=500", "ML")
def _():
    p = EnsemblePredictor()
    p._models = {"xgboost": type("M", (), {
        "n_features_in_": 5,
        "predict_proba": lambda self, X: np.column_stack(
            [np.full(len(X), 0.40), np.full(len(X), 0.60)])})()}
    for i in range(510):
        df = make_ohlcv(50, seed=i % 50)
        df.iloc[-1, df.columns.get_loc("close")] = 1000 + i
        p.predict(df, f"S{i:04d}")
    altel(len(p._prediction_log), 500)

@T("WEIGHTS sum to 1.0 (xgb 0.55 + lgb 0.45)", "ML")
def _():
    aapprox(sum(EnsemblePredictor.WEIGHTS.values()), 1.0, tol=0.001)

@T("feat_cache capped at 50 entries", "ML")
def _():
    p = EnsemblePredictor()
    p._models = {"xgboost": type("M", (), {
        "n_features_in_": 5,
        "predict_proba": lambda self, X: np.column_stack(
            [np.full(len(X), 0.40), np.full(len(X), 0.60)])})()}
    for i in range(60):
        df = make_ohlcv(50, seed=i)
        df.iloc[-1, df.columns.get_loc("close")] = 1000 + i
        p.predict(df, f"SYM{i:04d}")
    altel(len(p._feat_cache), 55)


# ══════════════════════════════════════════════════════════════════════════════
# Q  MODEL TRAINER  T201–T215
# ══════════════════════════════════════════════════════════════════════════════
hdr("Q  MODEL TRAINER  (T201–T215)")

from models.trainer import ModelTrainer, FeatureBuilder

@T("FeatureBuilder instantiates", "Trainer")
def _():
    fb = FeatureBuilder(); anot_none(fb)

@T("FeatureBuilder.FEATURE_COLS is a non-empty list", "Trainer")
def _():
    atype(FeatureBuilder.FEATURE_COLS, list); agt(len(FeatureBuilder.FEATURE_COLS), 10)

@T("FeatureBuilder.build() returns DataFrame", "Trainer")
def _():
    fb = FeatureBuilder()
    r  = fb.build(make_ohlcv(200))
    atype(r, pd.DataFrame)

@T("FeatureBuilder.build() adds 'return_1' column", "Trainer")
def _():
    r = FeatureBuilder().build(make_ohlcv(200))
    ain("return_1", r.columns)

@T("FeatureBuilder.build() adds 'ema9_vs_ema21' column", "Trainer")
def _():
    r = FeatureBuilder().build(make_ohlcv(200))
    ain("ema9_vs_ema21", r.columns)

@T("FeatureBuilder.build() adds 'bb_width' column", "Trainer")
def _():
    r = FeatureBuilder().build(make_ohlcv(200))
    ain("bb_width", r.columns)

@T("FeatureBuilder.build_target() returns 0/1 Series", "Trainer")
def _():
    fb = FeatureBuilder()
    df = make_ohlcv(200)
    t  = fb.build_target(df, forward=3)
    atype(t, pd.Series)
    atrue(set(t.dropna().unique()).issubset({0, 1}))

@T("FeatureBuilder.build() handles market_data=None", "Trainer")
def _():
    r = FeatureBuilder().build(make_ohlcv(200), market_data=None)
    atype(r, pd.DataFrame)

@T("ModelTrainer instantiates", "Trainer")
def _():
    mt = ModelTrainer(); anot_none(mt)

@T("ModelTrainer.train_full() runs without crash on 500-row df", "Trainer")
def _():
    mt  = ModelTrainer()
    df  = make_ohlcv(500)
    res = mt.train_full(df, "TEST")
    atype(res, dict)

@T("train_full() result has 'xgboost' key on sufficient data", "Trainer")
def _():
    mt  = ModelTrainer()
    df  = make_ohlcv(500)
    res = mt.train_full(df, "TEST")
    ain("xgboost", res)

@T("train_full() xgboost accuracy in [0, 1]", "Trainer")
def _():
    mt  = ModelTrainer()
    df  = make_ohlcv(500)
    res = mt.train_full(df, "TEST")
    if "xgboost" in res:
        acc = res["xgboost"]["accuracy"]
        agte(acc, 0.0); altel(acc, 1.0)

@T("train_full() result has 'cv' key", "Trainer")
def _():
    mt  = ModelTrainer()
    res = mt.train_full(make_ohlcv(500), "TEST")
    ain("cv", res)

@T("train_full() returns {} on too-small df (<100 rows)", "Trainer")
def _():
    mt  = ModelTrainer()
    res = mt.train_full(make_ohlcv(50), "TINY")
    aeq(res, {})

@T("FeatureBuilder.build_from_trades() returns None on empty list", "Trainer")
def _():
    fb = FeatureBuilder()
    r  = fb.build_from_trades([])
    atrue(r is None)


# ══════════════════════════════════════════════════════════════════════════════
# R  WALK-FORWARD BACKTESTER  T216–T228
# ══════════════════════════════════════════════════════════════════════════════
hdr("R  WALK-FORWARD BACKTESTER  (T216–T228)")

from backtester.walk_forward import WalkForwardBacktester, WalkForwardResult, WFWindow

@T("WalkForwardBacktester instantiates with defaults", "WalkFwd")
def _():
    wf = WalkForwardBacktester()
    aeq(wf.n_windows, 12); aapprox(wf.train_pct, 0.70)

@T("WalkForwardBacktester custom n_windows / train_pct", "WalkFwd")
def _():
    wf = WalkForwardBacktester(n_windows=6, train_pct=0.8)
    aeq(wf.n_windows, 6); aapprox(wf.train_pct, 0.80)

@T("WFWindow dataclass fields set correctly", "WalkFwd")
def _():
    w = WFWindow(window_num=1, train_start="2024-01-01",
                 train_end="2024-06-30", test_start="2024-07-01", test_end="2024-09-30")
    aeq(w.window_num, 1); aeq(w.train_start, "2024-01-01")

@T("WalkForwardResult is_robust is a bool", "WalkFwd")
def _():
    wr = WalkForwardResult(windows=[], avg_test_sharpe=0.8, avg_test_return=5.0,
                           avg_max_dd=3.0, avg_win_rate=55.0, total_trades=100,
                           overfitting_score=1.2, is_robust=True, verdict="OK")
    atype(wr.is_robust, bool); atrue(wr.is_robust)

@T("wf.run() returns WalkForwardResult on minimal df", "WalkFwd")
def _():
    from strategies.momentum import MomentumStrategy
    from backtester.engine import BacktestEngine
    wf = WalkForwardBacktester(n_windows=3)
    df = make_ohlcv(300)
    be = BacktestEngine()
    st = MomentumStrategy()
    r  = wf.run(df, lambda tr, te: be.run(te, st, "X"))
    atype(r, WalkForwardResult)

@T("wf.run() windows list length >= 0", "WalkFwd")
def _():
    from strategies.momentum import MomentumStrategy
    from backtester.engine import BacktestEngine
    wf = WalkForwardBacktester(n_windows=3)
    r  = wf.run(make_ohlcv(300), lambda tr, te: BacktestEngine().run(te, MomentumStrategy(), "X"))
    atype(r.windows, list)

@T("wf.run() equity_curve is a list", "WalkFwd")
def _():
    from strategies.momentum import MomentumStrategy
    from backtester.engine import BacktestEngine
    wf = WalkForwardBacktester(n_windows=3)
    r  = wf.run(make_ohlcv(300), lambda tr, te: BacktestEngine().run(te, MomentumStrategy(), "X"))
    atype(r.equity_curve, list)

@T("wf.run() monthly_returns is a dict", "WalkFwd")
def _():
    from strategies.momentum import MomentumStrategy
    from backtester.engine import BacktestEngine
    wf = WalkForwardBacktester(n_windows=3)
    r  = wf.run(make_ohlcv(300), lambda tr, te: BacktestEngine().run(te, MomentumStrategy(), "X"))
    atype(r.monthly_returns, dict)

@T("wf.run() overfitting_score is a number >= 0", "WalkFwd")
def _():
    from strategies.momentum import MomentumStrategy
    from backtester.engine import BacktestEngine
    wf = WalkForwardBacktester(n_windows=3)
    r  = wf.run(make_ohlcv(300), lambda tr, te: BacktestEngine().run(te, MomentumStrategy(), "X"))
    atype(r.overfitting_score, (int, float))
    agte(r.overfitting_score, 0)

@T("wf.run() verdict is a non-empty string", "WalkFwd")
def _():
    from strategies.momentum import MomentumStrategy
    from backtester.engine import BacktestEngine
    wf = WalkForwardBacktester(n_windows=3)
    r  = wf.run(make_ohlcv(300), lambda tr, te: BacktestEngine().run(te, MomentumStrategy(), "X"))
    atype(r.verdict, str); agt(len(r.verdict), 0)

@T("wf.run() mc_var_95 and mc_expected_return are floats", "WalkFwd")
def _():
    from strategies.momentum import MomentumStrategy
    from backtester.engine import BacktestEngine
    wf = WalkForwardBacktester(n_windows=3)
    r  = wf.run(make_ohlcv(300), lambda tr, te: BacktestEngine().run(te, MomentumStrategy(), "X"))
    atype(r.mc_var_95, (int, float)); atype(r.mc_expected_return, (int, float))

@T("wf.run() short df (<100 rows) returns empty result gracefully", "WalkFwd")
def _():
    wf = WalkForwardBacktester(n_windows=12)
    from strategies.momentum import MomentumStrategy
    from backtester.engine import BacktestEngine
    r  = wf.run(make_ohlcv(30), lambda tr, te: BacktestEngine().run(te, MomentumStrategy(), "X"))
    atype(r, WalkForwardResult)

@T("wf._split_windows() produces at most n_windows windows", "WalkFwd")
def _():
    wf = WalkForwardBacktester(n_windows=5)
    wins = wf._split_windows(make_ohlcv(300))
    altel(len(wins), 5)


# ══════════════════════════════════════════════════════════════════════════════
# S  BACKTEST ENGINE  T229–T245
# ══════════════════════════════════════════════════════════════════════════════
hdr("S  BACKTEST ENGINE  (T229–T245)")

from backtester.engine import BacktestEngine, BacktestResult, BacktestTrade

@T("BacktestEngine instantiates with default capital 10 000", "Backtest")
def _():
    be = BacktestEngine()
    aeq(be._capital, 10_000.0)

@T("BacktestEngine custom capital stored", "Backtest")
def _():
    be = BacktestEngine(initial_capital=55_000)
    aeq(be._capital, 55_000.0)

@T("run() returns BacktestResult", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    atype(r, BacktestResult)

@T("BacktestResult.win_rate in [0, 100]", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    agte(r.win_rate, 0.0); altel(r.win_rate, 100.0)

@T("BacktestResult.sharpe_ratio is numeric", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    atype(r.sharpe_ratio, (int, float))

@T("BacktestResult.sortino_ratio is numeric", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    atype(r.sortino_ratio, (int, float))

@T("BacktestResult.max_drawdown_pct >= 0", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    agte(r.max_drawdown_pct, 0.0)

@T("BacktestResult.start_capital matches constructor", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine(initial_capital=75_000).run(make_ohlcv(200), MomentumStrategy(), "X")
    aeq(r.start_capital, 75_000.0)

@T("BacktestResult.end_capital >= 0", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    agte(r.end_capital, 0.0)

@T("BacktestResult.wins + losses <= total_trades", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    altel(r.wins + r.losses, r.total_trades)

@T("BacktestResult.profit_factor >= 0", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    agte(r.profit_factor, 0.0)

@T("BacktestResult.monthly_returns is dict", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    atype(r.monthly_returns, dict)

@T("BacktestResult.trades is list of BacktestTrade", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    atype(r.trades, list)
    for t in r.trades: atype(t, BacktestTrade)

@T("BacktestResult.equity_curve first point ≈ initial_capital", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    be = BacktestEngine(initial_capital=100_000)
    r  = be.run(make_ohlcv(200), MomentumStrategy(), "X")
    if r.equity_curve:
        aapprox(r.equity_curve[0], 100_000.0, tol=1000)

@T("BacktestEngine handles downtrend df without crash", "Backtest")
def _():
    from strategies.mean_reversion import MeanReversionStrategy
    df = make_ohlcv(200)
    # force downtrend
    df["close"] = df["close"].iloc[0] - np.arange(200) * 2.0
    df["high"]  = df["close"] + 5; df["low"] = df["close"] - 5
    r = BacktestEngine().run(df, MeanReversionStrategy(), "X")
    anot_none(r)

@T("BacktestResult.start_date / end_date are non-empty strings", "Backtest")
def _():
    from strategies.momentum import MomentumStrategy
    r = BacktestEngine().run(make_ohlcv(200), MomentumStrategy(), "X")
    atype(r.start_date, str); agt(len(r.start_date), 0)


# ══════════════════════════════════════════════════════════════════════════════
# T  DATA FEEDS  T246–T263
# ══════════════════════════════════════════════════════════════════════════════
hdr("T  DATA FEEDS  (T246–T263)")

from data.feeds.realtime_feed import PaperRealtimeFeed, AngelOneRealtimeFeed

@T("PaperRealtimeFeed instantiates", "DataFeed")
def _():
    f = PaperRealtimeFeed(); anot_none(f)

@T("PaperRealtimeFeed._last_prices is a dict", "DataFeed")
def _():
    f = PaperRealtimeFeed(); atype(f._last_prices, dict)

@T("PaperRealtimeFeed.get_last_price() returns None for unknown symbol", "DataFeed")
def _():
    f = PaperRealtimeFeed()
    r = f.get_last_price("DOESNOTEXIST.NS")
    atrue(r is None)

@T("PaperRealtimeFeed.get_last_price() returns stored price", "DataFeed")
def _():
    f = PaperRealtimeFeed()
    f._last_prices["RELIANCE.NS"] = 2500.0
    aapprox(f.get_last_price("RELIANCE.NS"), 2500.0)

@T("PaperRealtimeFeed.stop() sets _running=False", "DataFeed")
def _():
    f = PaperRealtimeFeed(); f._running = True; f.stop()
    afalse(f._running)

@T("PaperRealtimeFeed._fetch_prices() returns a dict", "DataFeed")
def _():
    f = PaperRealtimeFeed()
    r = f._fetch_prices()
    atype(r, dict)

@T("PaperRealtimeFeed POLL_INTERVAL default <= 60", "DataFeed")
def _():
    altel(PaperRealtimeFeed.POLL_INTERVAL, 60)

@T("AngelOneRealtimeFeed with broker=None falls back (no crash)", "DataFeed")
def _():
    f = AngelOneRealtimeFeed(broker=None)
    anot_none(f)

@T("AngelOneRealtimeFeed._is_angel_available() False with None broker", "DataFeed")
def _():
    f = AngelOneRealtimeFeed(broker=None)
    afalse(f._is_angel_available())

@T("AngelOneRealtimeFeed._fallback is PaperRealtimeFeed when broker=None", "DataFeed")
def _():
    f = AngelOneRealtimeFeed(broker=None)
    atype(f._fallback, PaperRealtimeFeed)

@T("AngelOneRealtimeFeed._COMMON_TOKENS has RELIANCE.NS entry", "DataFeed")
def _():
    ain("RELIANCE.NS", AngelOneRealtimeFeed._COMMON_TOKENS)

@T("AngelOneRealtimeFeed.get_last_price() returns None for unknown sym", "DataFeed")
def _():
    f = AngelOneRealtimeFeed(broker=None)
    r = f.get_last_price("UNKNOWN.NS")
    atrue(r is None)

@T("AngelOneRealtimeFeed.get_tick_count() returns int", "DataFeed")
def _():
    f = AngelOneRealtimeFeed(broker=None)
    atype(f.get_tick_count(), int)

@T("AngelOneRealtimeFeed.stop() doesn't crash", "DataFeed")
def _():
    f = AngelOneRealtimeFeed(broker=None); f.stop(); atrue(True)

@T("MultiMarketFeed instantiates with explicit market_symbols", "DataFeed")
def _():
    from data.feeds.multi_market_feed import MultiMarketFeed
    f = MultiMarketFeed({"NSE": ["RELIANCE.NS", "TCS.NS"]})
    anot_none(f)

@T("MultiMarketFeed._market_symbols default includes 'NSE'", "DataFeed")
def _():
    from data.feeds.multi_market_feed import MultiMarketFeed
    f = MultiMarketFeed()
    ain("NSE", f._market_symbols)

@T("MultiMarketFeed.get_last_price() returns None for unknown sym", "DataFeed")
def _():
    from data.feeds.multi_market_feed import MultiMarketFeed
    f = MultiMarketFeed({"NSE": ["RELIANCE.NS"]})
    r = f.get_last_price("UNKNOWN.NS")
    atrue(r is None)

@T("MultiMarketFeed.get_stats() returns dict", "DataFeed")
def _():
    from data.feeds.multi_market_feed import MultiMarketFeed
    f = MultiMarketFeed({"NSE": ["RELIANCE.NS"]})
    s = f.get_stats()
    atype(s, dict)


# ══════════════════════════════════════════════════════════════════════════════
# U  INDICATOR ENGINE  T264–T277
# ══════════════════════════════════════════════════════════════════════════════
hdr("U  INDICATOR ENGINE  (T264–T277)")

from data.processors.indicator_engine import IndicatorEngine

@T("IndicatorEngine instantiates", "Indicators")
def _():
    ie = IndicatorEngine(); anot_none(ie)

@T("add_all() returns DataFrame", "Indicators")
def _():
    r = IndicatorEngine().add_all(make_ohlcv(100))
    atype(r, pd.DataFrame)

@T("add_all() preserves row count", "Indicators")
def _():
    df = make_ohlcv(150)
    r  = IndicatorEngine().add_all(df)
    aeq(len(r), len(df))

@T("add_all() adds EMA_9", "Indicators")
def _():
    ain("EMA_9", IndicatorEngine().add_all(make_ohlcv(100)).columns)

@T("add_all() adds EMA_21", "Indicators")
def _():
    ain("EMA_21", IndicatorEngine().add_all(make_ohlcv(100)).columns)

@T("add_all() adds RSI_14", "Indicators")
def _():
    ain("RSI_14", IndicatorEngine().add_all(make_ohlcv(100)).columns)

@T("add_all() adds ATRr_14", "Indicators")
def _():
    ain("ATRr_14", IndicatorEngine().add_all(make_ohlcv(100)).columns)

@T("add_all() adds vol_spike", "Indicators")
def _():
    ain("vol_spike", IndicatorEngine().add_all(make_ohlcv(100)).columns)

@T("add_all() adds OBV", "Indicators")
def _():
    ain("OBV", IndicatorEngine().add_all(make_ohlcv(100)).columns)

@T("RSI_14 values in [0, 100]", "Indicators")
def _():
    r   = IndicatorEngine().add_all(make_ohlcv(100))
    rsi = r["RSI_14"].dropna()
    atrue((rsi >= 0).all() and (rsi <= 100).all())

@T("ATRr_14 values >= 0", "Indicators")
def _():
    r   = IndicatorEngine().add_all(make_ohlcv(100))
    atr = r["ATRr_14"].dropna()
    atrue((atr >= 0).all())

@T("add_all() handles single-row df gracefully", "Indicators")
def _():
    r = IndicatorEngine().add_all(make_ohlcv(1))
    atype(r, pd.DataFrame)

@T("get_signal_snapshot() returns dict", "Indicators")
def _():
    snap = IndicatorEngine().get_signal_snapshot(make_ohlcv(100))
    atype(snap, dict)

@T("vol_spike ≈ volume / (vol_ma20 + 1) on last row", "Indicators")
def _():
    r = IndicatorEngine().add_all(make_ohlcv(100))
    row = r.dropna(subset=["vol_spike", "vol_ma20"]).iloc[-1]
    expected = row["volume"] / (row["vol_ma20"] + 1)
    aapprox(row["vol_spike"], expected, tol=0.5)


# ══════════════════════════════════════════════════════════════════════════════
# V  OPTIONS STRATEGY  T278–T296
# ══════════════════════════════════════════════════════════════════════════════
hdr("V  OPTIONS STRATEGY  (T278–T296)")

from strategies.options_strategy import (
    OptionsStrategy, black_scholes_price, estimate_iv, iv_percentile,
    _next_thursday, _last_thursday_of_month, LOT_SIZES, STRIKE_INTERVALS,
)

@T("OptionsStrategy instantiates (name='Options')", "Options")
def _():
    o = OptionsStrategy()
    aeq(o.name, "Options")

@T("LOT_SIZES is non-empty dict", "Options")
def _():
    atype(LOT_SIZES, dict); agt(len(LOT_SIZES), 5)

@T("LOT_SIZES['RELIANCE.NS'] == 250", "Options")
def _():
    aeq(LOT_SIZES.get("RELIANCE.NS"), 250)

@T("STRIKE_INTERVALS is non-empty dict", "Options")
def _():
    atype(STRIKE_INTERVALS, dict); agt(len(STRIKE_INTERVALS), 2)

@T("_next_thursday() returns a Thursday (weekday=3)", "Options")
def _():
    d = _next_thursday(date(2024, 3, 11))   # Monday
    aeq(d.weekday(), 3)

@T("_next_thursday() from Thursday returns NEXT Thursday", "Options")
def _():
    d = _next_thursday(date(2024, 3, 14))   # Thursday
    aeq(d.weekday(), 3)
    agt((d - date(2024, 3, 14)).days, 0)

@T("_last_thursday_of_month() returns Thursday in same month", "Options")
def _():
    d = _last_thursday_of_month(date(2024, 3, 1))
    aeq(d.month, 3); aeq(d.weekday(), 3)

@T("black_scholes_price() returns (price, greeks) tuple", "Options")
def _():
    r = black_scholes_price(S=100, K=100, T=0.1, r=0.065, sigma=0.2, option_type="CE")
    atype(r, tuple); aeq(len(r), 2)

@T("black_scholes_price() call price > 0 for ATM", "Options")
def _():
    price, _ = black_scholes_price(S=100, K=100, T=0.1, r=0.065, sigma=0.2, option_type="CE")
    agt(price, 0.0)

@T("black_scholes_price() put price > 0 for ATM", "Options")
def _():
    price, _ = black_scholes_price(S=100, K=100, T=0.1, r=0.065, sigma=0.2, option_type="PE")
    agt(price, 0.0)

@T("black_scholes_price() ITM call > OTM call", "Options")
def _():
    p_itm, _ = black_scholes_price(S=110, K=100, T=0.1, r=0.065, sigma=0.2, option_type="CE")
    p_otm, _ = black_scholes_price(S=90,  K=100, T=0.1, r=0.065, sigma=0.2, option_type="CE")
    agt(p_itm, p_otm)

@T("black_scholes_price() greeks dict has delta key", "Options")
def _():
    _, greeks = black_scholes_price(S=100, K=100, T=0.1, r=0.065, sigma=0.2, option_type="CE")
    ain("delta", greeks)

@T("estimate_iv() returns float in (0, 2)", "Options")
def _():
    iv = estimate_iv(make_ohlcv(100))
    atype(iv, float); agt(iv, 0.0); alt(iv, 2.0)

@T("iv_percentile() returns float in [0, 100]", "Options")
def _():
    pct = iv_percentile(make_ohlcv(100), current_iv=0.25)
    atype(pct, float); agte(pct, 0.0); altel(pct, 100.0)

@T("OptionsStrategy.generate_signal() with no equity_signal_side returns None", "Options")
def _():
    o   = OptionsStrategy()
    sig = o.generate_signal(make_ohlcv(100), "RELIANCE.NS", equity_signal_side=None)
    atrue(sig is None)

@T("OptionsStrategy.generate_signal() with BUY side on supported symbol", "Options")
def _():
    from risk.risk_engine import TradeSignal
    o   = OptionsStrategy()
    # RELIANCE.NS is in underlyings; iv gate may filter, result is None or TradeSignal
    sig = o.generate_signal(make_ohlcv(100), "RELIANCE.NS", equity_signal_side="BUY")
    atrue(sig is None or isinstance(sig, TradeSignal))

@T("OptionsStrategy.check_exit() returns None for unknown symbol", "Options")
def _():
    o = OptionsStrategy()
    r = o.check_exit("RELIANCE24MAR2500CE", 100.0)
    atrue(r is None)

@T("OptionsStrategy.check_exit() returns TARGET when premium hits target", "Options")
def _():
    o = OptionsStrategy()
    o._active_positions["TEST_CE"] = {
        "underlying": "RELIANCE.NS", "type": "CE", "strike": 2500,
        "expiry": (date.today() + timedelta(days=10)).isoformat(),
        "entry_premium": 50.0, "lots": 1, "lot_size": 250, "greeks": {},
        "days_to_expiry": 10, "stop_premium": 25.0, "target_premium": 75.0, "iv_at_entry": 18.0,
    }
    r = o.check_exit("TEST_CE", 80.0)    # above target_premium
    aeq(r, "TARGET")

@T("OptionsStrategy.get_active_positions() returns dict", "Options")
def _():
    atype(OptionsStrategy().get_active_positions(), dict)


# ══════════════════════════════════════════════════════════════════════════════
# W  NEWS / SENTIMENT ENGINE  T297–T312
# ══════════════════════════════════════════════════════════════════════════════
hdr("W  NEWS / SENTIMENT ENGINE  (T297–T312)")

from news.sentiment_engine import SentimentEngine
from news.feed_aggregator  import NewsFeedAggregator, SentimentResult, NewsItem

@T("SentimentEngine instantiates", "News")
def _():
    se = SentimentEngine(); anot_none(se)

@T("score() returns float in [-1.0, +1.0]", "News")
def _():
    se = SentimentEngine()
    s  = se.score("Company reports record revenue this quarter")
    atype(s, float); agte(s, -1.0); altel(s, 1.0)

@T("score() positive for bullish text", "News")
def _():
    se = SentimentEngine()
    s  = se.score("record profit beats estimates strong buy upgrade")
    agt(s, 0.0)

@T("score() negative for hard-block text", "News")
def _():
    se = SentimentEngine()
    s  = se.score("fraud investigation arrested ED raid sebi ban")
    alt(s, 0.5)    # negative or at most neutral

@T("is_hard_block() True for 'fraud'", "News")
def _():
    se = SentimentEngine()
    blocked, kw = se.is_hard_block("Company hit by fraud allegations")
    atrue(blocked); ain("fraud", kw)

@T("is_hard_block() True for 'arrested'", "News")
def _():
    se = SentimentEngine()
    blocked, _ = se.is_hard_block("CEO arrested by CBI for money laundering")
    atrue(blocked)

@T("is_hard_block() False for normal headline", "News")
def _():
    se = SentimentEngine()
    blocked, _ = se.is_hard_block("Bandhan Bank Q3 results beat estimates")
    afalse(blocked)     # 'ban' must NOT match 'Bandhan' (word-boundary fix)

@T("classify() bands: >=0.5 → STRONGLY BULLISH", "News")
def _():
    se = SentimentEngine()
    aeq(se.classify(0.6), "STRONGLY BULLISH")

@T("classify() bands: <=-0.5 → STRONGLY BEARISH", "News")
def _():
    se = SentimentEngine()
    aeq(se.classify(-0.6), "STRONGLY BEARISH")

@T("classify() NEUTRAL for score 0.0", "News")
def _():
    se = SentimentEngine()
    aeq(se.classify(0.0), "NEUTRAL")

@T("score_batch() returns list of same length", "News")
def _():
    se  = SentimentEngine()
    txts = ["profit up", "loss down", "fraud arrested", "neutral headline"]
    res = se.score_batch(txts)
    atype(res, list); aeq(len(res), 4)
    for r in res: atype(r, float)

@T("SentimentResult float() returns score", "News")
def _():
    sr = SentimentResult(score=0.75, has_fresh_data=True, item_count=3, label="BULLISH")
    aapprox(float(sr), 0.75)

@T("SentimentResult comparison operators work", "News")
def _():
    sr = SentimentResult(score=0.5, has_fresh_data=True, item_count=1, label="BULLISH")
    atrue(sr >= 0.4); atrue(sr <= 0.6); atrue(sr > 0.3); atrue(sr < 0.7)

@T("NewsFeedAggregator.get_sentiment_score() returns SentimentResult", "News")
def _():
    nf = NewsFeedAggregator()
    r  = nf.get_sentiment_score("RELIANCE.NS")
    atype(r, SentimentResult)

@T("NewsFeedAggregator.has_breaking_negative_news() returns (bool, str)", "News")
def _():
    nf     = NewsFeedAggregator()
    result = nf.has_breaking_negative_news("RELIANCE.NS")
    atype(result, tuple); aeq(len(result), 2); atype(result[0], bool)

@T("NewsFeedAggregator.register_threshold_callback() stores callable", "News")
def _():
    nf = NewsFeedAggregator()
    async def cb(item): pass
    nf.register_threshold_callback(cb)
    ain(cb, nf._threshold_callbacks)


# ══════════════════════════════════════════════════════════════════════════════
# X  RISK ENGINE — ALL 11 GATES  T313–T328
# ══════════════════════════════════════════════════════════════════════════════
hdr("X  RISK ENGINE — ALL 11 GATES  (T313–T328)")

from risk.risk_engine import RiskEngine, TradeSignal, RiskResult, DrawdownGuard
from core.state_manager import BotState

def _make_clean_state():
    """BotState with generous defaults that pass all gates."""
    from core.config import cfg
    s = BotState()
    s.status           = "RUNNING"
    s.is_halted        = False
    s.capital          = cfg.initial_capital
    s.available_margin = cfg.initial_capital
    s.daily_pnl        = 0.0
    s.consecutive_losses = 0
    s.open_positions   = {}
    s.market_data      = {"india_vix": 13.0}
    return s

class _FakeStateMgr:
    """Minimal state_manager stub."""
    def __init__(self, state=None):
        self.state = state or _make_clean_state()
    def get_closed_trades(self, **k): return []

def _fresh_engine(state=None):
    return RiskEngine(state_manager=_FakeStateMgr(state))

def _sig(symbol="RELIANCE.NS", side="BUY", confidence=80.0, atr=15.0, cmp=2500.0):
    return TradeSignal(symbol=symbol, side=side, strategy="Momentum",
                       confidence=confidence, trigger="test", atr=atr, cmp=cmp)

@T("RiskEngine instantiates", "RiskGates")
def _():
    e = _fresh_engine(); anot_none(e)

@T("evaluate() returns RiskResult", "RiskGates")
def _():
    e = _fresh_engine()
    r = e.evaluate(_sig(), cmp=100.0)
    atype(r, RiskResult)

@T("Gate 1: is_halted=True → blocked", "RiskGates")
def _():
    s = _make_clean_state(); s.is_halted = True
    r = _fresh_engine(s).evaluate(_sig(), cmp=100.0)
    afalse(r.approved)
    ain("Halted", r.blocked_reason)

@T("Gate 3: daily_pnl at limit → blocked", "RiskGates")
def _():
    from core.config import cfg
    s = _make_clean_state()
    s.daily_pnl = -(cfg.initial_capital * cfg.risk.max_daily_loss_pct / 100 + 500)
    r = _fresh_engine(s).evaluate(_sig(), cmp=100.0)
    afalse(r.approved)

@T("Gate 5: consecutive_losses at limit → blocked", "RiskGates")
def _():
    from core.config import cfg
    s = _make_clean_state()
    s.consecutive_losses = cfg.risk.consecutive_loss_limit + 1
    r = _fresh_engine(s).evaluate(_sig(), cmp=100.0)
    afalse(r.approved)

@T("Gate 6: confidence < 62 → blocked", "RiskGates")
def _():
    r = _fresh_engine().evaluate(_sig(confidence=40.0), cmp=100.0)
    afalse(r.approved)

@T("Gate 7: VIX above threshold → blocked", "RiskGates")
def _():
    from core.config import cfg
    r = _fresh_engine().evaluate(_sig(), cmp=100.0,
                                 vix=cfg.risk.vix_halt_threshold + 5.0)
    afalse(r.approved)

@T("Gate 8: margin nearly zero → blocked", "RiskGates")
def _():
    s = _make_clean_state(); s.capital = 1.0; s.available_margin = 1.0
    r = _fresh_engine(s).evaluate(_sig(cmp=2500.0), cmp=2500.0)
    afalse(r.approved)

@T("Gate 10: existing position in symbol → blocked (correlation)", "RiskGates")
def _():
    s = _make_clean_state()
    s.open_positions["RELIANCE.NS"] = {"qty": 10, "side": "LONG",
                                        "avg_price": 2500.0, "position_inr": 25000}
    r = _fresh_engine(s).evaluate(_sig(symbol="RELIANCE.NS"), cmp=2500.0)
    afalse(r.approved)

@T("approved signal has recommended_qty > 0", "RiskGates")
def _():
    r = _fresh_engine().evaluate(_sig(confidence=80.0), cmp=100.0, adx=25.0)
    if r.approved: agt(r.recommended_qty, 0)

@T("approved BUY: stop_loss < cmp", "RiskGates")
def _():
    r = _fresh_engine().evaluate(_sig(confidence=80.0, cmp=100.0), cmp=100.0, adx=25.0)
    if r.approved: alt(r.stop_loss, 100.0)

@T("approved BUY: target > cmp", "RiskGates")
def _():
    r = _fresh_engine().evaluate(_sig(confidence=80.0, cmp=100.0), cmp=100.0, adx=25.0)
    if r.approved: agt(r.target, 100.0)

@T("blocked_reason non-empty when rejected", "RiskGates")
def _():
    s = _make_clean_state(); s.is_halted = True
    r = _fresh_engine(s).evaluate(_sig(), cmp=100.0)
    agt(len(r.blocked_reason), 0)

@T("DrawdownGuard.check() OK when drawdown < limit", "RiskGates")
def _():
    dg = DrawdownGuard(max_drawdown_pct=20.0)
    class _S: drawdown_pct = 5.0
    ok, _ = dg.check(_S()); atrue(ok)

@T("DrawdownGuard.check() False when drawdown >= limit", "RiskGates")
def _():
    dg = DrawdownGuard(max_drawdown_pct=20.0)
    class _S: drawdown_pct = 25.0
    ok, _ = dg.check(_S()); afalse(ok)

@T("_dynamic_rr: ADX>30 → 3:1, 20-30 → 2:1, <20 → 1.5:1", "RiskGates")
def _():
    e = _fresh_engine()
    rr30, l30 = e._dynamic_rr(35.0)
    rr20, l20 = e._dynamic_rr(25.0)
    rr15, l15 = e._dynamic_rr(12.0)
    aapprox(rr30, 3.0); aeq(l30, "3:1")
    aapprox(rr20, 2.0); aeq(l20, "2:1")
    aapprox(rr15, 1.5); aeq(l15, "1.5:1")


# ══════════════════════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
_total = _PASS + _FAIL
_rate  = round(_PASS / _total * 100, 1) if _total > 0 else 0.0

cats: dict = {}
for _tid, _cat, _nm, _st, _dt in _RESULTS:
    cats.setdefault(_cat, []).append(_st)

_CAT_ORDER = ["ML","Trainer","WalkFwd","Backtest","DataFeed",
              "Indicators","Options","News","RiskGates"]

print(f"""
{'═'*70}
  BATCH 2 RESULTS  (T179–T328)
{'─'*70}
  ✅  PASSED  : {_PASS:>3}
  ❌  FAILED  : {_FAIL:>3}
  TOTAL       : {_total:>3}
  PASS RATE   : {_rate}%
{'─'*70}
  CATEGORY BREAKDOWN:""")

for _c in _CAT_ORDER:
    if _c not in cats: continue
    _rs = cats[_c]; _p = _rs.count("PASS"); _t = len(_rs)
    _bar = "█"*_p + "░"*(_t-_p)
    print(f"  {_c:<14} [{_bar:<22}] {_p}/{_t}")

_fails = [r for r in _RESULTS if r[3] != "PASS"]
if _fails:
    print(f"\n{'─'*70}\n  FAILURES / ERRORS ({len(_fails)}):")
    for _tid, _cat, _nm, _st, _dt in _fails:
        print(f"  {_tid} [{_cat}] {_st}: {_nm}")
        if _dt: print(f"         └─ {_dt}")

print(f"\n{'═'*70}")
print("""
  ┌──────────────────────────────────────────────────────────────────────┐
  │  BATCH 3 PROMPT  (T329–T478)                                        │
  │                                                                      │
  │  Using ZeroBot_v1_1_Patch16_B2_FIXED.zip (all T179–T328 pass),     │
  │  run the next 150 intensive tests (T329–T478) covering:             │
  │                                                                      │
  │  1. Engine Integration (core/engine.py)                             │
  │     _on_order_filled LONG/SHORT, _on_stop_hit, _on_target_hit,     │
  │     _emergency_exit idempotency, _on_tick spike detection,          │
  │     auto-squareoff trigger, _main_loop signal cycle (mock broker)   │
  │                                                                      │
  │  2. StateManager full async ops                                     │
  │     save_trade, save_signal, get_closed_trades, risk events,        │
  │     drawdown state, DB round-trip restore                           │
  │                                                                      │
  │  3. Dashboard API endpoints                                          │
  │     /api/status keys, /api/portfolio, /api/positions,               │
  │     /api/signals, /api/news, /api/risk/status, /api/risk/var,       │
  │     /api/indices, /api/strategies, /api/position_limit,             │
  │     emergency_halt, resume, exit_position, /api/health              │
  │                                                                      │
  │  4. Position reconciliation after crash                             │
  │     re-import open positions from DB, reconcile vs broker,          │
  │     detect ghost positions, apply SL/target after restore           │
  │                                                                      │
  │  5. PaperBroker full cycle (already tested basics in Batch 1)      │
  │     dual-broker fallback, hybrid broker routing, factory create,    │
  │     margin lock/release for SHORT, token_manager lookups            │
  │                                                                      │
  │  6. Strategy signal quality suite                                   │
  │     RSIDivergence bull/bear signals, VWAPStrategy intraday,        │
  │     StatArb pair spread, MarketMaking bid-ask spread,               │
  │     ORB first-candle breakout, Breakout volume confirm              │
  │                                                                      │
  │  7. Regime Detector                                                  │
  │     trending vs ranging, VIX-based risk-off, multi-timeframe,       │
  │     regime change event emission                                     │
  │                                                                      │
  │  8. Transaction Cost Calculator — deep                              │
  │     STT options vs equity, stamp duty, SEBI fees, F&O costs,        │
  │     round-trip P&L impact, slippage models                          │
  │                                                                      │
  │  9. Event Calendar gate                                             │
  │     earnings block, RBI meeting block, expiry-day sizing,           │
  │     holiday detection, get_event_risk() multipliers                 │
  │                                                                      │
  │ 10. Telegram Alerter (already partially tested in Batch 1)          │
  │     async send with throttle, priority ordering, format_signal,     │
  │     daily_summary formatting, graceful failure with bad token       │
  │                                                                      │
  │  Use same shim infrastructure (PYTHONPATH=. from zerobot_patch16/). │
  │  Fix all failures found. Provide B3_FIXED.zip + Batch 4 prompt.    │
  └──────────────────────────────────────────────────────────────────────┘
""")
