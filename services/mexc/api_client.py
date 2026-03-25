"""
services/mexc/api_client.py
MEXC REST API client — clean, without duplicates, with rate limiting
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

from config.settings import (
    MEXC_BASE_URL,
    MEXC_API_TIMEOUT,
    MEXC_MAX_CONNECTIONS,
    REQUESTS_PER_SECOND,
)

logger = logging.getLogger(__name__)


# ============================================================
# INTERVAL MAPPING
# ============================================================
_INTERVAL_MAP = {
    "1m":  "Min1",
    "5m":  "Min5",
    "15m": "Min15",
    "30m": "Min30",
    "1h":  "Min60",
    "4h":  "Hour4",
    "1d":  "Day1",
    "1w":  "Week1",
    "1M":  "Month1",
}


def _to_mexc_interval(interval: str) -> str:
    return _INTERVAL_MAP.get(interval, interval)


# ============================================================
# EXCEPTIONS
# ============================================================
class APIError(Exception):
    """Base API error"""
    pass


class RateLimitError(APIError):
    """Request rate limit exceeded"""
    pass


# ============================================================
# METRICS
# ============================================================
class _Metrics:
    def __init__(self):
        self.total = 0
        self.success = 0
        self.failed = 0
        self.retries = 0
        self.rate_limits = 0
        self._total_time = 0.0

    def on_success(self, elapsed: float):
        self.total += 1
        self.success += 1
        self._total_time += elapsed

    def on_failure(self):
        self.total += 1
        self.failed += 1

    def on_retry(self):
        self.retries += 1

    def on_rate_limit(self):
        self.rate_limits += 1

    @property
    def avg_response_ms(self) -> float:
        if self.success == 0:
            return 0.0
        return (self._total_time / self.success) * 1000

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.success / self.total * 100

    def summary(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "retries": self.retries,
            "rate_limits": self.rate_limits,
            "success_rate": f"{self.success_rate:.1f}%",
            "avg_response_ms": f"{self.avg_response_ms:.0f}ms",
        }


# ============================================================
# CLIENT
# ============================================================
class MexcClient:
    """
    Asynchronous REST client for MEXC Futures API.
    Supports: rate limiting, auto-retry, metrics.
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._metrics = _Metrics()
        self._rate_limiter = asyncio.Semaphore(int(REQUESTS_PER_SECOND))
        self._last_request_time = 0.0

    async def start(self) -> None:
        """Create aiohttp session"""
        connector = aiohttp.TCPConnector(limit=MEXC_MAX_CONNECTIONS)
        timeout = aiohttp.ClientTimeout(total=MEXC_API_TIMEOUT)
        self._session = aiohttp.ClientSession(
            base_url=MEXC_BASE_URL,
            connector=connector,
            timeout=timeout,
        )
        logger.info("✅ MexcClient started")

    async def stop(self) -> None:
        """Close session"""
        if self._session:
            await self._session.close()
            logger.info("🔒 MexcClient stopped")

    # ----------------------------------------------------------
    # PRIVATE METHODS
    # ----------------------------------------------------------
    async def _get(
        self,
        path: str,
        params: Optional[Dict] = None,
        retries: int = 3,
    ) -> Any:
        """
        GET request with auto-retry and rate limiting.
        """
        if self._session is None:
            raise APIError("Session not started. Call await client.start()")

        # Simple rate limiter — minimal interval between requests
        async with self._rate_limiter:
            now = time.monotonic()
            wait = (1.0 / REQUESTS_PER_SECOND) - (now - self._last_request_time)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_time = time.monotonic()

        last_error: Exception = APIError("Unknown error")

        for attempt in range(1, retries + 1):
            t0 = time.monotonic()
            try:
                async with self._session.get(path, params=params) as resp:
                    elapsed = time.monotonic() - t0

                    if resp.status == 429:
                        self._metrics.on_rate_limit()
                        wait_time = 2 ** attempt
                        logger.warning(f"⚠️ MEXC rate limit, waiting {wait_time}s")
                        await asyncio.sleep(wait_time)
                        continue

                    if resp.status != 200:
                        text = await resp.text()
                        raise APIError(f"HTTP {resp.status}: {text[:200]}")

                    data = await resp.json()
                    self._metrics.on_success(elapsed)
                    return data

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                self._metrics.on_retry()
                wait_time = 0.5 * attempt
                logger.warning(
                    f"⚠️ Attempt {attempt}/{retries} for {path}: {e}. Waiting {wait_time}s"
                )
                await asyncio.sleep(wait_time)

        self._metrics.on_failure()
        raise APIError(f"Failed to perform request {path} after {retries} attempts: {last_error}")

    # ----------------------------------------------------------
    # PUBLIC API
    # ----------------------------------------------------------
    async def ping(self) -> bool:
        """Check MEXC API availability"""
        try:
            await self._get("/api/v1/contract/ping")
            return True
        except APIError:
            return False

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> List[float]:
        """
        Get a list of candle close prices.

        Args:
            symbol: Trading pair, e.g., "BTC_USDT"
            interval: "1m", "15m", "1h", "4h", etc.
            limit: Number of candles

        Returns:
            List of closing prices [float]
        """
        mexc_interval = _to_mexc_interval(interval)
        params = {
            "interval": mexc_interval,
            "limit": limit,
            "start": "",
            "end": "",
        }

        try:
            data = await self._get(f"/api/v1/contract/kline/{symbol}", params=params)
        except APIError as e:
            logger.error(f"Klines error {symbol} {interval}: {e}")
            return []

        # Parse MEXC response
        try:
            rows = data.get("data", {})
            # MEXC returns: {"data": {"time": [...], "close": [...], ...}}
            closes = rows.get("close", [])
            if not closes:
                # Alternative format: list [time, open, high, low, close, ...]
                candles = rows if isinstance(rows, list) else []
                closes = [float(c[4]) for c in candles if len(c) > 4]

            return [float(p) for p in closes]

        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.error(f"Klines parsing error {symbol}: {e}")
            return []

    async def get_klines_ohlcv(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
    ) -> List[Dict]:
        """
        Get OHLCV candles as list of dicts.

        Args:
            symbol:   Trading pair, e.g. "BTC_USDT"
            interval: "1m", "5m", "15m", "1h", etc.
            limit:    Number of candles (max 2000)

        Returns:
            List of {"open","high","low","close","volume","time"}
        """
        mexc_interval = _to_mexc_interval(interval)
        params = {"interval": mexc_interval, "limit": limit, "start": "", "end": ""}

        try:
            data = await self._get(f"/api/v1/contract/kline/{symbol}", params=params)
        except APIError as e:
            logger.error(f"OHLCV klines error {symbol} {interval}: {e}")
            return []

        try:
            rows = data.get("data", {})
            # MEXC contract kline: {"time":[...],"open":[...],"close":[...],"high":[...],"low":[...],"vol":[...]}
            times  = rows.get("time",  [])
            opens  = rows.get("open",  [])
            closes = rows.get("close", [])
            highs  = rows.get("high",  [])
            lows   = rows.get("low",   [])
            vols   = rows.get("vol",   rows.get("volume", []))

            if times and opens:
                return [
                    {
                        "time":   int(times[i]),
                        "open":   float(opens[i]),
                        "high":   float(highs[i]),
                        "low":    float(lows[i]),
                        "close":  float(closes[i]),
                        "volume": float(vols[i]) if i < len(vols) else 0.0,
                    }
                    for i in range(len(times))
                ]

            # Fallback: list format [time, open, close, high, low, vol]
            candles = rows if isinstance(rows, list) else []
            result = []
            for c in candles:
                if len(c) >= 5:
                    result.append({
                        "time":   int(c[0]),
                        "open":   float(c[1]),
                        "close":  float(c[2]),
                        "high":   float(c[3]),
                        "low":    float(c[4]),
                        "volume": float(c[5]) if len(c) > 5 else 0.0,
                    })
            return result

        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.error(f"OHLCV parsing error {symbol}: {e}")
            return []

    async def get_ticker_price(self, symbol: str) -> Optional[float]:
        """Get current pair price"""
        try:
            data = await self._get(
                "/api/v1/contract/ticker",
                params={"symbol": symbol},
            )
            price = data.get("data", {}).get("lastPrice")
            return float(price) if price else None
        except (APIError, ValueError, TypeError) as e:
            logger.error(f"Error getting price {symbol}: {e}")
            return None

    async def get_all_symbols(self) -> List[str]:
        """Get all USDT futures trading pairs"""
        try:
            data = await self._get("/api/v1/contract/detail")
            contracts = data.get("data", [])
            return [
                c["symbol"]
                for c in contracts
                if isinstance(c, dict)
                and c.get("symbol", "").endswith("_USDT")
                and c.get("state") == 0  # 0 = active MEXC contract
            ]
        except APIError as e:
            logger.error(f"Error fetching symbols: {e}")
            return []

    def get_metrics(self) -> Dict[str, Any]:
        """Get request metrics"""
        return self._metrics.summary()