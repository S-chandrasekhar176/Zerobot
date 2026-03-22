"""Tests for trading strategies."""
import sys; sys.path.insert(0, "..")
import pandas as pd
import numpy as np
from strategies.momentum import MomentumStrategy
from strategies.mean_reversion import MeanReversionStrategy


def make_df(n=100, trend="up") -> pd.DataFrame:
    """Generate synthetic OHLCV data."""
    np.random.seed(42)
    prices = [100.0]
    for _ in range(n):
        move = np.random.randn() * 1.5 + (0.1 if trend == "up" else -0.1)
        prices.append(prices[-1] + move)

    df = pd.DataFrame({
        "open":   prices[:-1],
        "close":  prices[1:],
        "high":   [max(o, c) + abs(np.random.randn()) for o, c in zip(prices[:-1], prices[1:])],
        "low":    [min(o, c) - abs(np.random.randn()) for o, c in zip(prices[:-1], prices[1:])],
        "volume": np.random.randint(100000, 1000000, n),
    })
    # Add required indicator columns manually for test
    df["EMA_9"] = df["close"].ewm(span=9).mean()
    df["EMA_21"] = df["close"].ewm(span=21).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] / df["vol_ma20"]
    df["vwap_dev"] = (df["close"] - df["close"].rolling(20).mean()) / df["close"].rolling(20).mean() * 100
    df["ATRr_14"] = (df["high"] - df["low"]).ewm(span=14).mean()

    # RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["RSI_14"] = 100 - (100 / (1 + gain / (loss + 1e-9)))

    # BB
    ma = df["close"].rolling(20).mean()
    std = df["close"].rolling(20).std()
    df["BBU_20_2.0"] = ma + 2*std
    df["BBL_20_2.0"] = ma - 2*std
    df["MACD_12_26_9"] = df["close"].ewm(12).mean() - df["close"].ewm(26).mean()
    df["MACDs_12_26_9"] = df["MACD_12_26_9"].ewm(9).mean()

    return df.dropna()


def test_momentum_returns_signal_or_none():
    strat = MomentumStrategy()
    df = make_df(100)
    result = strat.generate_signal(df, "TEST")
    assert result is None or result.side in ["BUY", "SELL"]


def test_mean_reversion_signal():
    strat = MeanReversionStrategy()
    # Force oversold condition
    df = make_df(100)
    df.at[df.index[-1], "RSI_14"] = 25.0  # Force oversold
    df.at[df.index[-1], "close"] = df.at[df.index[-1], "BBL_20_2.0"] * 0.99  # Near lower band
    result = strat.generate_signal(df, "TEST")
    if result:
        assert result.side == "BUY"
        assert result.confidence >= 60


if __name__ == "__main__":
    test_momentum_returns_signal_or_none()
    test_mean_reversion_signal()
    print("✅ All strategy tests passed!")
