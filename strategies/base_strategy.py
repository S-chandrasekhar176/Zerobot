"""ZeroBot — Base Strategy (all strategies inherit this)"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import pandas as pd
from risk.risk_engine import TradeSignal
from core.logger import log


class BaseStrategy(ABC):
    def __init__(self, name: str):
        self.name = name
        self.enabled = True

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Optional[TradeSignal]:
        """Analyze DataFrame and return TradeSignal or None."""
        pass

    def log_signal(self, signal: TradeSignal):
        log.info(f"SIGNAL | {signal.symbol} {signal.side} | {self.name} | conf={signal.confidence:.1f}%")
