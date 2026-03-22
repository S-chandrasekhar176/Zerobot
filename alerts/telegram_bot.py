# -*- coding: utf-8 -*-
"""
ZeroBot — Telegram Alert System
Sends prioritized, throttled alerts to your Telegram.
Priority: CRITICAL > HIGH > MEDIUM > INFO
Throttling: prevents spam (min 30s between same-type alerts).

Setup:
1. Create bot: @BotFather on Telegram -> /newbot
2. Get your chat_id: @userinfobot
3. Add to config/.env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

PATCH13 FIX: Replaced python-telegram-bot library's async context manager with
direct stdlib urllib HTTPS POST. The old `async with Bot(token):` pattern calls
__aexit__ which tears down the underlying httpx session after every single send,
leaving self._bot broken for all subsequent calls. All failures were silently
swallowed by except Exception — so alerts stopped working after the very first
(sometimes even before the first) send. The urllib approach has no session
lifecycle issues and requires zero extra dependencies.
"""
import asyncio
import time
import urllib.request
import urllib.parse
import urllib.error
import json
from datetime import datetime
from typing import Dict
from core.config import cfg
from core.logger import log


PRIORITY_EMOJI = {
    "CRITICAL": "🚨",
    "HIGH":     "⚠️",
    "MEDIUM":   "📊",
    "INFO":     "ℹ️",
}

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def _post_telegram_sync(token: str, chat_id: str, text: str) -> bool:
    """
    Synchronous HTTPS POST to Telegram sendMessage endpoint.
    Returns True on success. Uses only stdlib urllib — no extra libraries.
    """
    url = _TG_API.format(token=token)
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            return result.get("ok", False)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        log.error(f"Telegram HTTP {e.code}: {body[:200]}")
        return False
    except Exception as e:
        log.error(f"Telegram POST failed: {e}")
        return False


