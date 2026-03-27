"""
bot/utils/chart_generator.py
Chart generator — candlestick 5m (last 12h) + RSI 5m + Volume with avg-of-top5-max line
"""

import logging
import time
from pathlib import Path
from typing import List, Optional, Dict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.gridspec import GridSpec
import numpy as np

from services.analysis.rsi import RSICalculator
from config.settings import RSI_PERIOD, RSI_OVERBOUGHT, RSI_OVERSOLD

logger = logging.getLogger(__name__)

CHARTS_DIR = Path("charts")
MAX_CHART_AGE_HOURS = 2
MAX_CHARTS_COUNT = 200

CANDLES_12H = 144       # 5m * 144 = 12h
VOLUME_LOOKBACK = 200   # how many candles back for top-5 volume calc


class ChartGenerator:
    """
    Generates a PNG chart with 3 panels:
      - Top:    Candlestick 5m (last 12h = 144 candles)
      - Middle: RSI 5m with OB/OS zones
      - Bottom: Volume bars + horizontal line = avg of top-5 max volumes (last 200 candles)
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
        candles_5m: Optional[List] = None,   # OHLCV list — preferred
        # Legacy params kept for backward compat (ignored if candles_5m given)
        prices_1h: Optional[List[float]] = None,
        prices_15m: Optional[List[float]] = None,
        current_price: float = 0.0,
        direction: str = "LONG",
        price_change: float = 0.0,
        rsi_1h: float = 50.0,
        rsi_15m: float = 50.0,
    ) -> Optional[Path]:
        self._cleanup()
        try:
            path = self._draw(
                symbol=symbol,
                candles_5m=candles_5m or [],
                current_price=current_price,
                direction=direction,
                price_change=price_change,
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
        candles_5m: List,
        current_price: float,
        direction: str,
        price_change: float,
    ) -> Path:
        ohlcv = _parse_candles(candles_5m)
        if not ohlcv:
            raise ValueError("No 5m candle data")

        # Slice for display (last 12h)
        display = ohlcv[-CANDLES_12H:]
        n = len(display)

        opens  = np.array([c["open"]   for c in display], dtype=float)
        highs  = np.array([c["high"]   for c in display], dtype=float)
        lows   = np.array([c["low"]    for c in display], dtype=float)
        closes = np.array([c["close"]  for c in display], dtype=float)
        vols   = np.array([c["volume"] for c in display], dtype=float)

        # RSI — use all available candles for warmup accuracy, then slice
        all_closes = [c["close"] for c in ohlcv]
        rsi_full   = RSICalculator.calculate(all_closes, RSI_PERIOD)
        rsi_series = np.array(rsi_full[-n:], dtype=float)

        x = np.arange(n)

        # --- Palette ---
        is_long      = direction == "LONG"
        signal_color = "#00e676" if is_long else "#ff1744"
        bg_color     = "#0d0d14"
        grid_color   = "#1a1a2e"
        text_color   = "#d0d0e0"
        bull_color   = "#00e676"
        bear_color   = "#ff1744"
        vol_bull     = "#2979ff"
        vol_bear     = "#d32f2f"
        avg_color    = "#ffd740"

        # --- Figure ---
        fig = plt.figure(figsize=(14, 9), facecolor=bg_color)
        gs  = GridSpec(3, 1, figure=fig, height_ratios=[3, 1.2, 1.2], hspace=0.05)

        ax_c   = fig.add_subplot(gs[0])
        ax_rsi = fig.add_subplot(gs[1], sharex=ax_c)
        ax_vol = fig.add_subplot(gs[2], sharex=ax_c)

        for ax in (ax_c, ax_rsi, ax_vol):
            ax.set_facecolor(bg_color)
            ax.tick_params(colors=text_color, labelsize=8)
            ax.spines[:].set_color(grid_color)
            ax.grid(color=grid_color, linewidth=0.4, linestyle="--", alpha=0.7)

        # ── TOP: Candlestick ──────────────────────────────────
        w = 0.55
        for i in range(n):
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            col = bull_color if c >= o else bear_color
            ax_c.plot([i, i], [l, h], color=col, linewidth=0.8, zorder=2)
            body_y = min(o, c)
            body_h = max(abs(c - o), (h - l) * 0.003)
            rect = mpatches.FancyBboxPatch(
                (i - w / 2, body_y), w, body_h,
                boxstyle="square,pad=0",
                facecolor=col, edgecolor=col, linewidth=0, zorder=3,
            )
            ax_c.add_patch(rect)

        ax_c.axhline(current_price, color=signal_color, linewidth=0.9,
                     linestyle="--", alpha=0.9, zorder=4)
        ax_c.text(n - 0.5, current_price, f"  {_fmt_price(current_price)}",
                  color=signal_color, fontsize=7.5, va="center", zorder=5)

        label = "🟢 LONG" if is_long else "🔴 SHORT"
        ax_c.set_title(
            f"{symbol}  5m  ·  {label}  ·  ${_fmt_price(current_price)}  ({price_change:+.2f}%)",
            color=text_color, fontsize=12, fontweight="bold", pad=10,
        )
        ax_c.set_ylabel("Цена", color=text_color, fontsize=9)
        ax_c.tick_params(labelbottom=False)
        ax_c.set_xlim(-0.7, n - 0.3)

        # ── MIDDLE: RSI 5m ────────────────────────────────────
        rsi_x   = np.arange(len(rsi_series))
        rsi_off = n - len(rsi_series)
        last_rsi = float(rsi_series[-1]) if len(rsi_series) else 50.0

        ax_rsi.plot(rsi_x + rsi_off, rsi_series,
                    color="#ffa726", linewidth=1.4,
                    label=f"RSI 5m  {last_rsi:.1f}")

        ax_rsi.axhline(RSI_OVERBOUGHT, color=bear_color, linewidth=0.8, linestyle="--")
        ax_rsi.axhline(RSI_OVERSOLD,   color=bull_color, linewidth=0.8, linestyle="--")
        ax_rsi.axhline(50, color=grid_color, linewidth=0.5)

        ax_rsi.fill_between(rsi_x + rsi_off, RSI_OVERBOUGHT, 100, alpha=0.07, color=bear_color)
        ax_rsi.fill_between(rsi_x + rsi_off, 0, RSI_OVERSOLD,     alpha=0.07, color=bull_color)

        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_ylabel("RSI", color=text_color, fontsize=9)
        ax_rsi.tick_params(labelbottom=False)
        ax_rsi.legend(loc="upper left", fontsize=8, facecolor=bg_color,
                      labelcolor=text_color, edgecolor=grid_color)
        ax_rsi.text(0.995, RSI_OVERBOUGHT / 100, f" {RSI_OVERBOUGHT}",
                    transform=ax_rsi.get_yaxis_transform(),
                    color=bear_color, fontsize=7, va="center")
        ax_rsi.text(0.995, RSI_OVERSOLD / 100, f" {RSI_OVERSOLD}",
                    transform=ax_rsi.get_yaxis_transform(),
                    color=bull_color, fontsize=7, va="center")

        # ── BOTTOM: Volume ────────────────────────────────────
        vcols = [vol_bull if closes[i] >= opens[i] else vol_bear for i in range(n)]
        ax_vol.bar(x, vols, color=vcols, width=0.6, alpha=0.9, zorder=2)

        # Dynamic rolling top-5 avg line (computed per candle, not a flat line)
        rolling_avg = _rolling_top5_avg(vols, window=5)
        ax_vol.plot(
            x, rolling_avg,
            color=avg_color, linewidth=1.3, linestyle="-", zorder=3,
            label=f"Roll top-5 avg  {_fmt_vol(float(rolling_avg[-1]))}",
        )

        ax_vol.set_ylabel("Объём", color=text_color, fontsize=9)
        ax_vol.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda v, _: _fmt_vol(v))
        )
        ax_vol.legend(loc="upper left", fontsize=8, facecolor=bg_color,
                      labelcolor=text_color, edgecolor=grid_color)

        # X-axis time labels — date+time like MEXC web
        x_labels, x_ticks = _make_time_labels(display, max_labels=9)
        ax_vol.set_xticks(x_ticks)
        ax_vol.set_xticklabels(x_labels, color=text_color, fontsize=7, rotation=15, ha="right")

        fig.text(0.99, 0.005, f"MEXC Signal Bot · {_now_utc()}",
                 ha="right", va="bottom", color="#44445a", fontsize=7)

        plt.tight_layout()

        fname = f"{symbol}_{int(time.time())}.png"
        path  = CHARTS_DIR / fname
        fig.savefig(path, dpi=130, bbox_inches="tight", facecolor=bg_color)
        plt.close(fig)
        return path

    # ----------------------------------------------------------
    @staticmethod
    def _setup_style() -> None:
        plt.rcParams.update({"font.family": "monospace", "axes.unicode_minus": False})

    @staticmethod
    def _cleanup() -> None:
        try:
            files = sorted(CHARTS_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime)
            now, max_age, deleted = time.time(), MAX_CHART_AGE_HOURS * 3600, 0
            for f in files:
                if now - f.stat().st_mtime > max_age:
                    f.unlink(missing_ok=True); deleted += 1
            files = sorted(CHARTS_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime)
            while len(files) > MAX_CHARTS_COUNT:
                files[0].unlink(missing_ok=True); files.pop(0); deleted += 1
            if deleted:
                logger.debug(f"🗑 Deleted old charts: {deleted}")
        except Exception as e:
            logger.warning(f"⚠️ Error cleaning charts/: {e}")


# ============================================================
# HELPERS
# ============================================================

def _parse_candles(raw: List) -> List[Dict]:
    """
    Accept:
      • list of dicts  {"open","high","low","close","volume"[,"time"]}
      • MEXC kline list [time, open, close, high, low, vol, ...]
      • close-only floats (fallback — no real OHLCV)
    """
    result = []
    for item in raw:
        if isinstance(item, dict):
            result.append({
                "open":   float(item.get("open",   item.get("o", 0))),
                "high":   float(item.get("high",   item.get("h", 0))),
                "low":    float(item.get("low",    item.get("l", 0))),
                "close":  float(item.get("close",  item.get("c", 0))),
                "volume": float(item.get("volume", item.get("v", 0))),
                "time":   item.get("time", item.get("t")),
            })
        elif isinstance(item, (list, tuple)) and len(item) >= 5:
            # MEXC futures kline: [time, open, close, high, low, vol, ...]
            try:
                t  = int(item[0])
                o  = float(item[1])
                c  = float(item[2])
                h  = float(item[3])
                l  = float(item[4])
                v  = float(item[5]) if len(item) > 5 else 0.0
                result.append({"open": o, "high": h, "low": l,
                                "close": c, "volume": v, "time": t})
            except (ValueError, TypeError):
                pass
        elif isinstance(item, (int, float)):
            p = float(item)
            result.append({"open": p, "high": p, "low": p,
                            "close": p, "volume": 0.0, "time": None})
    return result


def _top5_avg_volume(vols: np.ndarray) -> float:
    if len(vols) == 0:
        return 0.0
    k    = min(5, len(vols))
    top5 = np.partition(vols, -k)[-k:]
    return float(np.mean(top5))


def _rolling_top5_avg(vols: np.ndarray, window: int = 5) -> np.ndarray:
    """
    Rolling average of top-5 max volumes within a sliding window.
    For each candle i, takes the last `window` candles and computes avg of top-5.
    Result is a dynamic line (not flat), similar to what MEXC shows.
    """
    result = np.zeros(len(vols), dtype=float)
    for i in range(len(vols)):
        start = max(0, i - window + 1)
        chunk = vols[start:i + 1]
        k = min(5, len(chunk))
        if k == 0:
            result[i] = 0.0
        else:
            top_k = np.partition(chunk, -k)[-k:]
            result[i] = float(np.mean(top_k))
    return result


def _make_time_labels(ohlcv: List[Dict], max_labels: int = 9):
    """
    Generate X-axis time labels in MEXC web style:
    - Shows date if day changes: "Mar 27\n14:00"
    - Otherwise just time: "14:00"
    """
    n     = len(ohlcv)
    ticks = np.linspace(0, n - 1, min(max_labels, n), dtype=int)
    labels = []
    prev_day = None
    for i in ticks:
        t = ohlcv[i].get("time")
        if t is not None:
            try:
                from datetime import datetime, timezone
                ts = int(t)
                if ts > 1e10:
                    ts //= 1000
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                day_str = dt.strftime("%b %d")
                time_str = dt.strftime("%H:%M")
                if prev_day is None or day_str != prev_day:
                    labels.append(f"{day_str}\n{time_str}")
                    prev_day = day_str
                else:
                    labels.append(time_str)
            except Exception:
                labels.append(str(i))
        else:
            labels.append(str(i))
    return labels, ticks.tolist()


def _fmt_price(v: float) -> str:
    if v == 0:
        return "0"
    if v < 0.0001:
        return f"{v:.8f}".rstrip("0")
    if v < 1:
        return f"{v:.6f}".rstrip("0")
    if v < 100:
        return f"{v:.4f}"
    return f"{v:.2f}"


def _fmt_vol(v: float) -> str:
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v/1_000:.0f}K"
    return f"{v:.0f}"


def _now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")