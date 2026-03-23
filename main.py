"""
main.py
MEXC Signal Bot — WS-first architecture

Flow:
1. WS receives real-time price ticks → stored in RAM (deque per symbol)
2. Every CHECK_INTERVAL seconds:
   a. Filter 1: price change over 15 min >= ±PRICE_CHANGE_THRESHOLD%  (pure RAM, no REST)
   b. Only for symbols that passed filter 1:
      Filter 2: RSI 1h overbought/oversold                             (1 REST call)
   c. Only for symbols that passed filter 2:
      Filter 3: RSI 15m overbought/oversold                           (1 REST call)
   d. Signal → save to DB → send to Telegram
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
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
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
from services.analysis.rsi import RSICalculator
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

    for noisy in ("aiohttp", "aiogram", "websockets", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ============================================================
# KLINES CACHE  (TTL = 30 sec)
# ============================================================
class _KlinesCache:
    def __init__(self, ttl: float = 30.0):
        self._ttl = ttl
        self._store: Dict[str, tuple] = {}

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
    WS-first monitor.

    Filter 1 — pure RAM (WS ticks):
        price change over 15 min >= ±PRICE_CHANGE_THRESHOLD%

    Filter 2 — REST only if filter 1 passed:
        RSI 1h overbought (> RSI_OVERBOUGHT) or oversold (< RSI_OVERSOLD)

    Filter 3 — REST only if filter 2 passed:
        RSI 15m overbought or oversold

    Signal → DB → Telegram
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
            "f1_passed": 0,
            "f2_passed": 0,
            "signals": 0,
            "errors": 0,
            "rest_calls": 0,
        }

    async def start(self, symbols: List[str]) -> None:
        self._symbols = symbols
        self._running = True

        self._ws = MexcWSClient(symbols=symbols)
        await self._ws.start()

        await self._loop()

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.stop()

    async def _loop(self) -> None:
        import time

        logger.info(f"🚀 Monitor started | {len(self._symbols)} symbols | interval {CHECK_INTERVAL}s")
        logger.info(f"⏳ Waiting 15 minutes to accumulate ticks before first cycle...")

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
        ready_count = self._ws.symbols_ready_count()

        if ready_count == 0:
            logger.warning("⏳ No symbols with 15min data yet — waiting...")
            return

        logger.debug(f"📊 {ready_count} symbols have 15min data")

        # --- FILTER 1: price change over 15 min (pure RAM, no REST) ---
        f1_candidates = []
        for symbol in self._symbols:
            on_cd = await self._db.is_on_cooldown(symbol, SIGNAL_COOLDOWN)
            if on_cd:
                continue

            change = self._ws.get_price_change_15m(symbol)
            if change is None:
                continue

            if abs(change) >= PRICE_CHANGE_THRESHOLD:
                f1_candidates.append((symbol, change))
                logger.debug(f"✅ F1 passed: {symbol} {change:+.2f}%")

        if not f1_candidates:
            logger.debug("No symbols passed filter 1")
            return

        self._stats["f1_passed"] += len(f1_candidates)
        logger.info(f"🔍 Filter 1 passed: {len(f1_candidates)} symbols | {[s for s,_ in f1_candidates]}")

        # --- FILTER 2 + 3: RSI via REST (only for f1 candidates) ---
        sem = asyncio.Semaphore(5)

        async def check(symbol: str, price_change: float) -> None:
            async with sem:
                await self._check_rsi(symbol, price_change)

        await asyncio.gather(
            *[check(s, c) for s, c in f1_candidates],
            return_exceptions=True,
        )

    async def _check_rsi(self, symbol: str, price_change: float) -> None:
        """Filter 2 (RSI 1h) and Filter 3 (RSI 15m) via REST"""
        try:
            current_price = self._ws.get_price(symbol)
            if not current_price:
                return

            # --- FILTER 2: RSI 1h ---
            prices_1h = await self._get_klines(symbol, "1h", 100)
            if not prices_1h or not RSICalculator.is_valid(prices_1h, RSI_PERIOD):
                return

            self._stats["rest_calls"] += 1
            rsi_1h = RSICalculator.last(prices_1h, RSI_PERIOD)

            if not (rsi_1h > RSI_OVERBOUGHT or rsi_1h < RSI_OVERSOLD):
                logger.debug(f"❌ F2 failed: {symbol} RSI 1h={rsi_1h:.1f}")
                return

            self._stats["f2_passed"] += 1
            logger.debug(f"✅ F2 passed: {symbol} RSI 1h={rsi_1h:.1f}")

            # --- FILTER 3: RSI 15m ---
            prices_15m = await self._get_klines(symbol, "15m", 60)
            if not prices_15m or not RSICalculator.is_valid(prices_15m, RSI_PERIOD):
                return

            self._stats["rest_calls"] += 1
            rsi_15m = RSICalculator.last(prices_15m, RSI_PERIOD)

            if not (rsi_15m > RSI_OVERBOUGHT or rsi_15m < RSI_OVERSOLD):
                logger.debug(f"❌ F3 failed: {symbol} RSI 15m={rsi_15m:.1f}")
                return

            logger.debug(f"✅ F3 passed: {symbol} RSI 15m={rsi_15m:.1f}")

            # --- ALL 3 FILTERS PASSED → SIGNAL ---
            direction = _get_direction(price_change, rsi_1h, rsi_15m)
            await self._handle_signal(
                symbol=symbol,
                current_price=current_price,
                price_change=price_change,
                rsi_1h=rsi_1h,
                rsi_15m=rsi_15m,
                direction=direction,
                prices_1h=prices_1h,
                prices_15m=prices_15m,
            )

        except Exception as e:
            logger.error(f"Error checking RSI for {symbol}: {e}")

    async def _get_klines(
        self, symbol: str, interval: str, limit: int
    ) -> Optional[List[float]]:
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
        symbol: str,
        current_price: float,
        price_change: float,
        rsi_1h: float,
        rsi_15m: float,
        direction: str,
        prices_1h: List[float],
        prices_15m: List[float],
    ) -> None:
        await self._db.save_signal(
            symbol=symbol,
            price=current_price,
            price_change=price_change,
            rsi_1h=rsi_1h,
            rsi_15m=rsi_15m,
            direction=direction,
        )
        await self._db.set_cooldown(symbol)
        self._stats["signals"] += 1

        logger.info(
            f"🚨 SIGNAL {direction} {symbol} | "
            f"Price: {current_price:.4f} ({price_change:+.2f}% / 15m) | "
            f"RSI 1h: {rsi_1h:.1f} | RSI 15m: {rsi_15m:.1f}"
        )

        text = _build_signal_message(
            symbol=symbol,
            current_price=current_price,
            price_change=price_change,
            rsi_1h=rsi_1h,
            rsi_15m=rsi_15m,
            direction=direction,
        )

        chart_path = None
        if SEND_CHART:
            chart_path = self._chart.generate(
                symbol=symbol,
                prices_1h=prices_1h,
                prices_15m=prices_15m,
                current_price=current_price,
                direction=direction,
                price_change=price_change,
                rsi_1h=rsi_1h,
                rsi_15m=rsi_15m,
            )

        await self._tg.send_signal(text=text, photo_path=chart_path)

    def _log_stats(self) -> None:
        ws_metrics = self._ws.get_metrics() if self._ws else {}
        api_metrics = self._api.get_metrics()
        tg_metrics = self._tg.get_metrics()
        ready = self._ws.symbols_ready_count() if self._ws else 0

        logger.info(
            f"📊 Stats | "
            f"Cycles: {self._stats['cycles']} | "
            f"Ready: {ready}/{len(self._symbols)} | "
            f"F1: {self._stats['f1_passed']} | "
            f"F2: {self._stats['f2_passed']} | "
            f"Signals: {self._stats['signals']} | "
            f"REST calls: {self._stats['rest_calls']} | "
            f"Errors: {self._stats['errors']}"
        )
        logger.info(f"   API: {api_metrics}")
        logger.info(f"   WS:  {ws_metrics}")
        logger.info(f"   TG:  {tg_metrics}")


# ============================================================
# HELPERS
# ============================================================
def _get_direction(price_change: float, rsi_1h: float, rsi_15m: float) -> str:
    price_up = price_change > 0
    rsi_oversold = rsi_1h < RSI_OVERSOLD or rsi_15m < RSI_OVERSOLD
    rsi_overbought = rsi_1h > RSI_OVERBOUGHT or rsi_15m > RSI_OVERBOUGHT

    if price_up and rsi_oversold:
        return "LONG"
    if not price_up and rsi_overbought:
        return "SHORT"
    return "LONG" if price_up else "SHORT"


def _build_signal_message(
    symbol: str,
    current_price: float,
    price_change: float,
    rsi_1h: float,
    rsi_15m: float,
    direction: str,
) -> str:
    emoji = "🟢" if direction == "LONG" else "🔴"
    label = "LONG (buy)" if direction == "LONG" else "SHORT (sell)"

    rsi_1h_zone = "overbought" if rsi_1h > RSI_OVERBOUGHT else "oversold"
    rsi_15m_zone = "overbought" if rsi_15m > RSI_OVERBOUGHT else "oversold"

    return (
        f"{emoji} <b>{symbol}</b> — {label}\n\n"
        f"💰 Price:    <code>{current_price:.6f} USDT</code>\n"
        f"📈 Change: <code>{price_change:+.2f}%</code> over 15m\n\n"
        f"📊 RSI (1h):  <code>{rsi_1h:.1f}</code> — {rsi_1h_zone}\n"
        f"📊 RSI (15m): <code>{rsi_15m:.1f}</code> — {rsi_15m_zone}"
    )


# ============================================================
# SYMBOLS LOADER
# ============================================================
async def _load_symbols(api: MexcClient) -> List[str]:
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
        f"⏱ Check interval: {CHECK_INTERVAL}s\n"
        f"📈 Price threshold: ±{PRICE_CHANGE_THRESHOLD}%\n"
        f"🔄 Cooldown: {SIGNAL_COOLDOWN}s\n"
        f"⏳ First signals after ~15 minutes"
    )

    loop = asyncio.get_running_loop()

    def _handle_signal():
        logger.info("🛑 Stop signal received")
        asyncio.create_task(_shutdown(db, api, tg, monitor))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass

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