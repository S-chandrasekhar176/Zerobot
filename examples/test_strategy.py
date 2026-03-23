#!/usr/bin/env python3
"""
Example: Test a Single Strategy

Demonstrates how to test individual strategies without running the full engine.

Usage:
    python examples/test_strategy.py momentum          # Test momentum strategy
    python examples/test_strategy.py mean_reversion    # Test mean reversion
    python examples/test_strategy.py --symbol RELIANCE --bars 100
"""

import sys
import argparse
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_config
from core.logger import setup_logger
from core.state_manager import BotState
from data.feeds.realtime_feed import RealtimeFeed
from strategies.momentum import MomentumStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.supertrend import SuperTrendStrategy
from strategies.vwap_strategy import VWAPStrategy

logger = setup_logger("example_test_strategy")

# Strategy mapping
STRATEGIES = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
    "supertrend": SuperTrendStrategy,
    "vwap": VWAPStrategy,
}


def fetch_sample_data(
    symbol: str, timeframe: str = "5m", bars: int = 100
) -> pd.DataFrame:
    """
    Fetch sample OHLCV data for testing.

    Args:
        symbol: NSE symbol (e.g., "RELIANCE")
        timeframe: Time interval ("5m", "15m", "1h")
        bars: Number of bars to fetch

    Returns:
        DataFrame with columns [timestamp, open, high, low, close, volume]
    """
    logger.info(f"Fetching {bars} bars of {timeframe} data for {symbol}...")

    try:
        feed = RealtimeFeed()
        ohlcv = feed.get_ohlcv(symbol, timeframe, bars=bars)

        if ohlcv.empty:
            logger.error(f"No data returned for {symbol}")
            return pd.DataFrame()

        logger.info(f"✓ Fetched {len(ohlcv)} bars")
        return ohlcv

    except Exception as e:
        logger.error(f"Failed to fetch data: {e}")
        # Return dummy data for demonstration
        logger.info("Using dummy data for demonstration...")
        return _create_dummy_data(symbol, bars)


def _create_dummy_data(symbol: str, bars: int) -> pd.DataFrame:
    """Create dummy OHLCV data for testing when live data unavailable."""
    np.random.seed(42)
    dates = pd.date_range("2026-01-01", periods=bars, freq="5min")

    base_price = 2800.0
    prices = base_price + np.cumsum(np.random.randn(bars) * 5)

    return pd.DataFrame({
        "timestamp": dates,
        "open": prices + np.random.randn(bars) * 1,
        "high": prices + abs(np.random.randn(bars)) * 2,
        "low": prices - abs(np.random.randn(bars)) * 2,
        "close": prices,
        "volume": np.random.randint(1000, 100000, bars),
    })


def test_strategy(
    strategy_name: str,
    symbol: str = "RELIANCE",
    bars: int = 100,
    timeframe: str = "5m",
) -> None:
    """
    Test a single strategy.

    Args:
        strategy_name: Name of strategy ('momentum', 'mean_reversion', etc.)
        symbol: NSE symbol to test
        bars: Number of bars to fetch
        timeframe: Timeframe ('5m', '15m', '1h')
    """
    if strategy_name.lower() not in STRATEGIES:
        logger.error(
            f"Unknown strategy: {strategy_name}. "
            f"Available: {', '.join(STRATEGIES.keys())}"
        )
        return

    logger.info(f"Testing {strategy_name.upper()} strategy on {symbol}")
    logger.info(f"Configuration: {bars} bars, {timeframe} timeframe\n")

    # Fetch data
    ohlcv = fetch_sample_data(symbol, timeframe, bars)
    if ohlcv.empty:
        return

    # Initialize strategy
    strategy_class = STRATEGIES[strategy_name.lower()]
    strategy = strategy_class()
    logger.info(f"✓ Initialized {strategy_name} strategy\n")

    # Initialize dummy state
    state = BotState(capital=100000)
    state.open_positions = {}

    # Generate signal
    logger.info("Generating signal...")
    signal = strategy.generate_signal(ohlcv, state)

    # Display results
    logger.info("\n" + "=" * 60)
    logger.info("SIGNAL RESULT")
    logger.info("=" * 60)
    logger.info(f"Symbol:     {signal.get('symbol', symbol)}")
    logger.info(f"Side:       {signal.get('side', 'NONE')}")
    logger.info(f"Confidence: {signal.get('confidence', 0):.1f}%")
    logger.info(f"Reason:     {signal.get('reason', 'N/A')}")

    if signal.get("side"):
        logger.info(f"Qty:        {signal.get('qty', 1)} shares")
        logger.info(f"Order Type: {signal.get('order_type', 'LIMIT')}")
    logger.info("=" * 60 + "\n")

    # Display OHLCV tail for context
    logger.info("Last 5 candles (context):")
    logger.info(ohlcv[["open", "high", "low", "close", "volume"]].tail(5).to_string())
    logger.info("")

    # Interpretation
    if signal.get("side") == "BUY":
        logger.info("✓ BUY signal generated — strategy sees upside potential")
    elif signal.get("side") == "SELL":
        logger.info("✓ SELL signal generated — strategy sees downside risk")
    else:
        logger.info("  NO signal — strategy waiting for better conditions")


def main():
    """Parse arguments and run strategy test."""
    parser = argparse.ArgumentParser(
        description="Test a single ZeroBot strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test momentum strategy on RELIANCE
  python examples/test_strategy.py momentum

  # Test mean reversion on custom symbol
  python examples/test_strategy.py mean_reversion --symbol TCS

  # Test with more bars and custom timeframe
  python examples/test_strategy.py supertrend --bars 200 --timeframe 15m

Available strategies:
  - momentum: Trend-following strategy
  - mean_reversion: Reversion-to-mean strategy
  - supertrend: SuperTrend indicator-based
  - vwap: Volume-weighted average price
        """,
    )

    parser.add_argument(
        "strategy",
        choices=list(STRATEGIES.keys()),
        help="Strategy to test",
    )
    parser.add_argument(
        "--symbol",
        default="RELIANCE",
        help="NSE symbol to analyze (default: RELIANCE)",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=100,
        help="Number of bars to fetch (default: 100)",
    )
    parser.add_argument(
        "--timeframe",
        default="5m",
        choices=["5m", "15m", "1h", "1d"],
        help="Timeframe (default: 5m)",
    )

    args = parser.parse_args()

    try:
        test_strategy(
            strategy_name=args.strategy,
            symbol=args.symbol,
            bars=args.bars,
            timeframe=args.timeframe,
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
