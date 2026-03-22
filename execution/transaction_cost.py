"""
ZeroBot — Transaction Cost Calculator
Computes EXACT costs for every trade:
Brokerage + STT + Stamp Duty + Exchange Charges + GST + SEBI Turnover
Ensures strategies are profitable AFTER all costs.
"""
from dataclasses import dataclass
from typing import Dict


@dataclass
class CostBreakdown:
    brokerage: float
    stt: float
    stamp_duty: float
    exchange_charges: float
    gst: float
    sebi_turnover: float
    total: float
    trade_value: float
    break_even_pct: float    # % move needed just to cover costs


class CostCalculator:
    """
    Angel One cost model (as of 2024):
    - Brokerage: ₹20 flat per order (or 0.03% whichever lower)
    - STT: 0.025% on sell side (intraday equity)
    - Stamp Duty: 0.015% on buy side
    - Exchange: 0.00335% NSE
    - GST: 18% on (brokerage + exchange charges)
    - SEBI Turnover: ₹10 per ₹1 Crore
    """

    SEBI_RATE = 10 / 1_00_00_000  # ₹10 per ₹1Cr = 0.000001

    def __init__(self, cfg=None):
        if cfg:
            self._brokerage = cfg.brokerage_per_order
            self._stt_pct = cfg.stt_intraday_pct / 100
            self._stamp_pct = cfg.stamp_duty_pct / 100
            self._exchange_pct = cfg.exchange_charges_pct / 100
            self._gst_pct = cfg.gst_pct / 100
        else:
            self._brokerage = 20.0
            self._stt_pct = 0.025 / 100
            self._stamp_pct = 0.015 / 100
            self._exchange_pct = 0.00335 / 100
            self._gst_pct = 0.18

    def compute(
        self,
        side: str,
        qty: int,
        price: float,
        instrument_type: str = "EQ",   # EQ | FO
    ) -> Dict:
        trade_value = qty * price

        # Brokerage: ₹20 flat or 0.03% whichever is lower
        brokerage = min(self._brokerage, trade_value * 0.0003)

        # STT: only on sell side for intraday equity
        stt = trade_value * self._stt_pct if side == "SELL" else 0.0

        # Stamp: only on buy side
        stamp = trade_value * self._stamp_pct if side == "BUY" else 0.0

        # Exchange charges: both sides
        exchange = trade_value * self._exchange_pct

        # GST: 18% on (brokerage + exchange charges)
        gst = (brokerage + exchange) * self._gst_pct

        # SEBI turnover
        sebi = trade_value * self.SEBI_RATE

        total = brokerage + stt + stamp + exchange + gst + sebi

        # Break-even: what % move just to cover costs
        break_even = (total / trade_value * 100) if trade_value > 0 else 0

        return {
            "brokerage": round(brokerage, 4),
            "stt": round(stt, 4),
            "stamp_duty": round(stamp, 4),
            "exchange_charges": round(exchange, 4),
            "gst": round(gst, 4),
            "sebi_turnover": round(sebi, 6),
            "total": round(total, 2),
            "trade_value": round(trade_value, 2),
            "break_even_pct": round(break_even, 4),
        }

    def round_trip_cost(self, qty: int, buy_price: float, sell_price: float) -> Dict:
        """Calculate total cost for a complete buy-sell round trip."""
        buy_costs = self.compute("BUY", qty, buy_price)
        sell_costs = self.compute("SELL", qty, sell_price)
        total = buy_costs["total"] + sell_costs["total"]
        gross_pnl = (sell_price - buy_price) * qty
        net_pnl = gross_pnl - total
        return {
            "buy_costs": buy_costs,
            "sell_costs": sell_costs,
            "total_costs": round(total, 2),
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "cost_drag_pct": round(total / (qty * buy_price) * 100, 4),
        }


class TaxTracker:
    """
    Tracks tax liability throughout the year.
    Budget 2024: STCG = 20%, LTCG = 12.5% (above ₹1.25L)
    F&O = Business income (ITR-3)
    """

    STCG_RATE = 0.20       # 20% post July 2024
    LTCG_RATE = 0.125      # 12.5%
    LTCG_EXEMPTION = 125000

    def __init__(self):
        self.total_turnover = 0.0
        self.realized_stcg = 0.0
        self.realized_ltcg = 0.0
        self.fo_pnl = 0.0
        self.loss_harvested = 0.0
        self.trades_count = 0

    def add_trade(self, pnl: float, instrument: str = "EQ", holding_days: int = 0):
        self.trades_count += 1
        if instrument == "FO":
            self.fo_pnl += pnl
        elif holding_days > 365:
            self.realized_ltcg += pnl
        else:
            self.realized_stcg += pnl

    def tax_liability(self) -> Dict:
        stcg_tax = max(0, self.realized_stcg * self.STCG_RATE)
        ltcg_taxable = max(0, self.realized_ltcg - self.LTCG_EXEMPTION)
        ltcg_tax = ltcg_taxable * self.LTCG_RATE
        fo_tax = self.fo_pnl * 0.30 if self.fo_pnl > 0 else 0  # Assuming 30% slab

        return {
            "stcg_gains": round(self.realized_stcg, 2),
            "stcg_tax": round(stcg_tax, 2),
            "ltcg_gains": round(self.realized_ltcg, 2),
            "ltcg_taxable": round(ltcg_taxable, 2),
            "ltcg_tax": round(ltcg_tax, 2),
            "fo_pnl": round(self.fo_pnl, 2),
            "fo_tax_estimate": round(fo_tax, 2),
            "total_tax_estimate": round(stcg_tax + ltcg_tax + fo_tax, 2),
        }
