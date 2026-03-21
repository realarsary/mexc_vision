"""
main.py
Main entry point for MEXC Signal Bot
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import config.settings as cfg
from config.settings import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    CHECK_INTERVAL,
    SIGNAL_COOLDOWN,
    PRICE_CHANGE_THRESHOLD,
    RSI_PERIOD,
    SEND_CHART,
    TRADING_PAIRS_WHITELIST,
    TRADING_PAIRS_BLACKLIST,
    LOG_LEVEL,
    LOG_FILE,
    LOG_MAX_BYTES,
    LOG_BACKUP_COUNT,
)
from database import Database
from services.mexc import MexcClient, MexcWSClient
from services.analysis import SignalAnalyzer, SignalResult
from bot.services import TelegramService
from bot.handlers import setup as setup_handlers
from bot.utils import ChartGenerator


# ============================================================
# LOGGING
# ============================================================
def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    from logging.handlers import RotatingFileHandler

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Silence noisy libraries
    for noisy in ("aiohttp", "aiogram", "websockets", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ============================================================
# KLINES CACHE  (TTL = 30 sec, reduces REST requests)
# ============================================================
class _KlinesCache:
    def __init__(self, ttl: float = 30.0):
        self._ttl = ttl
        self._store: Dict[str, tuple] = {}  # key → (timestamp, data)

    def get(self, key: str) -> Optional[List[float]]:
        import time
        entry = self._store.get(key)
        if entry and (time.monotonic() - entry[0]) < self._ttl:
            return entry[1]
        return None

    def set(self, key: str, data: List[float]) -> None:
        import time
        self._store[key] = (time.monotonic(), data)

    def clear(self) -> None:
        self._store.clear()


# ============================================================
# MONITOR
# ============================================================
class Monitor:
    """
    Main monitor — combines WS, REST, analysis, and Telegram sending.

    Workflow:
    1. WS receives real-time prices
    2. Every CHECK_INTERVAL seconds — pre-filter symbols by price change
    3. For pre-filtered symbols — fetch klines via REST
    4. Run full analysis (RSI 1h + RSI 15m)
    5. If signal → save to DB → send to Telegram
    """

    def __init__(
        self,
        db: Database,
        api: MexcClient,
        tg: TelegramService,
        chart: ChartGenerator,
    ):
        self._db = db
        self._api = api
        self._tg = tg
        self._chart = chart
        self._cache = _KlinesCache(ttl=30.0)

        self._symbols: List[str] = []
        self._ws: Optional[MexcWSClient] = None
        self._running = False

        self._stats = {
            "cycles": 0,
            "checked": 0,
            "signals": 0,
            "errors": 0,
        }

    async def start(self, symbols: List[str]) -> None:
        self._symbols = symbols
        self._running = True

        self._ws = MexcWSClient(
            symbols=symbols,
            on_price_update=self._on_price_update,
        )
        await self._ws.start()

        await self._loop()

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.stop()

    def _on_price_update(self, symbol: str, price: float) -> None:
        """WS callback — update price cache (non-blocking)"""
        pass  # prices stored internally in MexcWSClient._prices

    async def _loop(self) -> None:
        """Main checking loop"""
        import time

        logger.info(f"🚀 Monitor started | {len(self._symbols)} symbols | interval {CHECK_INTERVAL}s")

        while self._running:
            cycle_start = time.monotonic()
            self._stats["cycles"] += 1

            try:
                await self._run_cycle()
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"❌ Cycle error: {e}", exc_info=True)

            if self._stats["cycles"] % 10 == 0:
                self._log_stats()

            elapsed = time.monotonic() - cycle_start
            sleep_time = max(0.0, CHECK_INTERVAL - elapsed)
            await asyncio.sleep(sleep_time)

    async def _run_cycle(self) -> None:
        """One cycle of checking all symbols"""
        import asyncio

        prices = self._ws.get_all_prices() if self._ws else {}

        if not prices:
            logger.warning("⚠️ WS returned no prices — skipping cycle")
            return

        candidates = []
        for symbol in self._symbols:
            if symbol not in prices:
                continue
            on_cd = await self._db.is_on_cooldown(symbol, SIGNAL_COOLDOWN)
            if not on_cd:
                candidates.append(symbol)

        if not candidates:
            return

        logger.debug(f"🔍 Checking {len(candidates)} symbols")

        sem = asyncio.Semaphore(20)

        async def check(symbol: str) -> None:
            async with sem:
                await self._check_symbol(symbol, prices[symbol])

        await asyncio.gather(*[check(s) for s in candidates], return_exceptions=True)
        self._stats["checked"] += len(candidates)

    async def _check_symbol(self, symbol: str, current_price: float) -> None:
        """Check one symbol"""
        try:
            prices_1m = await self._get_klines(symbol, "1m", 20)
            if not prices_1m or len(prices_1m) < 15:
                return

            prices_1h = await self._get_klines(symbol, "1h", 100)
            prices_15m = await self._get_klines(symbol, "15m", 60)

            if not prices_1h or not prices_15m:
                return

            result: SignalResult = SignalAnalyzer.analyze(
                symbol=symbol,
                prices_1m=prices_1m,
                prices_15m=prices_15m,
                prices_1h=prices_1h,
                current_price=current_price,
            )

            if result.triggered:
                await self._handle_signal(result, prices_1h, prices_15m)

        except Exception as e:
            logger.error(f"Error checking {symbol}: {e}")

    async def _get_klines(
        self, symbol: str, interval: str, limit: int
    ) -> Optional[List[float]]:
        """Fetch klines — first cache, then REST"""
        key = f"{symbol}:{interval}"
        cached = self._cache.get(key)
        if cached:
            return cached

        data = await self._api.get_klines(symbol, interval, limit)
        if data:
            self._cache.set(key, data)
        return data

    async def _handle_signal(
        self,
        result: SignalResult,
        prices_1h: List[float],
        prices_15m: List[float],
    ) -> None:
        """Handle signal — save to DB and send to Telegram"""
        symbol = result.symbol

        await self._db.save_signal(
            symbol=symbol,
            price=result.current_price,
            price_change=result.filter_price.value,
            rsi_1h=result.filter_rsi_1h.value,
            rsi_15m=result.filter_rsi_15m.value,
            direction=result.direction,
        )

        await self._db.set_cooldown(symbol)
        self._stats["signals"] += 1

        logger.info(
            f"🚨 SIGNAL {result.direction} {symbol} | "
            f"Price: {result.current_price:.4f} ({result.filter_price.value:+.2f}%) | "
            f"RSI 1h: {result.filter_rsi_1h.value:.1f} | "
            f"RSI 15m: {result.filter_rsi_15m.value:.1f}"
        )

        text = _build_signal_message(result)

        chart_path = None
        if SEND_CHART:
            chart_path = self._chart.generate(
                symbol=symbol,
                prices_1h=prices_1h,
                prices_15m=prices_15m,
                current_price=result.current_price,
                direction=result.direction,
                price_change=result.filter_price.value,
                rsi_1h=result.filter_rsi_1h.value,
                rsi_15m=result.filter_rsi_15m.value,
            )

        await self._tg.send_signal(text=text, photo_path=chart_path)

    def _log_stats(self) -> None:
        ws_metrics = self._ws.get_metrics() if self._ws else {}
        api_metrics = self._api.get_metrics()
        tg_metrics = self._tg.get_metrics()

        logger.info(
            f"📊 Stats | "
            f"Cycles: {self._stats['cycles']} | "
            f"Checked: {self._stats['checked']} | "
            f"Signals: {self._stats['signals']} | "
            f"Errors: {self._stats['errors']}"
        )
        logger.info(f"   API: {api_metrics}")
        logger.info(f"   WS:  {ws_metrics}")
        logger.info(f"   TG:  {tg_metrics}")


# ============================================================
# SIGNAL MESSAGE BUILDER
# ============================================================
def _build_signal_message(result: SignalResult) -> str:
    direction_emoji = "🟢" if result.direction == "LONG" else "🔴"
    direction_text = "LONG (buy)" if result.direction == "LONG" else "SHORT (sell)"

    return (
        f"{direction_emoji} <b>{result.symbol}</b> — {direction_text}\n\n"
        f"💰 Price:    <code>{result.current_price:.6f} USDT</code>\n"
        f"📈 Change: <code>{result.filter_price.value:+.2f}%</code> over 15m\n\n"
        f"📊 RSI (1h):  <code>{result.filter_rsi_1h.value:.1f}</code>\n"
        f"📊 RSI (15m): <code>{result.filter_rsi_15m.value:.1f}</code>\n\n"
        f"<i>{result.filter_rsi_1h.description}</i>\n"
        f"<i>{result.filter_rsi_15m.description}</i>"
    )


# ============================================================
# SYMBOLS LOADER
# ============================================================
async def _load_symbols(api: MexcClient) -> List[str]:
    """Load symbols list: from file or API"""
    symbols_file = Path("data/symbols_usdt.txt")

    if symbols_file.exists():
        symbols = [
            line.strip()
            for line in symbols_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        logger.info(f"📂 Loaded {len(symbols)} symbols from file")
    else:
        logger.info("📡 Fetching symbols from MEXC API...")
        symbols = await api.get_all_symbols()
        if symbols:
            symbols_file.parent.mkdir(exist_ok=True)
            symbols_file.write_text("\n".join(symbols))
            logger.info(f"✅ Got {len(symbols)} symbols, saved to {symbols_file}")

    if TRADING_PAIRS_WHITELIST:
        symbols = [s for s in symbols if s in TRADING_PAIRS_WHITELIST]
        logger.info(f"🔍 Whitelist: {len(symbols)} symbols remain")

    if TRADING_PAIRS_BLACKLIST:
        symbols = [s for s in symbols if s not in TRADING_PAIRS_BLACKLIST]
        logger.info(f"🚫 Blacklist: {len(symbols)} symbols remain")

    return symbols


# ============================================================
# AIOGRAM BOT POLLER
# ============================================================
async def _run_bot_polling(db: Database) -> None:
    """Start aiogram polling in background"""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    router = setup_handlers(db)
    dp.include_router(router)

    logger.info("🤖 Telegram bot polling started")
    await dp.start_polling(bot, handle_signals=False)


# ============================================================
# MAIN
# ============================================================
async def main() -> None:
    setup_logging()

    logger.info("=" * 60)
    logger.info("🚀 Starting MEXC Signal Bot...")
    logger.info("=" * 60)

    try:
        cfg.validate()
        cfg.print_summary()
    except cfg.ConfigError as e:
        logger.error(f"❌ {e}")
        sys.exit(1)

    db = Database()
    api = MexcClient()
    tg = TelegramService()
    chart = ChartGenerator()
    monitor = Monitor(db=db, api=api, tg=tg, chart=chart)

    await db.connect()
    await api.start()
    await tg.start()

    symbols = await _load_symbols(api)
    if not symbols:
        logger.error("❌ Symbol list empty — exiting")
        await _shutdown(db, api, tg, monitor)
        sys.exit(1)

    await tg.send_message(
        f"🚀 <b>MEXC Signal Bot started</b>\n\n"
        f"📊 Monitoring <b>{len(symbols)}</b> symbols\n"
        f"⏱ Interval: {CHECK_INTERVAL}s\n"
        f"📈 Price threshold: ±{PRICE_CHANGE_THRESHOLD}%\n"
        f"🔄 Cooldown: {SIGNAL_COOLDOWN}s"
    )

    loop = asyncio.get_running_loop()

    def _handle_signal():
        logger.info("🛑 Stop signal received")
        asyncio.create_task(_shutdown(db, api, tg, monitor))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows does not support add_signal_handler

    await asyncio.gather(
        _run_bot_polling(db),
        monitor.start(symbols),
        return_exceptions=True,
    )


async def _shutdown(
    db: Database,
    api: MexcClient,
    tg: TelegramService,
    monitor: Monitor,
) -> None:
    """Graceful shutdown of all components"""
    logger.info("🛑 Shutting down...")
    monitor._running = False
    await monitor.stop()
    await tg.send_message("🛑 <b>MEXC Signal Bot stopped</b>")
    await tg.stop()
    await api.stop()
    await db.close()
    logger.info("✅ Bot stopped")


# ============================================================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Stopped by user")
