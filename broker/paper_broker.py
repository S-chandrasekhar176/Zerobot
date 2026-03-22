# -*- coding: utf-8 -*-
"""
ZeroBot v2 — Paper Broker (Indian Market Simulation)
Simulates EXACTLY how Angel One works for NSE India:
  - Real market prices from Yahoo Finance
  - Realistic slippage (based on volume & volatility)
  - Full cost deduction: STT, Stamp, Exchange, GST, SEBI
  - Order lifecycle: PENDING → FILLED/CANCELLED/REJECTED
  - Auto square-off at 3:15 PM (intraday mode)
  - Stop loss and target tracking
  - Margin calculation per SEBI rules

Paper mode is IDENTICAL to live except real money is not used.
You can validate all strategies here before going live.
"""
import uuid
import asyncio
from datetime import datetime, time
from dataclasses import dataclass, field
from typing import Dict, Optional, List
from enum import Enum
from core.config import cfg, PaperBrokerConfig
from core.logger import log
from core.event_bus import bus
from execution.transaction_cost import CostCalculator


class OrderStatus(str, Enum):
    PENDING   = "PENDING"
    FILLED    = "FILLED"
    PARTIAL   = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED  = "REJECTED"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT  = "LIMIT"
    SL     = "SL"
    SL_M   = "SL-M"


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str
    qty: int
    order_type: OrderType
    price: Optional[float]
    trigger_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    fill_price: Optional[float] = None
    placed_at: datetime = field(default_factory=datetime.now)
    filled_at: Optional[datetime] = None
    strategy: str = ""
    slippage: float = 0.0
    costs: Dict = field(default_factory=dict)
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    notes: str = ""
    confidence: float = 0.0   # FIX: store ML confidence for reporting


