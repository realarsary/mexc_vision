"""
bot/services/telegram_service.py
Telegram service with send queue — spam protection and rate limiting
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError
from aiogram.types import BufferedInputFile


from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# Delay between messages (flood protection)
SEND_DELAY = 0.05   # 50ms = ~20 messages/sec (Telegram limit: 30/sec)
MAX_RETRIES = 3


class TelegramService:
    """
    Service for sending messages to Telegram.

    All messages go through an asyncio.Queue —
    even if 100 signals come at once,
    they will be sent one by one without getting banned by Telegram.
    """

    def __init__(self):
        self._bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # Metrics
        self.sent_total = 0
        self.sent_photos = 0
        self.failed_total = 0
        self.retries_total = 0

    # ----------------------------------------------------------
    # LIFECYCLE
    # ----------------------------------------------------------
    async def start(self) -> None:
        """Start the queue worker"""
        self._running = True
        self._task = asyncio.create_task(self._worker())
        logger.info("✅ TelegramService started")

    async def stop(self) -> None:
        """Wait for queue to finish and stop"""
        self._running = False
        await self._queue.join()
        if self._task:
            self._task.cancel()
        await self._bot.session.close()
        logger.info("🔒 TelegramService stopped")

    # ----------------------------------------------------------
    # PUBLIC — add to queue
    # ----------------------------------------------------------
    async def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        parse_mode: str = "HTML",
    ) -> None:
        """Put a text message into the queue"""
        await self._queue.put({
            "type": "text",
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
        })

    async def send_photo(
        self,
        photo_path: Path,
        caption: str = "",
        chat_id: Optional[str] = None,
        parse_mode: str = "HTML",
    ) -> None:
        """Put a photo into the queue"""
        await self._queue.put({
            "type": "photo",
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "photo_path": photo_path,
            "caption": caption,
            "parse_mode": parse_mode,
        })

    async def send_signal(
        self,
        text: str,
        photo_path: Optional[Path] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        """
        Send a signal — photo with caption if exists,
        otherwise just text.
        """
        if photo_path and photo_path.exists():
            await self.send_photo(
                photo_path=photo_path,
                caption=text,
                chat_id=chat_id,
            )
        else:
            await self.send_message(text=text, chat_id=chat_id)

    def queue_size(self) -> int:
        return self._queue.qsize()

    def get_metrics(self) -> dict:
        return {
            "sent_total": self.sent_total,
            "sent_photos": self.sent_photos,
            "failed_total": self.failed_total,
            "retries_total": self.retries_total,
            "queue_size": self.queue_size(),
        }

    # ----------------------------------------------------------
    # PRIVATE — worker
    # ----------------------------------------------------------
    async def _worker(self) -> None:
        """Background worker — sends messages from queue one by one"""
        while self._running or not self._queue.empty():
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                await self._dispatch(item)
            except Exception as e:
                logger.error(f"❌ Sending error: {e}")
                self.failed_total += 1
            finally:
                self._queue.task_done()
                await asyncio.sleep(SEND_DELAY)

    async def _dispatch(self, item: dict) -> None:
        """Send a single message with retry"""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if item["type"] == "text":
                    await self._bot.send_message(
                        chat_id=item["chat_id"],
                        text=item["text"],
                        parse_mode=item["parse_mode"],
                    )
                    self.sent_total += 1

                elif item["type"] == "photo":
                    with open(item["photo_path"], "rb") as f:
                        photo_data = BufferedInputFile(
                            f.read(),
                            filename=item["photo_path"].name
                        )
                    await self._bot.send_photo(
                        chat_id=item["chat_id"],
                        photo=photo_data,
                        caption=item["caption"][:1024],
                        parse_mode=item["parse_mode"],
                    )
                    self.sent_total += 1
                    self.sent_photos += 1

                return  # success — exit

            except TelegramRetryAfter as e:
                # Telegram asks to wait
                wait = e.retry_after + 1
                logger.warning(f"⏳ Telegram flood wait: {wait}s")
                self.retries_total += 1
                await asyncio.sleep(wait)

            except TelegramAPIError as e:
                logger.error(f"❌ Telegram API error (attempt {attempt}): {e}")
                self.retries_total += 1
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1.0 * attempt)
                else:
                    self.failed_total += 1
                    raise
