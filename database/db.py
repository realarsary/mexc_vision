"""
database/db.py
SQLite database for storing signals and cooldowns
"""

import aiosqlite
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from config.settings import DATABASE_PATH

logger = logging.getLogger(__name__)


# ============================================================
# SCHEMA
# ============================================================
_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    price       REAL    NOT NULL,
    price_change REAL   NOT NULL,
    rsi_1h      REAL    NOT NULL,
    rsi_15m     REAL    NOT NULL,
    direction   TEXT    NOT NULL,  -- 'LONG' or 'SHORT'
    sent_at     TEXT    NOT NULL   -- ISO 8601
);

CREATE TABLE IF NOT EXISTS cooldowns (
    symbol      TEXT PRIMARY KEY,
    last_sent   TEXT NOT NULL      -- ISO 8601
);
"""


# ============================================================
# DATABASE CLASS
# ============================================================
class Database:
    """Asynchronous wrapper around SQLite"""

    def __init__(self, path: Path = DATABASE_PATH):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Open connection and create tables"""
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        logger.info(f"✅ Database connected: {self.path}")

    async def close(self) -> None:
        """Close connection"""
        if self._conn:
            await self._conn.close()
            logger.info("🔒 Database closed")

    # ----------------------------------------------------------
    # SIGNALS
    # ----------------------------------------------------------
    async def save_signal(
        self,
        symbol: str,
        price: float,
        price_change: float,
        rsi_1h: float,
        rsi_15m: float,
        direction: str,
    ) -> int:
        """Save a signal. Returns the record ID."""
        sent_at = datetime.utcnow().isoformat()
        async with self._conn.execute(
            """
            INSERT INTO signals (symbol, price, price_change, rsi_1h, rsi_15m, direction, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, price, price_change, rsi_1h, rsi_15m, direction, sent_at),
        ) as cursor:
            await self._conn.commit()
            logger.debug(f"💾 Signal saved: {symbol} @ {price}")
            return cursor.lastrowid

    async def get_last_signals(self, limit: int = 10) -> List[aiosqlite.Row]:
        """Get the last N signals"""
        async with self._conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            return await cursor.fetchall()

    async def get_signals_today(self) -> List[aiosqlite.Row]:
        """Get all signals from today (UTC)"""
        today = datetime.utcnow().date().isoformat()
        async with self._conn.execute(
            "SELECT * FROM signals WHERE sent_at LIKE ? ORDER BY id DESC",
            (f"{today}%",),
        ) as cursor:
            return await cursor.fetchall()

    async def get_stats(self) -> dict:
        """Statistics for all time"""
        async with self._conn.execute(
            "SELECT COUNT(*) as total FROM signals"
        ) as cur:
            total = (await cur.fetchone())["total"]

        async with self._conn.execute(
            "SELECT COUNT(*) as today FROM signals WHERE sent_at LIKE ?",
            (f"{datetime.utcnow().date().isoformat()}%",),
        ) as cur:
            today = (await cur.fetchone())["today"]

        async with self._conn.execute(
            "SELECT COUNT(*) as longs FROM signals WHERE direction = 'LONG'"
        ) as cur:
            longs = (await cur.fetchone())["longs"]

        async with self._conn.execute(
            "SELECT COUNT(*) as shorts FROM signals WHERE direction = 'SHORT'"
        ) as cur:
            shorts = (await cur.fetchone())["shorts"]

        return {
            "total": total,
            "today": today,
            "longs": longs,
            "shorts": shorts,
        }

    # ----------------------------------------------------------
    # COOLDOWNS
    # ----------------------------------------------------------
    async def set_cooldown(self, symbol: str) -> None:
        """Set the last sent time for a trading pair"""
        now = datetime.utcnow().isoformat()
        await self._conn.execute(
            "INSERT OR REPLACE INTO cooldowns (symbol, last_sent) VALUES (?, ?)",
            (symbol, now),
        )
        await self._conn.commit()

    async def is_on_cooldown(self, symbol: str, cooldown_seconds: int) -> bool:
        """
        Check if a trading pair is on cooldown.
        Cooldown is stored in the database — survives bot restart.
        """
        async with self._conn.execute(
            "SELECT last_sent FROM cooldowns WHERE symbol = ?", (symbol,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return False

        last_sent = datetime.fromisoformat(row["last_sent"])
        elapsed = (datetime.utcnow() - last_sent).total_seconds()
        return elapsed < cooldown_seconds

    async def clear_expired_cooldowns(self, cooldown_seconds: int) -> int:
        """Delete expired cooldown records. Returns the number deleted."""
        threshold = datetime.utcnow().timestamp() - cooldown_seconds
        # SQLite has no built-in timestamp, calculate via strftime
        async with self._conn.execute(
            """
            DELETE FROM cooldowns
            WHERE (strftime('%s', last_sent)) < ?
            """,
            (str(int(threshold)),),
        ) as cursor:
            await self._conn.commit()
            return cursor.rowcount