class TelegramAlerter:
    """Throttled, prioritized Telegram notifications."""

    PRIORITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "INFO": 1}

    def __init__(self):
        self._token    = cfg.telegram.bot_token
        self._chat_id  = cfg.telegram.chat_id
        self._throttle = cfg.telegram.throttle_seconds
        self._last_sent: Dict[str, float] = {}
        self._enabled  = cfg.telegram.enabled and bool(self._token and self._chat_id)

        if self._enabled:
            log.info("Telegram alerter initialized")
        else:
            log.warning("Telegram disabled — add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to config/.env")

    def _is_throttled(self, alert_type: str, priority: str) -> bool:
        """CRITICAL alerts are never throttled. Others respect cooldown."""
        if priority == "CRITICAL":
            return False
        key  = f"{alert_type}_{priority}"
        last = self._last_sent.get(key, 0)
        return (time.time() - last) < self._throttle

    async def send(self, message: str, priority: str = "INFO", alert_type: str = "general"):
        """
        Send alert via Telegram.
        Runs the blocking urllib POST in a thread executor so it does not
        block the asyncio event loop during network I/O.
        """
        if not self._enabled:
            log.debug(f"[Telegram DISABLED] {priority}: {message[:60]}")
            return

        if self._is_throttled(alert_type, priority):
            log.debug(f"Telegram throttled ({self._throttle}s): {alert_type}")
            return

        emoji     = PRIORITY_EMOJI.get(priority, "i")
        timestamp = datetime.now().strftime("%H:%M:%S IST")
        formatted = f"{emoji} *ZeroBot {priority}*\n\n{message}\n\n`{timestamp}`"

        loop = asyncio.get_event_loop()
        ok   = await loop.run_in_executor(
            None,
            _post_telegram_sync,
            self._token,
            self._chat_id,
            formatted,
        )

        if ok:
            self._last_sent[f"{alert_type}_{priority}"] = time.time()
            log.debug(f"Telegram sent [{priority}]: {message[:60]}")

    # ── Convenience Methods ────────────────────────────────────

    async def trade_signal(self, symbol: str, side: str, confidence: float,
                           strategy: str, cmp: float,
                           stop_loss: float = 0, target: float = 0,
                           sentiment: float = 0, news_headline: str = ""):
        """Signal alert — only fires for HIGH confidence (>=70%) to avoid noise."""
        if confidence < 70:
            return
        arrow = "🟢 BUY" if side == "BUY" else "🔴 SHORT"
        sl_txt = f"₹{stop_loss:.2f}" if stop_loss > 0 else "⚠️ Not set"
        tgt_txt = f"₹{target:.2f}" if target > 0 else "⚠️ Not set"
        rr_txt = "—"
        if stop_loss > 0 and target > 0 and cmp > 0:
            risk = abs(cmp - stop_loss)
            reward = abs(target - cmp)
            rr_txt = f"{reward / risk:.1f}:1" if risk > 0 else "—"
        potential_profit = abs(target - cmp) if target > 0 and cmp > 0 else 0
        sent_icon = "📈 Bullish" if sentiment > 0.2 else ("📉 Bearish" if sentiment < -0.2 else "➡️ Neutral")
        news_line = f"\n📰 _{news_headline[:80]}_" if news_headline else ""
        mode_badge = "🔴 LIVE" if cfg.is_live else ("🔀 HYBRID" if cfg.is_hybrid else "📄 PAPER")
        await self.send(
            f"{arrow} Signal — *{symbol}*\n\n"
            f"📊 Strategy: `{strategy}`\n"
            f"💡 ML Conf: `{confidence:.1f}%`\n"
            f"💰 Entry CMP: `₹{cmp:.2f}`\n"
            f"🛑 Stop Loss: `{sl_txt}`\n"
            f"🎯 Target: `{tgt_txt}`\n"
            f"⚖️ Risk:Reward: `{rr_txt}`\n"
            f"💵 Potential Gain: `₹{potential_profit:.0f}`\n"
            f"📡 Sentiment: {sent_icon}{news_line}\n"
            f"Mode: {mode_badge}",
            priority="HIGH",
            alert_type="signal",
        )

    async def trade_filled(self, symbol: str, side: str, qty: int,
                           fill_price: float, costs: float,
                           stop_loss: float = 0, target: float = 0,
                           confidence: float = 0):
        """Fill alert — trade executed, position now open."""
        arrow = "✅ BOUGHT" if side == "BUY" else "✅ SHORTED"
        sl_txt = f"₹{stop_loss:.2f}" if stop_loss > 0 else "⚠️ Not set"
        tgt_txt = f"₹{target:.2f}" if target > 0 else "⚠️ Not set"
        position_value = qty * fill_price
        max_risk = abs(fill_price - stop_loss) * qty if stop_loss > 0 else 0
        max_gain = abs(target - fill_price) * qty if target > 0 else 0
        rr_txt = f"{max_gain / max_risk:.1f}:1" if max_risk > 0 else "—"
        mode_badge = "🔴 LIVE ORDER" if cfg.is_live else ("🔀 HYBRID (paper fill)" if cfg.is_hybrid else "📄 PAPER")
        await self.send(
            f"{arrow} *{qty}× {symbol}*\n\n"
            f"💰 Fill Price: `₹{fill_price:.2f}`\n"
            f"📦 Qty: `{qty}` | Position Value: `₹{position_value:,.0f}`\n"
            f"🛑 Stop Loss: `{sl_txt}`\n"
            f"🎯 Target: `{tgt_txt}`\n"
            f"⚖️ R:R: `{rr_txt}` | Max Risk: `₹{max_risk:.0f}` | Max Gain: `₹{max_gain:.0f}`\n"
            f"💡 Confidence: `{confidence:.1f}%`\n"
            f"💸 Brokerage: `₹{costs:.2f}`\n"
            f"Mode: {mode_badge}",
            priority="HIGH",
            alert_type="fill",
        )

    async def trade_closed(self, symbol: str, pnl: float, net_pnl: float,
                           strategy: str, entry_price: float = 0,
                           exit_price: float = 0, qty: int = 0,
                           brokerage: float = 0):
        """Rich close alert — P&L breakdown, price move, ROI."""
        is_win = net_pnl >= 0
        icon = "✅ PROFIT" if is_win else "❌ LOSS"
        arrow = "📈" if is_win else "📉"
        price_move = exit_price - entry_price if entry_price > 0 and exit_price > 0 else 0
        pct_move = (price_move / entry_price * 100) if entry_price > 0 else 0
        position_value = qty * entry_price if qty > 0 and entry_price > 0 else 0
        roi_pct = (net_pnl / position_value * 100) if position_value > 0 else 0
        mode_badge = "🔴 LIVE" if cfg.is_live else ("🔀 HYBRID" if cfg.is_hybrid else "📄 PAPER")
        await self.send(
            f"{arrow} {icon} — *{symbol}* Closed\n\n"
            f"📊 Strategy: `{strategy}`\n"
            f"💰 Entry: `₹{entry_price:.2f}` → Exit: `₹{exit_price:.2f}`\n"
            f"📦 Qty: `{qty}` | Position: `₹{position_value:,.0f}`\n"
            f"📈 Price Move: `{price_move:+.2f}` ({pct_move:+.2f}%)\n"
            f"━━━━━━━━━━━━━━\n"
            f"💵 Gross P&L: `₹{pnl:+.2f}`\n"
            f"💸 Brokerage: `₹{brokerage:.2f}`\n"
            f"🏆 *Net P&L: ₹{net_pnl:+.2f}* ({roi_pct:+.2f}% ROI)\n"
            f"Mode: {mode_badge}",
            priority="HIGH",
            alert_type="close",
        )

    async def news_alert_high(self, headline: str, score: float, symbol: str = ""):
        """Alert only for major news (|score| >= 0.5)."""
        if abs(score) < 0.5:
            return
        direction = "🔴 BEARISH" if score < 0 else "🟢 BULLISH"
        strength = "STRONG" if abs(score) >= 0.8 else "MODERATE"
        sym_tag = f" `[{symbol}]`" if symbol else ""
        await self.send(
            f"📰 *MAJOR NEWS*{sym_tag}\n\n"
            f"{direction} — {strength} (score: `{score:+.2f}`)\n\n"
            f"_{headline[:200]}_",
            priority="CRITICAL" if abs(score) >= 0.8 else "HIGH",
            alert_type="news_major",
        )

    async def risk_alert(self, reason: str):
        await self.send(
            f"⚠️ *RISK ALERT*\n\n{reason}",
            priority="CRITICAL",
            alert_type="risk",
        )

    async def system_halted(self, reason: str):
        await self.send(
            f"⏸ *BOT HALTED*\n\n"
            f"Reason: {reason}\n\n"
            f"⚠️ All trading paused. Open positions will be squared off.\n"
            f"Review dashboard and resume manually when ready.",
            priority="CRITICAL",
            alert_type="halt",
        )

    async def daily_report(self, summary: dict):
        """Rich daily summary — full session stats."""
        pnl    = summary.get("daily_pnl", 0)
        wr     = summary.get("win_rate", 0)
        trades = summary.get("daily_trades", 0)
        wins   = summary.get("daily_wins", 0)
        losses = summary.get("daily_losses", 0)
        cap    = summary.get("capital", 0)
        signals= summary.get("signals_today", 0)
        pnl_icon = "📈" if pnl >= 0 else "📉"
        mode_badge = "🔴 LIVE" if cfg.is_live else ("🔀 HYBRID" if cfg.is_hybrid else "📄 PAPER")
        await self.send(
            f"📊 *Daily Report — ZeroBot*\n\n"
            f"{pnl_icon} Net P&L: *₹{pnl:+,.2f}*\n"
            f"🏆 Win Rate: `{wr:.1f}%` ({wins}W / {losses}L)\n"
            f"📈 Trades: `{trades}` | Signals: `{signals}`\n"
            f"💼 Capital: `₹{cap:,.0f}`\n"
            f"Mode: {mode_badge}\n\n"
            f"_See dashboard for full breakdown_",
            priority="INFO",
            alert_type="daily_report",
        )

    async def ml_retrained(self, n_trades: int, full: bool = False):
        """Notify when ML models retrain."""
        label = "Full Monthly Retrain" if full else f"Incremental Retrain ({n_trades} trades)"
        await self.send(
            f"🤖 *ML Models Retrained*\n\n"
            f"Type: `{label}`\n"
            f"Models: XGBoost (55%) + LightGBM (45%)\n"
            f"_Confidence gate now uses updated weights_",
            priority="INFO",
            alert_type="ml_retrain",
        )

    async def heartbeat(self):
        """Silent heartbeat — only logs, no Telegram send (prevents spam)."""
        log.debug("Telegram heartbeat OK")


