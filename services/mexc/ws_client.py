"""
services/mexc/ws_client.py
MEXC WebSocket client — real-time price ticks stored in RAM
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

WS_URL = "wss://contract.mexc.com/edge"

RECONNECT_DELAY = 5
RECONNECT_MAX_DELAY = 60
PING_INTERVAL = 20

# Храним тики за последние 20 минут (с запасом)
TICK_WINDOW_SECONDS = 20 * 60


class _WSMetrics:
    def __init__(self):
        self.connections = 0
        self.reconnections = 0
        self.messages = 0
        self.errors = 0
        self.last_message_at: Optional[float] = None

    def on_connect(self, is_reconnect: bool = False):
        self.connections += 1
        if is_reconnect:
            self.reconnections += 1

    def on_message(self):
        self.messages += 1
        self.last_message_at = time.monotonic()

    def on_error(self):
        self.errors += 1

    def summary(self) -> Dict:
        idle = None
        if self.last_message_at:
            idle = f"{time.monotonic() - self.last_message_at:.0f}s"
        return {
            "connections": self.connections,
            "reconnections": self.reconnections,
            "messages": self.messages,
            "errors": self.errors,
            "idle_since": idle,
        }


class MexcWSClient:
    """
    MEXC Futures WebSocket client.

    Subscribes to tickers for all symbols.
    Stores price ticks in RAM as deque with timestamps.
    Automatically reconnects on disconnect.

    Each tick stored as (monotonic_timestamp, price).
    Old ticks (> TICK_WINDOW_SECONDS) are pruned automatically.
    """

    def __init__(
        self,
        symbols: List[str],
        on_price_update: Optional[Callable[[str, float], None]] = None,
    ):
        self.symbols = symbols
        self.on_price_update = on_price_update

        # symbol -> deque of (timestamp, price)
        self._ticks: Dict[str, deque] = {s: deque() for s in symbols}

        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._metrics = _WSMetrics()
        self._task: Optional[asyncio.Task] = None

    # ----------------------------------------------------------
    # PUBLIC
    # ----------------------------------------------------------
    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_forever())
        logger.info(f"✅ MexcWSClient started ({len(self.symbols)} symbols)")

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🔒 MexcWSClient stopped")

    def get_price(self, symbol: str) -> Optional[float]:
        """Get latest price for symbol"""
        ticks = self._ticks.get(symbol)
        if ticks:
            return ticks[-1][1]
        return None

    def get_all_prices(self) -> Dict[str, float]:
        """Get latest price for all symbols that have ticks"""
        result = {}
        for symbol, ticks in self._ticks.items():
            if ticks:
                result[symbol] = ticks[-1][1]
        return result

    def get_price_change_15m(self, symbol: str) -> Optional[float]:
        """
        Returns price change % over last 15 minutes.
        Uses oldest tick within 15-min window vs latest tick.
        Returns None if not enough data (< 15 min of ticks).
        """
        ticks = self._ticks.get(symbol)
        if not ticks or len(ticks) < 2:
            return None

        now = time.monotonic()
        window_start = now - (15 * 60)

        # Find oldest tick within 15-min window
        oldest_in_window = None
        for ts, price in ticks:
            if ts >= window_start:
                oldest_in_window = (ts, price)
                break

        if oldest_in_window is None:
            return None

        # Check we actually have ~15 min of data
        # (oldest tick should be close to 15 min ago, not just arrived)
        age = now - oldest_in_window[0]
        if age < 14 * 60:  # less than 14 minutes of data — skip
            return None

        old_price = oldest_in_window[1]
        new_price = ticks[-1][1]

        if old_price <= 0:
            return None

        return (new_price - old_price) / old_price * 100

    def symbols_ready_count(self) -> int:
        """How many symbols have enough data for filter 1"""
        now = time.monotonic()
        count = 0
        for ticks in self._ticks.values():
            if ticks and (now - ticks[0][0]) >= 14 * 60:
                count += 1
        return count

    def get_metrics(self) -> Dict:
        return self._metrics.summary()

    # ----------------------------------------------------------
    # PRIVATE
    # ----------------------------------------------------------
    async def _run_forever(self) -> None:
        delay = RECONNECT_DELAY
        is_reconnect = False

        while self._running:
            try:
                await self._connect(is_reconnect)
                delay = RECONNECT_DELAY
                is_reconnect = True

            except asyncio.CancelledError:
                break

            except Exception as e:
                self._metrics.on_error()
                logger.error(f"❌ WS error: {e}. Reconnecting in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
                is_reconnect = True

    async def _connect(self, is_reconnect: bool = False) -> None:
        logger.info(f"{'🔄 Reconnecting' if is_reconnect else '🔌 Connecting'} to {WS_URL}")

        async with websockets.connect(
            WS_URL,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            self._metrics.on_connect(is_reconnect)
            logger.info(f"✅ WS connected ({len(self.symbols)} symbols)")

            await self._subscribe(ws)

            # JSON keepalive — MEXC closes connection after ~60s without it
            keepalive_task = asyncio.create_task(self._keepalive(ws))
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    await self._handle_message(raw)
            finally:
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass

    async def _keepalive(self, ws) -> None:
        """Send JSON ping to MEXC every 20 seconds to keep connection alive"""
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send(json.dumps({"method": "ping"}))
                logger.debug("📡 Ping sent")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _subscribe(self, ws) -> None:
        """Subscribe to tickers one by one (MEXC requires individual subscriptions)"""
        for i, symbol in enumerate(self.symbols):
            msg = json.dumps({
                "method": "sub.ticker",
                "param": {"symbol": symbol}
            })
            await ws.send(msg)
            logger.debug(f"📡 Subscribed: {symbol}")
            # Small pause every 50 symbols to avoid flooding
            if i % 50 == 49:
                await asyncio.sleep(0.3)
            else:
                await asyncio.sleep(0.02)
        logger.info(f"✅ Subscribed to {len(self.symbols)} symbols")

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        channel = msg.get("channel", "")
        if not channel.startswith("push.ticker"):
            return

        data = msg.get("data", {})
        symbol = data.get("symbol")
        price_raw = data.get("lastPrice") or data.get("indexPrice")

        if not symbol or price_raw is None:
            return

        try:
            price = float(price_raw)
        except (ValueError, TypeError):
            return

        now = time.monotonic()

        # Store tick
        ticks = self._ticks.get(symbol)
        if ticks is None:
            self._ticks[symbol] = deque()
            ticks = self._ticks[symbol]

        ticks.append((now, price))

        # Prune old ticks (older than TICK_WINDOW_SECONDS)
        cutoff = now - TICK_WINDOW_SECONDS
        while ticks and ticks[0][0] < cutoff:
            ticks.popleft()

        self._metrics.on_message()

        if self.on_price_update:
            try:
                self.on_price_update(symbol, price)
            except Exception as e:
                logger.error(f"Error in on_price_update callback: {e}")