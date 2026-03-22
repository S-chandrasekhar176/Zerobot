"""
ZeroBot — Indicator Engine
Computes all technical indicators using pandas-ta.
Used by both strategies and ML feature builder.
"""
import pandas as pd
import numpy as np
from typing import Optional
from core.logger import log


class IndicatorEngine:
    """
    Computes all TA indicators on OHLCV DataFrames.
    Input: DataFrame with [open, high, low, close, volume]
    Output: Same DF + all indicator columns
    """

    def add_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add only the indicators actually used by strategies and ML. 
        Previously attempted ta.strategy('All') which computed 200+ indicators — 
        now computes only the ~10 actually needed, reducing CPU by ~95%."""
        try:
            import pandas_ta as ta

            df = df.copy()
            results = [df]

            def safe_add(fn, *args, **kwargs):
                try:
                    result = fn(*args, **kwargs)
                    if result is not None:
                        if isinstance(result, pd.Series):
                            result = result.to_frame()
                        results.append(result)
                except Exception:
                    pass

            safe_add(ta.ema, df["close"], length=9)
            safe_add(ta.ema, df["close"], length=21)
            safe_add(ta.ema, df["close"], length=50)
            safe_add(ta.sma, df["close"], length=200)
            safe_add(ta.rsi, df["close"], length=14)
            safe_add(ta.macd, df["close"], fast=12, slow=26, signal=9)
            safe_add(ta.atr,  df["high"], df["low"], df["close"], length=14)
            safe_add(ta.bbands, df["close"], length=20, std=2)
            safe_add(ta.obv, df["close"], df["volume"])
            safe_add(ta.mfi, df["high"], df["low"], df["close"], df["volume"], length=14)

            try:
                vwap_result = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
                if vwap_result is not None:
                    results.append(vwap_result.to_frame() if isinstance(vwap_result, pd.Series) else vwap_result)
            except Exception:
                pass

            df = pd.concat(results, axis=1)
            df = df.loc[:, ~df.columns.duplicated()]
            df = self._add_custom(df)
            return df

        except ImportError:
            log.warning("pandas-ta not installed — using manual calculations")
            return self._add_manual(df)
        except Exception as e:
            log.warning(f"pandas-ta failed ({e}) — falling back to manual calculations")
            return self._add_manual(df)

    def _add_custom(self, df: pd.DataFrame) -> pd.DataFrame:
        """Custom indicators not in pandas-ta."""
        # Price position in Bollinger Bands (0-1 scale)
        if "BBU_20_2.0" in df.columns and "BBL_20_2.0" in df.columns:
            bbu = df["BBU_20_2.0"]
            bbl = df["BBL_20_2.0"]
            df["bb_position"] = (df["close"] - bbl) / (bbu - bbl + 1e-9)

        # VWAP deviation
        if "VWAP_D" in df.columns:
            df["vwap_dev"] = (df["close"] - df["VWAP_D"]) / df["VWAP_D"] * 100

        # Volume spike (current vs 20-period MA)
        df["vol_ma20"] = df["volume"].rolling(20).mean()
        df["vol_spike"] = df["volume"] / (df["vol_ma20"] + 1)

        # Price momentum (5-period return)
        df["return_5"] = df["close"].pct_change(5)
        df["return_1"] = df["close"].pct_change(1)

        # High-Low range as % of close
        df["hl_pct"] = (df["high"] - df["low"]) / df["close"] * 100

        # Gap from previous close
        df["gap_pct"] = (df["open"] - df["close"].shift(1)) / df["close"].shift(1) * 100

        return df

    def _add_manual(self, df: pd.DataFrame) -> pd.DataFrame:
        """Fallback: compute key indicators manually using only numpy/pandas."""
        df = df.copy()

        # RSI
        df["RSI_14"] = self._rsi(df["close"], 14)

        # EMA
        df["EMA_9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["EMA_21"] = df["close"].ewm(span=21, adjust=False).mean()
        df["EMA_50"] = df["close"].ewm(span=50, adjust=False).mean()
        df["SMA_200"] = df["close"].rolling(200).mean()

        # Bollinger Bands
        ma20 = df["close"].rolling(20).mean()
        std20 = df["close"].rolling(20).std()
        df["BBU_20_2.0"] = ma20 + 2 * std20
        df["BBL_20_2.0"] = ma20 - 2 * std20
        df["BBM_20_2.0"] = ma20

        # ATR
        df["ATRr_14"] = self._atr(df, 14)

        # MACD
        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        df["MACD_12_26_9"] = ema12 - ema26
        df["MACDs_12_26_9"] = df["MACD_12_26_9"].ewm(span=9, adjust=False).mean()

        # Volume
        df["vol_ma20"] = df["volume"].rolling(20).mean()
        df["vol_spike"] = df["volume"] / (df["vol_ma20"] + 1)
        df["return_1"] = df["close"].pct_change(1)
        df["return_5"] = df["close"].pct_change(5)
        df["hl_pct"] = (df["high"] - df["low"]) / df["close"] * 100

        return df

    @staticmethod
    def _rsi(prices: pd.Series, period: int = 14) -> pd.Series:
        delta = prices.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / (loss + 1e-9)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        hl = df["high"] - df["low"]
        hc = (df["high"] - df["close"].shift()).abs()
        lc = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()

    def get_signal_snapshot(self, df: pd.DataFrame) -> dict:
        """Return latest indicator values as a dict (for ML features)."""
        if df.empty or len(df) < 2:
            return {}
        last = df.iloc[-1]
        return {k: round(float(v), 4) for k, v in last.items() if pd.notna(v)}
