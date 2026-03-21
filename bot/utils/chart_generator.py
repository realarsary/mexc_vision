"""
bot/utils/chart_generator.py
Chart generator for trading signals — with automatic cleanup of old files
"""

import logging
import time
from pathlib import Path
from typing import List, Optional

import matplotlib
matplotlib.use("Agg")  # no GUI — must be before importing pyplot
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import numpy as np

from services.analysis.rsi import RSICalculator
from config.settings import RSI_PERIOD, RSI_OVERBOUGHT, RSI_OVERSOLD

logger = logging.getLogger(__name__)

CHARTS_DIR = Path("charts")
MAX_CHART_AGE_HOURS = 2   # delete charts older than 2 hours
MAX_CHARTS_COUNT = 200    # max number of files in folder


class ChartGenerator:
    """
    Generates a PNG chart for a trading signal.

    Chart has 2 panels:
      - Top: price line (last 50 1h candles)
      - Bottom: RSI 1h and RSI 15m with OB/OS zones
    """

    def __init__(self):
        CHARTS_DIR.mkdir(exist_ok=True)
        self._setup_style()

    # ----------------------------------------------------------
    # PUBLIC
    # ----------------------------------------------------------
    def generate(
        self,
        symbol: str,
        prices_1h: List[float],
        prices_15m: List[float],
        current_price: float,
        direction: str,
        price_change: float,
        rsi_1h: float,
        rsi_15m: float,
    ) -> Optional[Path]:
        """
        Generate a PNG chart for a signal.

        Returns:
            Path to the PNG file or None on error
        """
        self._cleanup()

        try:
            path = self._draw(
                symbol=symbol,
                prices_1h=prices_1h,
                prices_15m=prices_15m,
                current_price=current_price,
                direction=direction,
                price_change=price_change,
                rsi_1h=rsi_1h,
                rsi_15m=rsi_15m,
            )
            logger.debug(f"📊 Chart created: {path}")
            return path

        except Exception as e:
            logger.error(f"❌ Error generating chart {symbol}: {e}", exc_info=True)
            return None

    # ----------------------------------------------------------
    # PRIVATE — drawing
    # ----------------------------------------------------------
    def _draw(
        self,
        symbol: str,
        prices_1h: List[float],
        prices_15m: List[float],
        current_price: float,
        direction: str,
        price_change: float,
        rsi_1h: float,
        rsi_15m: float,
    ) -> Path:
        # Take last 50 candles for display
        display = prices_1h[-50:] if len(prices_1h) >= 50 else prices_1h

        # Compute RSI series
        rsi_1h_series = RSICalculator.calculate(prices_1h, RSI_PERIOD)[-50:]
        rsi_15m_series = RSICalculator.calculate(prices_15m, RSI_PERIOD)[-50:]

        # Align lengths
        n = len(display)
        x = np.arange(n)
        rsi_1h_series = rsi_1h_series[-n:] if len(rsi_1h_series) >= n else rsi_1h_series
        rsi_15m_series = rsi_15m_series[-n:] if len(rsi_15m_series) >= n else rsi_15m_series
        x_rsi_1h = np.arange(len(rsi_1h_series))
        x_rsi_15m = np.arange(len(rsi_15m_series))

        # --- Colors ---
        is_long = direction == "LONG"
        signal_color = "#00c853" if is_long else "#ff1744"
        signal_emoji = "🟢 LONG" if is_long else "🔴 SHORT"
        bg_color = "#0f0f17"
        grid_color = "#1e1e2e"
        text_color = "#e0e0e0"
        price_color = "#82b1ff"

        # --- Figure ---
        fig = plt.figure(figsize=(12, 7), facecolor=bg_color)
        gs = GridSpec(2, 1, figure=fig, height_ratios=[2, 1], hspace=0.08)

        ax_price = fig.add_subplot(gs[0])
        ax_rsi = fig.add_subplot(gs[1], sharex=ax_price)

        for ax in (ax_price, ax_rsi):
            ax.set_facecolor(bg_color)
            ax.tick_params(colors=text_color, labelsize=8)
            ax.spines[:].set_color(grid_color)
            ax.grid(color=grid_color, linewidth=0.5, linestyle="--")

        # === TOP PANEL — Price ===
        ax_price.plot(x, display, color=price_color, linewidth=1.5, zorder=3)
        ax_price.fill_between(x, display, min(display), alpha=0.08, color=price_color)

        # Current price line
        ax_price.axhline(
            current_price, color=signal_color,
            linewidth=1.0, linestyle="--", alpha=0.8
        )

        # Last price point
        ax_price.scatter([x[-1]], [display[-1]], color=signal_color, s=60, zorder=5)

        # Title
        ax_price.set_title(
            f"{symbol}  |  {signal_emoji}  |  "
            f"${current_price:.4f}  ({price_change:+.2f}%)",
            color=text_color, fontsize=12, fontweight="bold", pad=10,
        )
        ax_price.set_ylabel("Price (USDT)", color=text_color, fontsize=9)
        ax_price.tick_params(labelbottom=False)

        # === BOTTOM PANEL — RSI ===
        ax_rsi.plot(
            x_rsi_1h, rsi_1h_series,
            color="#ff9800", linewidth=1.5, label=f"RSI 1h ({rsi_1h:.1f})"
        )
        ax_rsi.plot(
            x_rsi_15m, rsi_15m_series,
            color="#ab47bc", linewidth=1.2, linestyle="--",
            label=f"RSI 15m ({rsi_15m:.1f})"
        )

        # Overbought/oversold zones
        ax_rsi.axhline(RSI_OVERBOUGHT, color="#ff1744", linewidth=0.8, linestyle=":")
        ax_rsi.axhline(RSI_OVERSOLD, color="#00c853", linewidth=0.8, linestyle=":")
        ax_rsi.axhline(50, color=grid_color, linewidth=0.6)

        ax_rsi.fill_between(x_rsi_1h, RSI_OVERBOUGHT, 100, alpha=0.08, color="#ff1744")
        ax_rsi.fill_between(x_rsi_1h, 0, RSI_OVERSOLD, alpha=0.08, color="#00c853")

        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_ylabel("RSI", color=text_color, fontsize=9)
        ax_rsi.set_xlabel("Candles", color=text_color, fontsize=9)

        legend = ax_rsi.legend(
            loc="upper left", fontsize=8,
            facecolor=bg_color, labelcolor=text_color,
            edgecolor=grid_color,
        )

        # RSI level labels
        ax_rsi.text(
            0.99, RSI_OVERBOUGHT / 100,
            f" {RSI_OVERBOUGHT}", transform=ax_rsi.get_yaxis_transform(),
            color="#ff1744", fontsize=7, va="center",
        )
        ax_rsi.text(
            0.99, RSI_OVERSOLD / 100,
            f" {RSI_OVERSOLD}", transform=ax_rsi.get_yaxis_transform(),
            color="#00c853", fontsize=7, va="center",
        )

        # Generation time
        fig.text(
            0.99, 0.01,
            f"MEXC Signal Bot • {_now_utc()}",
            ha="right", va="bottom", color="#555566", fontsize=7,
        )

        plt.tight_layout()

        # --- Save ---
        filename = f"{symbol}_{int(time.time())}.png"
        path = CHARTS_DIR / filename
        fig.savefig(path, dpi=120, bbox_inches="tight", facecolor=bg_color)
        plt.close(fig)

        return path

    # ----------------------------------------------------------
    # PRIVATE — style and cleanup
    # ----------------------------------------------------------
    @staticmethod
    def _setup_style() -> None:
        plt.rcParams.update({
            "font.family": "monospace",
            "axes.unicode_minus": False,
        })

    @staticmethod
    def _cleanup() -> None:
        """
        Delete old charts:
        - files older than MAX_CHART_AGE_HOURS
        - if more than MAX_CHARTS_COUNT files — remove oldest
        """
        try:
            files = sorted(CHARTS_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime)

            now = time.time()
            max_age = MAX_CHART_AGE_HOURS * 3600
            deleted = 0

            # Remove by age
            for f in files:
                if now - f.stat().st_mtime > max_age:
                    f.unlink(missing_ok=True)
                    deleted += 1

            # Remove by count
            files = sorted(CHARTS_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime)
            while len(files) > MAX_CHARTS_COUNT:
                files[0].unlink(missing_ok=True)
                files.pop(0)
                deleted += 1

            if deleted:
                logger.debug(f"🗑 Deleted old charts: {deleted}")

        except Exception as e:
            logger.warning(f"⚠️ Error cleaning charts/: {e}")


def _now_utc() -> str:
    """Current UTC time in readable format"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
