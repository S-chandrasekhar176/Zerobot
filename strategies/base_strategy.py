"""
ZeroBot — Base Strategy

All trading strategies in ZeroBot inherit from BaseStrategy to ensure
consistent signal generation, logging, and integration with the risk engine.

Base Requirements:
- Implement generate_signal() returning Optional[TradeSignal]
- Use provided DataFrame (OHLCV) for analysis
- Return None when no signal (no trade)
- Log all signals for audit trail
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import pandas as pd
from risk.risk_engine import TradeSignal
from core.logger import log


class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.
    
    Strategies analyze market data and generate trading signals.
    All signals must pass the 13-gate risk engine before execution.
    
    Attributes:
        name (str): Strategy identifier (e.g., "Momentum", "MeanReversion")
        enabled (bool): Whether this strategy is active in the trading loop
    """

    def __init__(self, name: str):
        """
        Initialize a strategy.
        
        Args:
            name (str): Human-readable strategy name
        """
        self.name = name
        self.enabled = True

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Optional[TradeSignal]:
        """
        Generate a trading signal based on OHLCV data.
        
        This method is called by the engine every market tick for each configured
        symbol. It should analyze the provided DataFrame and return a TradeSignal
        if trading conditions are met, or None otherwise.
        
        Args:
            df (pd.DataFrame): OHLCV data with columns [timestamp, open, high, low, close, volume].
                              Sorted by timestamp (oldest first).
            symbol (str): NSE symbol being analyzed (e.g., "RELIANCE.NS", "TCS.NS")
        
        Returns:
            Optional[TradeSignal]: A signal with direction (BUY/SELL), confidence, and reason.
                                  Return None if no actionable signal.
        
        Example:
            >>> signal = strategy.generate_signal(ohlcv_data, "RELIANCE.NS")
            >>> if signal:
            >>>     risk_engine.evaluate(signal)  # Risk validation next
        """
        pass

    def log_signal(self, signal: TradeSignal):
        """
        Log a generated signal for audit trail.
        
        Called automatically by the engine to maintain detailed signal history.
        
        Args:
            signal (TradeSignal): The signal to log
        """
        log.info(f"SIGNAL | {signal.symbol} {signal.side} | {self.name} | conf={signal.confidence:.1f}%")
