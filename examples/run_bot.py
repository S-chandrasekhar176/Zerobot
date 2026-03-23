#!/usr/bin/env python3
"""
ZeroBot Z1 — Simple Example Script

This script demonstrates how to initialize and run ZeroBot in paper trading mode.

Usage:
    python examples/run_bot.py                      # Run in paper mode (default)
    python examples/run_bot.py --mode s_mode        # Run with Shoonya WS
    python examples/run_bot.py --symbols RELIANCE TCS INFY     # Custom symbols

See docs/usage.md for complete documentation.
"""

import sys
import argparse
import asyncio
from pathlib import Path
from typing import List, Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import TradingEngine
from core.config import load_config
from core.logger import setup_logger

logger = setup_logger("example_run_bot")


def run_bot(
    mode: str = "paper",
    symbols: Optional[List[str]] = None,
    capital: Optional[float] = None,
    max_duration_minutes: Optional[int] = None,
) -> None:
    """
    Initialize and run ZeroBot in the specified mode.

    Args:
        mode: Trading mode ('paper', 's_mode', 'hybrid', 'dual', 'live')
        symbols: Optional list of symbols to trade (overrides config)
        capital: Optional capital amount (overrides config)
        max_duration_minutes: Optional max runtime (for testing)

    Raises:
        FileNotFoundError: If config files not found
        RuntimeError: If engine initialization fails
    """
    logger.info(f"ZeroBot Z1 — Example Script")
    logger.info(f"Mode: {mode}")

    # Load configuration
    config_file = PROJECT_ROOT / "config" / "settings.yaml"
    env_file = PROJECT_ROOT / "config" / ".env"

    if not config_file.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_file}. "
            "Please create config/settings.yaml"
        )

    logger.info(f"Loading config from {config_file}")
    config = load_config(str(config_file), str(env_file) if env_file.exists() else None)

    # Override config if provided
    if symbols:
        logger.info(f"Overriding symbols: {symbols}")
        config.symbols = symbols

    if capital:
        logger.info(f"Overriding capital: ₹{capital:,.2f}")
        config.capital = capital

    # Initialize engine
    logger.info("Initializing TradingEngine...")
    engine = TradingEngine(config)

    try:
        # Start trading
        logger.info(f"Starting bot in {mode} mode...")
        logger.info("Press Ctrl+C to stop gracefully")

        engine.start(mode=mode)

        # If max_duration_minutes is set (for testing), stop after that time
        if max_duration_minutes:
            logger.info(f"Running for {max_duration_minutes} minutes...")
            asyncio.run(asyncio.sleep(max_duration_minutes * 60))
            logger.info("Max duration reached, stopping...")
            engine.stop()

    except KeyboardInterrupt:
        logger.info("Stop signal received. Shutting down gracefully...")
        engine.stop()
        logger.info("Shutdown complete.")
    except Exception as e:
        logger.error(f"Error during execution: {e}", exc_info=True)
        engine.stop()
        raise


def main():
    """Parse arguments and run the bot."""
    parser = argparse.ArgumentParser(
        description="ZeroBot Z1 — Algorithmic Trading Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Paper trading (Yahoo Finance, simulated execution)
  python examples/run_bot.py

  # S-Mode (Shoonya WS during market hours, Yahoo fallback)
  python examples/run_bot.py --mode s_mode

  # Custom symbols and capital
  python examples/run_bot.py --capital 50000 --symbols RELIANCE TCS INFY

  # Hybrid mode (Angel One data, paper execution)
  python examples/run_bot.py --mode hybrid

  # Live trading (REAL MONEY - use with caution)
  python examples/run_bot.py --mode live --broker shoonya

See docs/usage.md for complete documentation.
        """,
    )

    parser.add_argument(
        "--mode",
        choices=["paper", "s_mode", "hybrid", "dual", "live"],
        default="paper",
        help="Trading mode (default: paper)",
    )
    parser.add_argument(
        "--broker",
        choices=["paper", "shoonya_paper", "angel_paper", "hybrid", "dual", "shoonya", "angel"],
        default="paper",
        help="Broker to use (default: paper)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        help="Starting capital in ₹ (overrides config)",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Symbols to trade (overrides config)",
    )
    parser.add_argument(
        "--max-duration",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Max runtime in minutes (for testing)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Validate mode + broker combination
    if args.mode == "paper" and args.broker not in ["paper", "shoonya_paper", "angel_paper"]:
        logger.warning(f"Paper mode with {args.broker} broker may not work as expected")

    if args.mode == "live" and args.broker in ["paper", "shoonya_paper", "angel_paper"]:
        logger.error("Live mode requires a real broker (shoonya, angel, dual)")
        sys.exit(1)

    # Run
    try:
        run_bot(
            mode=args.mode,
            symbols=args.symbols,
            capital=args.capital,
            max_duration_minutes=args.max_duration,
        )
    except KeyboardInterrupt:
        print("\nShutdown by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
