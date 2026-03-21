"""
services/analysis/signal_analyzer.py
Trade signal analysis — 3 filters + LONG/SHORT direction
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

from config.settings import (
    PRICE_CHANGE_THRESHOLD,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_PERIOD,
)
from .rsi import RSICalculator

logger = logging.getLogger(__name__)


# ============================================================
# DATA CLASSES
# ============================================================
@dataclass
class FilterResult:
    """Result of a single filter"""
    passed: bool
    value: float
    description: str


@dataclass
class SignalResult:
    """Complete result of signal analysis"""
    symbol: str
    triggered: bool
    direction: Optional[str]      # "LONG", "SHORT", or None
    current_price: float

    filter_price: FilterResult    # Filter 1: price change
    filter_rsi_1h: FilterResult   # Filter 2: RSI 1h
    filter_rsi_15m: FilterResult  # Filter 3: RSI 15m

    def __str__(self) -> str:
        status = "✅ SIGNAL" if self.triggered else "❌ none"
        return (
            f"{self.symbol} [{status}] {self.direction or ''} "
            f"| Price: {self.filter_price.value:+.2f}% "
            f"| RSI_1h: {self.filter_rsi_1h.value:.1f} "
            f"| RSI_15m: {self.filter_rsi_15m.value:.1f}"
        )


# ============================================================
# ANALYZER
# ============================================================
class SignalAnalyzer:
    """
    Analyzes trade signals using 3 filters:

    Filter 1: Price change >= PRICE_CHANGE_THRESHOLD over 15 minutes
    Filter 2: RSI (1h) > RSI_OVERBOUGHT or < RSI_OVERSOLD
    Filter 3: RSI (15m) > RSI_OVERBOUGHT or < RSI_OVERSOLD

    Direction:
        LONG  — price rises + RSI oversold (< RSI_OVERSOLD)
        SHORT — price falls  + RSI overbought (> RSI_OVERBOUGHT)
    """

    @staticmethod
    def analyze(
        symbol: str,
        prices_1m: List[float],
        prices_15m: List[float],
        prices_1h: List[float],
        current_price: float,
    ) -> SignalResult:
        """
        Full analysis using three filters.

        Args:
            symbol:        Trading pair
            prices_1m:     1m candles (min 15)
            prices_15m:    15m candles (min 28)
            prices_1h:     1h candles (min 28)
            current_price: Current price

        Returns:
            SignalResult with all filter results
        """
        f1 = SignalAnalyzer._check_price(prices_1m)
        f2 = SignalAnalyzer._check_rsi(prices_1h, "1h")
        f3 = SignalAnalyzer._check_rsi(prices_15m, "15m")

        triggered = f1.passed and f2.passed and f3.passed
        direction = None

        if triggered:
            direction = SignalAnalyzer._get_direction(
                price_change=f1.value,
                rsi_1h=f2.value,
                rsi_15m=f3.value,
            )

        result = SignalResult(
            symbol=symbol,
            triggered=triggered,
            direction=direction,
            current_price=current_price,
            filter_price=f1,
            filter_rsi_1h=f2,
            filter_rsi_15m=f3,
        )

        if triggered:
            logger.info(f"🚨 {result}")
        else:
            logger.debug(f"   {result}")

        return result

    # ----------------------------------------------------------
    # PRIVATE
    # ----------------------------------------------------------
    @staticmethod
    def _check_price(prices_1m: List[float]) -> FilterResult:
        """Filter 1: price change over last 15 1m candles"""
        if len(prices_1m) < 15:
            return FilterResult(
                passed=False,
                value=0.0,
                description="Not enough data (1m)",
            )

        old = float(prices_1m[-15])
        new = float(prices_1m[-1])

        if old <= 0:
            return FilterResult(passed=False, value=0.0, description="Price <= 0")

        change = (new - old) / old * 100
        passed = abs(change) >= PRICE_CHANGE_THRESHOLD

        return FilterResult(
            passed=passed,
            value=round(change, 4),
            description=(
                f"{'✅' if passed else '❌'} Price {change:+.2f}% "
                f"(threshold ±{PRICE_CHANGE_THRESHOLD}%)"
            ),
        )

    @staticmethod
    def _check_rsi(prices: List[float], label: str) -> FilterResult:
        """Filter 2/3: RSI breaks overbought/oversold zones"""
        if not RSICalculator.is_valid(prices, RSI_PERIOD):
            return FilterResult(
                passed=False,
                value=0.0,
                description=f"Not enough RSI data ({label})",
            )

        rsi = RSICalculator.last(prices, RSI_PERIOD)
        passed = rsi > RSI_OVERBOUGHT or rsi < RSI_OVERSOLD

        if rsi > RSI_OVERBOUGHT:
            zone = f"overbought (>{RSI_OVERBOUGHT})"
        elif rsi < RSI_OVERSOLD:
            zone = f"oversold (<{RSI_OVERSOLD})"
        else:
            zone = "neutral zone"

        return FilterResult(
            passed=passed,
            value=round(rsi, 2),
            description=f"{'✅' if passed else '❌'} RSI {label} = {rsi:.1f} — {zone}",
        )

    @staticmethod
    def _get_direction(
        price_change: float,
        rsi_1h: float,
        rsi_15m: float,
    ) -> str:
        """
        Determine signal direction.

        LONG:  price rises + RSI oversold (bounce up)
        SHORT: price falls + RSI overbought (reversal down)
        """
        price_up = price_change > 0
        rsi_oversold = rsi_1h < RSI_OVERSOLD or rsi_15m < RSI_OVERSOLD
        rsi_overbought = rsi_1h > RSI_OVERBOUGHT or rsi_15m > RSI_OVERBOUGHT

        if price_up and rsi_oversold:
            return "LONG"
        if not price_up and rsi_overbought:
            return "SHORT"

        # If signal is ambiguous — use price direction
        return "LONG" if price_up else "SHORT"
