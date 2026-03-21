"""
bot/handlers/commands.py
Telegram commands: /start /help /status /signals /stats /check
"""

import logging
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config.settings import (
    PRICE_CHANGE_THRESHOLD,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    CHECK_INTERVAL,
    SIGNAL_COOLDOWN,
)
from database import Database

logger = logging.getLogger(__name__)
router = Router(name="commands")

# Bot start time (for /status)
_start_time = datetime.utcnow()


# ============================================================
# DEPENDENCY — inject DB via closure
# ============================================================
def setup(db: Database) -> Router:
    """
    Bind handlers to a database instance.
    Called once on bot startup.
    """

    # ----------------------------------------------------------
    @router.message(Command("start"))
    async def cmd_start(msg: Message):
        await msg.answer(
            "👋 <b>MEXC Signal Bot</b>\n\n"
            "Monitoring MEXC futures in real-time.\n"
            "Sending signals when all filters are met.\n\n"
            "<b>Filters:</b>\n"
            f"1️⃣ Price change ≥ {PRICE_CHANGE_THRESHOLD}% over 15 minutes\n"
            f"2️⃣ RSI (1h) &gt; {RSI_OVERBOUGHT} or &lt; {RSI_OVERSOLD}\n"
            f"3️⃣ RSI (15m) &gt; {RSI_OVERBOUGHT} or &lt; {RSI_OVERSOLD}\n\n"
            "<b>Commands:</b>\n"
            "/status — bot status\n"
            "/signals — last 10 signals\n"
            "/stats — statistics\n"
            "/help — help",
            parse_mode="HTML",
        )

    # ----------------------------------------------------------
    @router.message(Command("help"))
    async def cmd_help(msg: Message):
        await msg.answer(
            "<b>📚 Commands:</b>\n\n"
            "/start — greeting and filters\n"
            "/status — uptime and settings\n"
            "/signals — last 10 signals from DB\n"
            "/stats — all-time statistics\n"
            "/help — this help message",
            parse_mode="HTML",
        )

    # ----------------------------------------------------------
    @router.message(Command("status"))
    async def cmd_status(msg: Message):
        uptime = datetime.utcnow() - _start_time
        hours, rem = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(rem, 60)

        await msg.answer(
            "✅ <b>Status: RUNNING</b>\n\n"
            f"⏱ Uptime: <code>{hours:02d}:{minutes:02d}:{seconds:02d}</code>\n"
            f"🔄 Check interval: {CHECK_INTERVAL}s\n"
            f"⏸ Cooldown: {SIGNAL_COOLDOWN}s\n\n"
            "<b>Filters:</b>\n"
            f"  Price: ±{PRICE_CHANGE_THRESHOLD}% over 15m\n"
            f"  RSI: OB={RSI_OVERBOUGHT} / OS={RSI_OVERSOLD}",
            parse_mode="HTML",
        )

    # ----------------------------------------------------------
    @router.message(Command("signals"))
    async def cmd_signals(msg: Message):
        rows = await db.get_last_signals(limit=10)

        if not rows:
            await msg.answer("📭 No signals yet.", parse_mode="HTML")
            return

        lines = ["<b>📊 Last 10 signals:</b>\n"]
        for row in rows:
            dt = row["sent_at"][:16].replace("T", " ")
            direction = "🟢 LONG" if row["direction"] == "LONG" else "🔴 SHORT"
            lines.append(
                f"{direction} <b>{row['symbol']}</b>\n"
                f"  💰 {row['price']:.4f}  "
                f"  📈 {row['price_change']:+.2f}%  "
                f"  RSI 1h: {row['rsi_1h']:.1f}  "
                f"  RSI 15m: {row['rsi_15m']:.1f}\n"
                f"  🕐 {dt} UTC\n"
            )

        await msg.answer("\n".join(lines), parse_mode="HTML")

    # ----------------------------------------------------------
    @router.message(Command("stats"))
    async def cmd_stats(msg: Message):
        s = await db.get_stats()

        await msg.answer(
            "<b>📈 Signal statistics:</b>\n\n"
            f"📊 Total signals: <b>{s['total']}</b>\n"
            f"📅 Today: <b>{s['today']}</b>\n\n"
            f"🟢 LONG:  <b>{s['longs']}</b>\n"
            f"🔴 SHORT: <b>{s['shorts']}</b>",
            parse_mode="HTML",
        )

    return router