# Global singleton
alerter = TelegramAlerter()


# ─── P16: Inbound command polling + startup notification ─────────────────────

class TelegramCommandHandler:
    """
    P16: Inbound Telegram command handler.

    Polls for updates from Telegram and dispatches commands to the engine.
    Uses long-polling (timeout=60s) via raw urllib — no extra libraries.

    COMMANDS:
      /status    — capital, open positions, today's P&L
      /halt      — emergency stop all trading
      /resume    — restart trading after halt
      /positions — table of open positions
      /pnl       — today's P&L breakdown
      /help      — list all commands
    """

    _COMMANDS = ["/status", "/halt", "/resume", "/positions", "/pnl", "/help"]
    _POLL_URL = "https://api.telegram.org/bot{token}/getUpdates"

    def __init__(self, alerter_instance, engine_ref=None):
        self._alerter = alerter_instance
        self._engine  = engine_ref
        self._token   = cfg.telegram.bot_token
        self._chat_id = cfg.telegram.chat_id
        self._offset  = 0
        self._running = False
        self._rate_limit_until = 0.0
        self._failed_log = None
        self._enabled = bool(self._token and self._chat_id)

    def set_engine(self, engine):
        """Wire in the engine after startup (avoids circular import)."""
        self._engine = engine

    async def start_polling(self):
        """Start long-polling for inbound commands. Runs as asyncio task."""
        if not self._enabled:
            log.debug("[TelegramCmd] Disabled — no token/chat_id")
            return
        self._running = True
        log.info("[TelegramCmd] Inbound command polling started")
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug(f"[TelegramCmd] Poll error: {e}")
                await asyncio.sleep(10)

    async def _poll_once(self):
        """Single long-poll request to Telegram getUpdates."""
        import urllib.request, urllib.parse
        url = self._POLL_URL.format(token=self._token)
        params = urllib.parse.urlencode({
            "offset": self._offset,
            "timeout": 30,
            "allowed_updates": '["message"]',
        })
        full_url = f"{url}?{params}"

        loop = asyncio.get_event_loop()

        def _fetch():
            try:
                with urllib.request.urlopen(full_url, timeout=35) as r:
                    return r.read().decode()
            except Exception as e:
                return None

        raw = await loop.run_in_executor(None, _fetch)
        if not raw:
            return

        import json
        data = json.loads(raw)
        updates = data.get("result", [])
        for upd in updates:
            self._offset = upd["update_id"] + 1
            msg = upd.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if chat_id == str(self._chat_id) and text.startswith("/"):
                cmd = text.split()[0].lower()
                await self._dispatch(cmd)

    async def _dispatch(self, cmd: str):
        """Route a command to the appropriate handler."""
        handlers = {
            "/status":    self._cmd_status,
            "/halt":      self._cmd_halt,
            "/resume":    self._cmd_resume,
            "/positions": self._cmd_positions,
            "/pnl":       self._cmd_pnl,
            "/help":      self._cmd_help,
        }
        handler = handlers.get(cmd)
        if handler:
            log.info(f"[TelegramCmd] Executing: {cmd}")
            await handler()
        else:
            await self._alerter.send(
                f"❓ Unknown command: `{cmd}`\nSend /help for available commands.",
                priority="INFO", alert_type="cmd"
            )

    async def _cmd_status(self):
        eng = self._engine
        if eng is None:
            await self._alerter.send("⚠️ Engine not available", priority="INFO", alert_type="cmd")
            return
        summary = eng.state.get_summary()
        pos_count = len(eng.state.state.open_positions)
        mode_icon = "🔴" if cfg.is_live else "🔀" if cfg.is_hybrid else "📄"
        halt_txt = "⏸ HALTED" if eng.state.state.is_halted else "✅ RUNNING"
        await self._alerter.send(
            f"📊 *ZeroBot Status*\n\n"
            f"{halt_txt} | {mode_icon} {cfg.mode.upper()}\n"
            f"💼 Capital: `₹{summary.get('capital',0):,.0f}`\n"
            f"📈 Day P&L: `₹{summary.get('daily_pnl',0):+,.2f}`\n"
            f"🔢 Open Positions: `{pos_count}`\n"
            f"📊 Day Trades: `{summary.get('daily_trades',0)}`\n"
            f"🏆 Win Rate: `{summary.get('win_rate',0):.1f}%`\n"
            f"📡 Feed: `{type(eng.rt_feed).__name__}`",
            priority="INFO", alert_type="cmd"
        )

    async def _cmd_halt(self):
        if self._engine:
            self._engine.halt("Telegram /halt command")
            await self._alerter.send(
                "⏸ *Bot HALTED via Telegram*\n\nAll trading paused.\nSend /resume to restart.",
                priority="CRITICAL", alert_type="halt"
            )
        else:
            await self._alerter.send("⚠️ Engine ref not set", priority="INFO", alert_type="cmd")

    async def _cmd_resume(self):
        if self._engine:
            self._engine.resume()
            await self._alerter.send(
                "▶️ *Bot RESUMED via Telegram*\n\nTrading restarted.",
                priority="HIGH", alert_type="cmd"
            )
        else:
            await self._alerter.send("⚠️ Engine ref not set", priority="INFO", alert_type="cmd")

    async def _cmd_positions(self):
        eng = self._engine
        if eng is None:
            await self._alerter.send("⚠️ Engine not available", priority="INFO", alert_type="cmd")
            return
        positions = eng.state.state.open_positions
        if not positions:
            await self._alerter.send("📭 No open positions", priority="INFO", alert_type="cmd")
            return
        lines = ["📋 *Open Positions*\n"]
        for sym, pos in positions.items():
            side_icon = "🟢" if pos.get("side") == "LONG" else "🔴"
            lines.append(
                f"{side_icon} *{sym}*\n"
                f"  Side: `{pos.get('side','?')}` | Qty: `{pos.get('qty',0)}`\n"
                f"  Entry: `₹{pos.get('avg_price',0):.2f}` → CMP: `₹{pos.get('current_price',0):.2f}`\n"
                f"  SL: `₹{pos.get('stop_loss',0) or 0:.2f}` | Target: `₹{pos.get('target',0) or 0:.2f}`\n"
                f"  P&L: `₹{pos.get('unrealized_pnl',0):+.2f}`"
            )
        await self._alerter.send("\n".join(lines), priority="INFO", alert_type="cmd")

    async def _cmd_pnl(self):
        eng = self._engine
        if eng is None:
            await self._alerter.send("⚠️ Engine not available", priority="INFO", alert_type="cmd")
            return
        summary = eng.state.get_summary()
        unrealized = sum(
            p.get("unrealized_pnl", 0)
            for p in eng.state.state.open_positions.values()
        )
        await self._alerter.send(
            f"💰 *Today's P&L*\n\n"
            f"Realized:   `₹{summary.get('daily_pnl',0):+,.2f}`\n"
            f"Unrealized: `₹{unrealized:+,.2f}`\n"
            f"Total:      `₹{summary.get('daily_pnl',0)+unrealized:+,.2f}`\n\n"
            f"Trades: `{summary.get('daily_trades',0)}` | "
            f"W/L: `{summary.get('daily_wins',0)}/{summary.get('daily_losses',0)}`",
            priority="INFO", alert_type="cmd"
        )

    async def _cmd_help(self):
        await self._alerter.send(
            "🤖 *ZeroBot Commands*\n\n"
            "/status    — Capital, positions, day P&L\n"
            "/positions — All open positions\n"
            "/pnl       — Today's P&L breakdown\n"
            "/halt      — ⏸ Emergency stop trading\n"
            "/resume    — ▶️ Restart trading\n"
            "/help      — This message",
            priority="INFO", alert_type="cmd"
        )


