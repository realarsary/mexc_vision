"""
config/settings.py
Centralized configuration with validation
"""

import os
import logging
from pathlib import Path
from typing import List
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ============================================================
# TELEGRAM
# ============================================================
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
# MEXC API
# ============================================================
MEXC_BASE_URL: str = os.getenv("MEXC_BASE_URL", "https://contract.mexc.com")
MEXC_API_TIMEOUT: int = int(os.getenv("MEXC_API_TIMEOUT", "30"))
MEXC_MAX_CONNECTIONS: int = int(os.getenv("MEXC_MAX_CONNECTIONS", "100"))

# ============================================================
# BOT OPERATION
# ============================================================
CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "60"))
MAX_CONCURRENT_REQUESTS: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "20"))
REQUESTS_PER_SECOND: float = float(os.getenv("REQUESTS_PER_SECOND", "15"))

# ============================================================
# TRADING FILTERS
# ============================================================
PRICE_CHANGE_THRESHOLD: float = float(os.getenv("PRICE_CHANGE_THRESHOLD", "8"))
PRICE_CHECK_PERIOD_MINUTES: int = int(os.getenv("PRICE_CHECK_PERIOD_MINUTES", "15"))
RSI_PERIOD: int = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERBOUGHT: int = int(os.getenv("RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD: int = int(os.getenv("RSI_OVERSOLD", "30"))

# ============================================================
# SIGNAL SETTINGS
# ============================================================
SIGNAL_COOLDOWN: int = int(os.getenv("SIGNAL_COOLDOWN", "300"))
SEND_CHART: bool = os.getenv("SEND_CHART", "true").lower() == "true"
SEND_DETAILED_ANALYSIS: bool = os.getenv("SEND_DETAILED_ANALYSIS", "true").lower() == "true"

# ============================================================
# LOGGING
# ============================================================
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE: Path = Path(os.getenv("LOG_FILE", "logs/bot.log"))
LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", "10485760"))  # 10MB
LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "5"))

# ============================================================
# DATABASE
# ============================================================
DATABASE_PATH: Path = Path(os.getenv("DATABASE_PATH", "data/signals.db"))

# ============================================================
# PAIRS FILTER
# ============================================================
_whitelist = os.getenv("TRADING_PAIRS_WHITELIST", "")
TRADING_PAIRS_WHITELIST: List[str] = (
    [p.strip() for p in _whitelist.split(",") if p.strip()] if _whitelist else []
)

_blacklist = os.getenv("TRADING_PAIRS_BLACKLIST", "")
TRADING_PAIRS_BLACKLIST: List[str] = (
    [p.strip() for p in _blacklist.split(",") if p.strip()] if _blacklist else []
)

# ============================================================
# DIRECTORIES — created on import
# ============================================================
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
Path("charts").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)


# ============================================================
# VALIDATION
# ============================================================
class ConfigError(Exception):
    """Critical configuration error"""
    pass


def validate() -> None:
    """
    Validate required settings.
    Raises ConfigError if something is wrong.
    """
    errors: List[str] = []
    warnings: List[str] = []

    # --- Required ---
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "your_bot_token_here":
        errors.append("TELEGRAM_BOT_TOKEN is not set in .env")

    if not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == "your_chat_id_here":
        errors.append("TELEGRAM_CHAT_ID is not set in .env")

    if CHECK_INTERVAL < 10:
        errors.append("CHECK_INTERVAL must be >= 10 seconds")

    if PRICE_CHANGE_THRESHOLD <= 0:
        errors.append("PRICE_CHANGE_THRESHOLD must be > 0")

    if RSI_OVERBOUGHT <= RSI_OVERSOLD:
        errors.append("RSI_OVERBOUGHT must be > RSI_OVERSOLD")

    if RSI_PERIOD < 2:
        errors.append("RSI_PERIOD must be >= 2")

    # --- Warnings ---
    if SIGNAL_COOLDOWN < 60:
        warnings.append("SIGNAL_COOLDOWN < 60 sec — possible Telegram spam")

    if PRICE_CHANGE_THRESHOLD < 5:
        warnings.append("PRICE_CHANGE_THRESHOLD < 5% — many false signals")

    if REQUESTS_PER_SECOND > 20:
        warnings.append("REQUESTS_PER_SECOND > 20 — possible MEXC rate limit")

    # --- Result ---
    for w in warnings:
        logger.warning(f"⚠️  {w}")

    if errors:
        for e in errors:
            logger.error(f"❌ {e}")
        raise ConfigError(f"{len(errors)} configuration errors found. Fix .env and restart.")

    logger.info("✅ Configuration is valid")


def print_summary() -> None:
    """Print configuration summary to logs (without full token)"""
    token_safe = (
        f"{TELEGRAM_BOT_TOKEN[:10]}...{TELEGRAM_BOT_TOKEN[-4:]}"
        if len(TELEGRAM_BOT_TOKEN) > 14 else "not set"
    )
    logger.info("=" * 60)
    logger.info("📋 CONFIGURATION")
    logger.info("=" * 60)
    logger.info(f"  Telegram token : {token_safe}")
    logger.info(f"  Telegram chat  : {TELEGRAM_CHAT_ID}")
    logger.info(f"  MEXC URL       : {MEXC_BASE_URL}")
    logger.info(f"  Check interval : {CHECK_INTERVAL}s")
    logger.info(f"  Signal cooldown: {SIGNAL_COOLDOWN}s")
    logger.info(f"  Price threshold: {PRICE_CHANGE_THRESHOLD}%")
    logger.info(f"  RSI period     : {RSI_PERIOD}")
    logger.info(f"  RSI OB/OS      : {RSI_OVERBOUGHT}/{RSI_OVERSOLD}")
    logger.info(f"  Send chart     : {SEND_CHART}")
    logger.info(f"  Database       : {DATABASE_PATH}")
    logger.info(f"  Log file       : {LOG_FILE}")
    if TRADING_PAIRS_WHITELIST:
        logger.info(f"  Whitelist      : {len(TRADING_PAIRS_WHITELIST)} pairs")
    if TRADING_PAIRS_BLACKLIST:
        logger.info(f"  Blacklist      : {len(TRADING_PAIRS_BLACKLIST)} pairs")
    logger.info("=" * 60)
