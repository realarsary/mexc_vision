"""
services/analysis/rsi.py
RSI calculator — Wilder's Smoothing algorithm (as in TradingView)
"""

import logging
from typing import List

import numpy as np

logger = logging.getLogger(__name__)


class RSICalculator:
    """
    Calculates RSI (Relative Strength Index).
    Algorithm: Wilder's Smoothing — identical to TradingView.
    """

    @staticmethod
    def calculate(prices: List[float], period: int = 14) -> List[float]:
        """
        Calculate RSI for the entire list of prices.

        Args:
            prices: List of closing prices
            period: RSI period (usually 14)

        Returns:
            List of RSI values of the same length.
            The first `period` values = 0.0 (not enough data).
        """
        if not prices or len(prices) < 2:
            logger.debug("RSI: not enough data")
            return [0.0] * len(prices)

        arr = np.array(prices, dtype=float)
        n = len(arr)

        if n <= period:
            return [0.0] * n

        deltas = np.diff(arr)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        result = [0.0] * period  # first `period` values = 0

        # Initialization: simple average of first `period` bars
        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))

        result.append(_rsi_from_avg(avg_gain, avg_loss))

        # Wilder's Smoothing for the rest
        for i in range(period, n - 1):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            result.append(_rsi_from_avg(avg_gain, avg_loss))

        return result

    @staticmethod
    def last(prices: List[float], period: int = 14) -> float:
        """
        Get the last RSI value.

        Args:
            prices: List of closing prices (at least period+1 candles)
            period: RSI period

        Returns:
            Last RSI value (0.0 if not enough data)
        """
        values = RSICalculator.calculate(prices, period)
        return values[-1] if values else 0.0

    @staticmethod
    def is_valid(prices: List[float], period: int = 14) -> bool:
        """Check if there is enough data for a reliable RSI"""
        # Minimum of period*2 candles needed for stable value
        return len(prices) >= period * 2


def _rsi_from_avg(avg_gain: float, avg_loss: float) -> float:
    """Calculate RSI from average gain/loss"""
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