# Wire into the existing TelegramAlerter
_original_init = TelegramAlerter.__init__

def _patched_init(self):
    _original_init(self)
    # Attach command handler (engine wired later via set_engine)
    self.cmd_handler = TelegramCommandHandler(self, engine_ref=None)

TelegramAlerter.__init__ = _patched_init


async def _startup_notification_method(self, capital: float, mode: str,
                                        strategies_count: int, symbols_count: int):
    """P16: Send a startup message when ZeroBot goes live each morning."""
    mode_icon = "🔴" if mode == "live" else ("🔀" if mode == "hybrid" else "📄")
    await self.send(
        f"🚀 *ZeroBot Started*\n\n"
        f"Mode: {mode_icon} `{mode.upper()}`\n"
        f"Capital: `₹{capital:,.0f}`\n"
        f"Strategies: `{strategies_count}` active\n"
        f"Symbols: `{symbols_count}` in watchlist\n\n"
        f"_Send /help for available commands_",
        priority="INFO", alert_type="startup"
    )

TelegramAlerter.startup_notification = _startup_notification_method


async def _rate_limit_send_method(self, message: str, priority: str = "INFO",
                                   alert_type: str = "general"):
    """
    P16: Rate-limit aware send — backs off on HTTP 429, logs failures.
    Wraps the original send() with retry+backoff logic.
    """
    import time as _t

    MAX_ATTEMPTS = 4
    for attempt in range(MAX_ATTEMPTS):
        if not self._enabled:
            log.debug(f"[Telegram DISABLED] {priority}: {message[:60]}")
            return
        if self._is_throttled(alert_type, priority):
            log.debug(f"Telegram throttled ({self._throttle}s): {alert_type}")
            return

        emoji     = PRIORITY_EMOJI.get(priority, "ℹ️")
        timestamp = datetime.now().strftime("%H:%M:%S IST")
        formatted = f"{emoji} *ZeroBot {priority}*\n\n{message}\n\n`{timestamp}`"

        url     = _TG_API.format(token=self._token)
        import json, urllib.request, urllib.error
        payload = json.dumps({
            "chat_id": self._chat_id,
            "text": formatted,
            "parse_mode": "Markdown",
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )

        loop = asyncio.get_event_loop()

        def _do_post():
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.read().decode(), None
            except urllib.error.HTTPError as e:
                return None, e
            except Exception as e:
                return None, e

        raw, err = await loop.run_in_executor(None, _do_post)

        if raw:
            result = json.loads(raw)
            if result.get("ok"):
                self._last_sent[f"{alert_type}_{priority}"] = _t.time()
                return
        elif err:
            import urllib.error
            if isinstance(err, urllib.error.HTTPError) and err.code == 429:
                delay = min(2 ** attempt * 2, 120)
                log.warning(f"[Telegram] Rate limited (429) — backing off {delay}s (attempt {attempt+1})")
                await asyncio.sleep(delay)
                continue
            else:
                # Log to file on permanent failure
                try:
                    import os
                    failed_log = os.path.join(
                        str(__import__('pathlib').Path(__file__).parent.parent),
                        "data", "telegram_failed.log"
                    )
                    os.makedirs(os.path.dirname(failed_log), exist_ok=True)
                    with open(failed_log, "a") as fh:
                        fh.write(
                            f"{datetime.now().isoformat()} | {priority} | "
                            f"{alert_type} | {message[:100]}\n"
                        )
                except Exception:
                    pass
                log.error(f"Telegram POST failed ({err}): {message[:60]}")
                return

# Replace send() method with rate-limit aware version
TelegramAlerter.send = _rate_limit_send_method


# Re-expose the singleton (patched __init__ already ran on the instance, re-init)
# The alerter singleton was created before the patch, so re-attach cmd_handler
try:
    alerter.cmd_handler = TelegramCommandHandler(alerter, engine_ref=None)
except Exception:
    pass
