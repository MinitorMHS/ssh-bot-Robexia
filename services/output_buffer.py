"""
output_buffer.py - سیستم نمایش خروجی ترمینال

معماری per-command:
- هر دستور یک CommandBuffer جدید می‌سازد
- اولین flush بعد از FIRST_FLUSH_DELAY ثانیه (پاسخ سریع)
- بعد از آن هر EDIT_INTERVAL ثانیه ادیت می‌شود
- ادیت فقط اگر محتوا تغییر کرده باشد
- ChatRateLimiter: حداقل MIN_EDIT_GAP بین ادیت‌های یک chat
"""

import asyncio
import logging
import re
import time
from collections import deque
from typing import Optional
from telegram import Bot
from telegram.error import TelegramError, RetryAfter, BadRequest

logger = logging.getLogger(__name__)

FIRST_FLUSH_DELAY = 1.5   # اولین flush سریع (ثانیه)
EDIT_INTERVAL = 7.0        # فاصله ادیت‌های بعدی
MIN_EDIT_GAP = 3.0         # حداقل فاصله بین ادیت‌ها (rate limit)
MAX_LINES = 40
MAX_LINE_LEN = 300
MAX_MSG_CHARS = 3800

_ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|[\(\)][A-Za-z0-9])')
_CTRL_RE = re.compile(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]')


def clean(text: str) -> str:
    text = _ANSI_RE.sub('', text)
    text = _CTRL_RE.sub('', text)
    return text.replace('\r\n', '\n').replace('\r', '\n')


def esc(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def trim(line: str) -> str:
    return line[:MAX_LINE_LEN] + '…' if len(line) > MAX_LINE_LEN else line


class ChatRateLimiter:
    def __init__(self):
        self._last = 0.0

    async def wait(self):
        gap = time.time() - self._last
        if gap < MIN_EDIT_GAP:
            await asyncio.sleep(MIN_EDIT_GAP - gap)

    def record(self):
        self._last = time.time()


class CommandBuffer:
    """بافر خروجی برای یک دستور - یک پیام تلگرام"""

    def __init__(self, bot: Bot, chat_id: int, rate: ChatRateLimiter):
        self.bot = bot
        self.chat_id = chat_id
        self._rate = rate
        self._lines: deque = deque(maxlen=MAX_LINES)
        self._partial: str = ""
        self.msg_id: Optional[int] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_sent = ""
        self._dirty = False
        self._frozen = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def freeze(self):
        self._frozen = True
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush()

    async def append(self, raw: str):
        if not raw or self._frozen:
            return
        cleaned = clean(raw)
        if not cleaned:
            return
        async with self._lock:
            combined = self._partial + cleaned
            parts = combined.split('\n')
            for line in parts[:-1]:
                self._lines.append(trim(line))
            self._partial = parts[-1]
            if len(self._partial) > MAX_LINE_LEN:
                self._lines.append(trim(self._partial))
                self._partial = ""
            self._dirty = True

    def _build(self) -> str:
        lines = list(self._lines)
        if self._partial:
            lines.append(self._partial + "▌")
        if not lines:
            return ""
        text = '\n'.join(lines)
        if len(text) > MAX_MSG_CHARS:
            text = text[-MAX_MSG_CHARS:]
            nl = text.find('\n')
            if nl > 0:
                text = text[nl + 1:]
        return text

    async def _loop(self):
        try:
            # اولین flush سریع
            await asyncio.sleep(FIRST_FLUSH_DELAY)
            await self._flush()
            # بعد از آن هر EDIT_INTERVAL
            while self._running:
                await asyncio.sleep(EDIT_INTERVAL)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self):
        async with self._lock:
            if not self._dirty:
                return
            content = self._build()
            if not content.strip() or content == self._last_sent:
                self._dirty = False
                return

            await self._rate.wait()
            html = f"<pre>{esc(content)}</pre>"
            try:
                if self.msg_id is None:
                    msg = await self.bot.send_message(
                        chat_id=self.chat_id, text=html, parse_mode="HTML"
                    )
                    self.msg_id = msg.message_id
                else:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.msg_id,
                        text=html, parse_mode="HTML",
                    )
                self._rate.record()
                self._last_sent = content
                self._dirty = False
            except RetryAfter as e:
                logger.warning(f"RetryAfter {e.retry_after}s")
                await asyncio.sleep(e.retry_after + 1)
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    self._dirty = False
                else:
                    logger.warning(f"BadRequest: {e}")
                    self.msg_id = None
                    self._dirty = False
            except TelegramError as e:
                logger.warning(f"TelegramError: {e}")


class SessionOutputManager:
    """مدیر خروجی یک session SSH"""

    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self._rate = ChatRateLimiter()
        self._current: Optional[CommandBuffer] = None

    async def new_command(self) -> CommandBuffer:
        if self._current:
            asyncio.create_task(self._current.freeze())
        buf = CommandBuffer(self.bot, self.chat_id, self._rate)
        await buf.start()
        self._current = buf
        return buf

    async def append(self, raw: str):
        if self._current:
            await self._current.append(raw)

    async def send_system(self, text: str, reply_markup=None):
        await self._rate.wait()
        try:
            await self.bot.send_message(
                chat_id=self.chat_id, text=text,
                parse_mode="HTML", reply_markup=reply_markup,
            )
            self._rate.record()
        except TelegramError as e:
            logger.warning(f"System msg error: {e}")

    async def stop(self):
        if self._current:
            try:
                await asyncio.wait_for(self._current.freeze(), timeout=5)
            except Exception:
                pass
            self._current = None