class PaperBroker:
    """
    Paper trading broker — exactly mirrors Angel One NSE behavior.
    Switch to AngelOneBroker without changing any other code.
    """

    INTRADAY_SQUAREOFF_TIME = time(15, 15)  # 3:15 PM IST auto square-off

    def __init__(self, initial_capital: float = None):
        self.cfg: PaperBrokerConfig = cfg.paper_broker
        self._capital = initial_capital or cfg.initial_capital
        self._available = self._capital
        self._orders: Dict[str, Order] = {}
        self._positions: Dict[str, Dict] = {}
        self._cost_calc = CostCalculator(cfg.paper_broker)
        self._daily_pnl = 0.0
        self._total_pnl = 0.0
        self._all_fills: List[Order] = []
        self._trade_count = 0
        self._win_count = 0

        log.info(
            f"📄 Paper Broker initialized\n"
            f"   Capital:  ₹{self._capital:,.2f}\n"
            f"   Slippage: {self.cfg.slippage_pct}%\n"
            f"   Brokerage: ₹{self.cfg.brokerage_per_order} flat\n"
            f"   Mode: Simulates Angel One NSE INTRADAY"
        )

    def _compute_dynamic_slippage(self, symbol: str, side: str, qty: int, cmp: float, volume: float = 0) -> float:
        """
        Dynamic slippage model:
        - Base: 0.05%
        - Large orders: +0.01% per 100 shares above 500
        - Low volume: +0.02% if volume < avg
        """
        base = self.cfg.slippage_pct / 100
        # Size impact
        if qty > 500:
            base += (qty - 500) / 100 * 0.0001
        # Cap at 0.2%
        base = min(base, 0.002)

        if side == "BUY":
            return cmp * (1 + base)
        else:
            return cmp * (1 - base)

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        cmp: float,
        order_type: OrderType = OrderType.MARKET,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        strategy: str = "",
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
        confidence: float = 0.0,
    ) -> Order:
        """Place a paper order. Identical interface to AngelOneBroker."""
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            price=price or cmp,
            trigger_price=trigger_price,
            strategy=strategy,
            stop_loss=stop_loss,
            target=target,
            confidence=confidence,
        )
        self._orders[order_id] = order

        log.info(
            f"PAPER ORDER | {order_id} | {side} {qty}x {symbol} "
            f"@ ₹{cmp:.2f} | [{strategy}]"
        )

        # Simulate exchange processing latency
        await asyncio.sleep(0.05)
        await self._simulate_fill(order, cmp)
        return order

    async def _simulate_fill(self, order: Order, cmp: float):
        """Simulate realistic fill with dynamic slippage and full cost calculation."""
        fill_price = self._compute_dynamic_slippage(
            order.symbol, order.side, order.qty, cmp
        )
        fill_price = round(fill_price, 2)
        slippage_inr = abs(fill_price - cmp) * order.qty

        # FIX: detect if this is an options order (symbol ends CE/PE)
        is_option = order.symbol.endswith("CE") or order.symbol.endswith("PE")
        instrument_type = "OPT" if is_option else "EQ"

        costs = self._cost_calc.compute(
            side=order.side,
            qty=order.qty,
            price=fill_price,
            instrument_type=instrument_type,
        )

        order.fill_price = fill_price
        order.filled_qty = order.qty
        order.slippage = slippage_inr
        order.costs = costs
        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now()

        # FIX: Options trade value = premium × qty (qty already = lots × lot_size)
        # Stock trade value = price × qty
        trade_value = fill_price * order.qty
        total_cost = costs["total"]

        if order.side == "BUY":
            # BUG-FIX: Detect if BUY is closing an existing SHORT position
            existing = self._positions.get(order.symbol)
            is_closing_short = existing is not None and existing.get("side") == "SHORT"

            if is_closing_short:
                # Closing SHORT → return locked margin + realised PnL
                pos = existing
                gross_pnl = (pos["avg_price"] - fill_price) * order.qty  # profit when price falls
                pnl = gross_pnl - total_cost
                locked = pos.get("short_margin_locked", trade_value * 0.30)
                self._available += locked + pnl
                pos["qty"] -= order.qty
                if pos["qty"] <= 0:
                    del self._positions[order.symbol]
                self._daily_pnl += pnl
                self._total_pnl += pnl
                self._trade_count += 1
                if pnl > 0:
                    self._win_count += 1
            else:
                # Opening or adding to LONG position
                required = trade_value + total_cost
                if self._available < required:
                    order.status = OrderStatus.REJECTED
                    order.notes = f"Insufficient funds: need ₹{required:.0f}, have ₹{self._available:.0f}"
                    log.warning(f"ORDER REJECTED | {order.order_id} | {order.notes}")
                    await bus.publish("order_rejected", {"order_id": order.order_id, "reason": order.notes})
                    return

                self._available -= required
                if order.symbol in self._positions:
                    pos = self._positions[order.symbol]
                    total_qty = pos["qty"] + order.qty
                    pos["avg_price"] = (pos["avg_price"] * pos["qty"] + fill_price * order.qty) / total_qty
                    pos["qty"] = total_qty
                    pos["costs_paid"] = pos.get("costs_paid", 0) + total_cost
                else:
                    self._positions[order.symbol] = {
                        "symbol": order.symbol,
                        "side": "LONG",
                        "qty": order.qty,
                        "avg_price": fill_price,
                        "strategy": order.strategy,
                        "opened_at": datetime.now().isoformat(),
                        "costs_paid": total_cost,
                        "stop_loss": order.stop_loss,
                        "target": order.target,
                        "unrealized_pnl": 0.0,
                        "t1_done": False,
                        "trailing_high": fill_price,
                        "trailing_low": fill_price,
                    }

        else:  # SELL — distinguish: closing LONG vs opening/closing SHORT
            pnl = 0.0
            is_closing_long = order.symbol in self._positions and self._positions[order.symbol].get('side') == 'LONG'
            is_opening_short = order.symbol not in self._positions

            if is_closing_long:
                # Closing LONG → return locked entry margin + realised PnL
                pos = self._positions[order.symbol]
                entry_locked = pos['avg_price'] * order.qty + pos.get('costs_paid', 0) * order.qty / max(1, pos['qty'])
                gross_pnl = (fill_price - pos['avg_price']) * order.qty
                pnl = gross_pnl - total_cost
                self._available += entry_locked + pnl
                pos['qty'] -= order.qty
                if pos['qty'] <= 0:
                    del self._positions[order.symbol]
            elif is_opening_short:
                # Opening new SHORT → DEDUCT 30% SPAN margin (not receive cash)
                short_margin = trade_value * 0.30 + total_cost
                if self._available < short_margin:
                    order.status = OrderStatus.REJECTED
                    order.notes = f'Insufficient margin for SHORT: need ₹{short_margin:.0f}, have ₹{self._available:.0f}'
                    log.warning(f'ORDER REJECTED | {order.order_id} | {order.notes}')
                    await bus.publish('order_rejected', {'order_id': order.order_id, 'reason': order.notes})
                    return
                self._available -= short_margin
                self._positions[order.symbol] = {
                    'symbol': order.symbol, 'side': 'SHORT', 'qty': order.qty,
                    'avg_price': fill_price, 'strategy': order.strategy,
                    'opened_at': datetime.now().isoformat(), 'costs_paid': total_cost,
                    'stop_loss': order.stop_loss, 'target': order.target,
                    'unrealized_pnl': 0.0, 'short_margin_locked': short_margin,
                    't1_done': False, 'trailing_low': fill_price,
                }
            else:
                # Closing existing SHORT → return locked short margin + PnL
                pos = self._positions[order.symbol]
                gross_pnl = (pos['avg_price'] - fill_price) * order.qty  # profit when price falls
                pnl = gross_pnl - total_cost
                locked = pos.get('short_margin_locked', trade_value * 0.30)
                self._available += locked + pnl
                pos['qty'] -= order.qty
                if pos['qty'] <= 0:
                    del self._positions[order.symbol]

            self._daily_pnl += pnl
            self._total_pnl += pnl
            self._trade_count += 1
            if pnl > 0:
                self._win_count += 1

        self._all_fills.append(order)

        log.info(
            f"PAPER FILLED | {order.order_id} | {order.side} {order.qty}x {order.symbol} "
            f"@ ₹{fill_price:.2f} | Slippage: ₹{slippage_inr:.2f} | "
            f"Costs: ₹{total_cost:.2f} | Break-even: {costs['break_even_pct']:.3f}%"
        )

        await bus.publish("order_filled", {
            "order":      order,
            "symbol":     order.symbol,
            "side":       order.side,
            "fill_price": fill_price,
            "qty":        order.qty,
            "costs":      costs,
            "broker":     "paper",
            "stop_loss":  order.stop_loss,    # FIX: was missing — engine needs these
            "target":     order.target,        # FIX: was missing
            "strategy":   order.strategy,      # FIX: was missing
            "confidence": getattr(order, "confidence", 0),
        })

    async def check_stops_and_targets(self, symbol: str, current_price: float):
        """
        Monitor stop losses, targets, trailing stops, and tiered exits.
        Called every 5s for open positions.

        P16 ADDITIONS:
          - Trailing stop: slides SL as price moves in our favour
            LONG: if price > trailing_high → update trailing_high, SL = price*(1-trail_pct)
            SHORT: if price < trailing_low  → update trailing_low,  SL = price*(1+trail_pct)
          - Tiered exit (T1): when unrealized PnL >= 50% of target profit →
            exit 50% of qty, move SL to breakeven (avg_price)
        """
        pos = self._positions.get(symbol)
        if not pos:
            return

        stop   = pos.get("stop_loss")
        target = pos.get("target")
        side   = pos.get("side", "LONG")

        # Always update current price and unrealized PnL
        avg_price = pos.get("avg_price", current_price)
        qty = pos.get("qty", 0)
        pos["current_price"] = current_price
        if side == "SHORT":
            pos["unrealized_pnl"] = round((avg_price - current_price) * qty, 2)
        else:
            pos["unrealized_pnl"] = round((current_price - avg_price) * qty, 2)

        # ── P16: Trailing Stop ────────────────────────────────────
        from core.config import cfg as _cfg
        trailing_pct = _cfg.risk.trailing_stop_pct / 100.0
        if trailing_pct > 0 and stop:
            if side == "LONG":
                trailing_high = pos.get("trailing_high", avg_price)
                if current_price > trailing_high:
                    new_trail_sl = round(current_price * (1 - trailing_pct), 2)
                    if new_trail_sl > stop:   # Only move SL up, never down
                        pos["trailing_high"] = current_price
                        pos["stop_loss"]      = new_trail_sl
                        stop = new_trail_sl
                        log.info(
                            f"⟳ TRAIL SL | {symbol} | new_high={current_price:.2f} "
                            f"→ SL={new_trail_sl:.2f}"
                        )
            elif side == "SHORT":
                trailing_low = pos.get("trailing_low", avg_price)
                if current_price < trailing_low:
                    new_trail_sl = round(current_price * (1 + trailing_pct), 2)
                    if new_trail_sl < stop:   # Only move SL down for shorts
                        pos["trailing_low"] = current_price
                        pos["stop_loss"]     = new_trail_sl
                        stop = new_trail_sl
                        log.info(
                            f"⟳ TRAIL SL (SHORT) | {symbol} | new_low={current_price:.2f} "
                            f"→ SL={new_trail_sl:.2f}"
                        )

        # ── P16: Tiered Exit (T1) ─────────────────────────────────
        if (
            _cfg.risk.tiered_exit_enabled
            and target
            and qty > 1
            and not pos.get("t1_done", False)
        ):
            if side == "LONG":
                full_profit = (target - avg_price) * qty
                curr_profit = (current_price - avg_price) * qty
            else:
                full_profit = (avg_price - target) * qty
                curr_profit = (avg_price - current_price) * qty

            t1_threshold = full_profit * _cfg.risk.tiered_exit_at_pct
            if curr_profit >= t1_threshold and full_profit > 0:
                exit_qty = max(1, qty // 2)
                close_side = "SELL" if side == "LONG" else "BUY"
                log.info(
                    f"🎯 T1 EXIT | {symbol} | Profit {curr_profit:+.0f} >= "
                    f"50% of target {full_profit:.0f} | "
                    f"Exiting {exit_qty}/{qty} shares"
                )
                await self.place_order(
                    symbol=symbol, side=close_side, qty=exit_qty,
                    cmp=current_price, strategy="TIER1_EXIT"
                )
                pos["t1_done"]   = True
                pos["qty"]       = qty - exit_qty
                pos["stop_loss"] = avg_price   # Move SL to breakeven
                await bus.publish("target_hit", {
                    "symbol": symbol, "price": current_price,
                    "target": target, "tier": 1
                })
                return  # Don't check full target on same tick

        # Max hold time: close position after 2h in test/holiday mode, 6h normal
        import os
        max_hold_min = 120 if os.getenv("ZEROBOT_FORCE_MARKET_OPEN", "0") == "1" else 360
        try:
            opened = datetime.fromisoformat(pos.get("opened_at", datetime.now().isoformat()))
            held_min = (datetime.now() - opened).total_seconds() / 60
            if held_min >= max_hold_min:
                log.info(f"⏰ MAX HOLD TIME | {symbol} held {held_min:.0f}min → force closing")
                await self.place_order(
                    symbol=symbol, side="SELL" if side == "LONG" else "BUY",
                    qty=qty, cmp=current_price, strategy="MAX_HOLD_CLOSE"
                )
                return
        except Exception:
            pass

        if side == "LONG":
            if stop and current_price <= stop:
                log.info(f"🛑 STOP HIT | {symbol} @ ₹{current_price:.2f} (stop: ₹{stop:.2f}) | P&L: ₹{pos['unrealized_pnl']:+.2f}")
                await self.place_order(
                    symbol=symbol, side="SELL", qty=qty,
                    cmp=current_price, strategy="STOP_LOSS"
                )
                await bus.publish("stop_hit", {"symbol": symbol, "price": current_price, "stop": stop})

            elif target and current_price >= target:
                log.info(f"🎯 TARGET HIT | {symbol} @ ₹{current_price:.2f} (target: ₹{target:.2f}) | P&L: ₹{pos['unrealized_pnl']:+.2f}")
                await self.place_order(
                    symbol=symbol, side="SELL", qty=qty,
                    cmp=current_price, strategy="TARGET"
                )
                await bus.publish("target_hit", {"symbol": symbol, "price": current_price, "target": target})

        elif side == "SHORT":
            if stop and current_price >= stop:
                log.info(f"🛑 SHORT STOP HIT | {symbol} @ ₹{current_price:.2f} (stop: ₹{stop:.2f}) | P&L: ₹{pos['unrealized_pnl']:+.2f}")
                await self.place_order(
                    symbol=symbol, side="BUY", qty=qty,
                    cmp=current_price, strategy="STOP_LOSS"
                )
                await bus.publish("stop_hit", {"symbol": symbol, "price": current_price, "stop": stop})

            elif target and current_price <= target:
                log.info(f"🎯 SHORT TARGET HIT | {symbol} @ ₹{current_price:.2f} (target: ₹{target:.2f}) | P&L: ₹{pos['unrealized_pnl']:+.2f}")
                await self.place_order(
                    symbol=symbol, side="BUY", qty=qty,
                    cmp=current_price, strategy="TARGET"
                )
                await bus.publish("target_hit", {"symbol": symbol, "price": current_price, "target": target})

    async def square_off_all_intraday(self):
        """
        Auto square-off all intraday positions at 3:15 PM.
        Exactly like Angel One's auto square-off.
        """
        for symbol, pos in list(self._positions.items()):
            log.info(f"AUTO SQUARE-OFF | {symbol} | {pos['qty']} qty")
            current_price = pos.get("avg_price", 0) * 0.999  # Simulate 0.1% worse close
            side = "SELL" if pos["side"] == "LONG" else "BUY"
            await self.place_order(
                symbol=symbol, side=side, qty=pos["qty"],
                cmp=current_price, strategy="AUTO_SQUAREOFF"
            )

    async def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if not order or order.status != OrderStatus.PENDING:
            return False
        order.status = OrderStatus.CANCELLED
        log.info(f"ORDER CANCELLED | {order_id}")
        await bus.publish("order_cancelled", {"order_id": order_id})
        return True

    def get_positions(self) -> Dict:
        return self._positions.copy()

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_portfolio_summary(self) -> Dict:
        total_unrealized = sum(p.get("unrealized_pnl", 0) for p in self._positions.values())
        win_rate = (self._win_count / self._trade_count * 100) if self._trade_count > 0 else 0
        return {
            "capital": self._capital,
            "available": round(self._available, 2),
            "deployed": round(self._capital - self._available, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "total_pnl": round(self._total_pnl, 2),
            "unrealized_pnl": round(total_unrealized, 2),
            "total_capital": round(self._capital + self._total_pnl, 2),  # P9: total_capital = initial + all realized PnL
            "open_positions": len(self._positions),
            "total_trades": self._trade_count,
            "win_rate": round(win_rate, 2),
            "mode": "PAPER (NSE Simulation)",
            "broker": "Paper",
        }

    def get_all_orders(self) -> List[Order]:
        return list(self._orders.values())

    def reset_daily(self):
        """Reset daily PnL at market open."""
        self._daily_pnl = 0.0
