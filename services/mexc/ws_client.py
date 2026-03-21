"""
services/mexc/ws_client.py
MEXC WebSocket client — real-time prices for all USDT pairs
"""

import asyncio
import json
import logging
import time
from typing import Callable, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# MEXC Futures WebSocket endpoint
WS_URL = "wss://contract.mexc.com/edge"

# Reconnection settings
RECONNECT_DELAY = 5       # seconds before first reconnection
RECONNECT_MAX_DELAY = 60  # maximum delay
PING_INTERVAL = 20        # ping every N seconds


# ============================================================
# METRICS
# ============================================================
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


# ============================================================
# CLIENT
# ============================================================
class MexcWSClient:
    """
    MEXC Futures WebSocket client.

    Subscribes to tickers for all given symbols.
    Automatically reconnects on disconnect.

    Usage:
        ws = MexcWSClient(symbols, on_price_update)
        await ws.start()
        ...
        await ws.stop()
    """

    def __init__(
        self,
        symbols: List[str],
        on_price_update: Callable[[str, float], None],
    ):
        """
        Args:
            symbols: List of pairs, e.g. ["BTC_USDT", "ETH_USDT"]
            on_price_update: Callback(symbol, price) on each price update
        """
        self.symbols = symbols
        self.on_price_update = on_price_update

        self._prices: Dict[str, float] = {}
        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._metrics = _WSMetrics()
        self._task: Optional[asyncio.Task] = None

    # ----------------------------------------------------------
    # PUBLIC
    # ----------------------------------------------------------
    async def start(self) -> None:
        """Start WebSocket in background task"""
        self._running = True
        self._task = asyncio.create_task(self._run_forever())
        logger.info(f"✅ MexcWSClient started ({len(self.symbols)} symbols)")

    async def stop(self) -> None:
        """Stop WebSocket"""
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
        """Get the last cached price for a symbol"""
        return self._prices.get(symbol)

    def get_all_prices(self) -> Dict[str, float]:
        """Get all cached prices"""
        return dict(self._prices)

    def get_metrics(self) -> Dict:
        return self._metrics.summary()

    # ----------------------------------------------------------
    # PRIVATE
    # ----------------------------------------------------------
    async def _run_forever(self) -> None:
        """Loop with automatic reconnection"""
        delay = RECONNECT_DELAY
        is_reconnect = False

        while self._running:
            try:
                await self._connect(is_reconnect)
                delay = RECONNECT_DELAY  # reset delay on success
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
        """Establish connection and listen for messages"""
        logger.info(f"{'🔄 Reconnecting' if is_reconnect else '🔌 Connecting'} to {WS_URL}")

        async with websockets.connect(
            WS_URL,
            ping_interval=PING_INTERVAL,
            ping_timeout=10,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            self._metrics.on_connect(is_reconnect)
            logger.info(f"✅ WS connected ({len(self.symbols)} symbols)")

            # Subscribe in batches of 50 symbols (MEXC limit)
            await self._subscribe(ws)

            # Listen for messages
            async for raw in ws:
                if not self._running:
                    break
                await self._handle_message(raw)

    async def _subscribe(self, ws) -> None:
        """Subscribe to tickers in batches"""
        batch_size = 50
        for i in range(0, len(self.symbols), batch_size):
            batch = self.symbols[i:i + batch_size]
            params = [f"sub.ticker.{symbol}" for symbol in batch]
            msg = json.dumps({"method": "sub.ticker", "param": {"symbol": batch}})
            await ws.send(msg)
            logger.debug(f"📡 Batch subscription {i // batch_size + 1}: {len(batch)} symbols")
            await asyncio.sleep(0.1)  # small pause between batches

    async def _handle_message(self, raw: str) -> None:
        """Handle incoming WS message"""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Filter out system messages
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

        # Update cache and call callback
        self._prices[symbol] = price
        self._metrics.on_message()

        try:
            self.on_price_update(symbol, price)
        except Exception as e:
            logger.error(f"Error in on_price_update callback: {e}")
