"""ssh_manager.py - مدیریت session های SSH"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

import asyncssh
from telegram import Bot

from services.output_buffer import SessionOutputManager
from keyboards.terminal_kb import detect_terminal_mode, get_keyboard_for_mode

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = 300
WAIT_TIMEOUT = 900
WATCHDOG_INTERVAL = 30


@dataclass
class UserSession:
    user_id: int
    chat_id: int
    host: str
    port: int
    username: str
    log_id: int = 0

    connection: Optional[asyncssh.SSHClientConnection] = None
    process: Optional[asyncssh.SSHClientProcess] = None
    output_mgr: Optional[SessionOutputManager] = None
    reader_task: Optional[asyncio.Task] = None

    state: str = "active"
    terminal_mode: str = "normal"
    last_activity: float = field(default_factory=time.time)
    _last_sample: str = ""

    def touch(self):
        self.last_activity = time.time()

    def update_sample(self, text: str):
        self._last_sample = (self._last_sample + text)[-5000:]


class SSHManager:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.sessions: Dict[int, UserSession] = {}
        self._lock = asyncio.Lock()
        self._watchdog_task: Optional[asyncio.Task] = None

    def get_session(self, user_id: int) -> Optional[UserSession]:
        return self.sessions.get(user_id)

    async def start_watchdog(self):
        if not self._watchdog_task:
            self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _watchdog(self):
        while True:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                now = time.time()
                to_close = []
                for uid, s in list(self.sessions.items()):
                    if s.state == "active" and now - s.last_activity > IDLE_TIMEOUT:
                        to_close.append((uid, "⏰ Session بسته شد (5 دقیقه بی‌فعالیتی)"))
                    elif s.state == "waiting" and now - s.last_activity > WAIT_TIMEOUT:
                        to_close.append((uid, "⏰ Session بسته شد (15 دقیقه انتظار)"))
                for uid, msg in to_close:
                    s = self.sessions.get(uid)
                    if s:
                        try:
                            from keyboards.main_menu import MAIN_MENU
                            await self.bot.send_message(
                                chat_id=s.chat_id, text=msg,
                                parse_mode="HTML", reply_markup=MAIN_MENU,
                            )
                        except Exception:
                            pass
                    await self.close_session(uid)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Watchdog: {e}")

    async def connect(
        self, user_id: int, chat_id: int,
        host: str, port: int, username: str,
        password: Optional[str] = None,
        private_key: Optional[str] = None,
        key_passphrase: Optional[str] = None,
    ) -> tuple:
        if user_id in self.sessions:
            await self.close_session(user_id)

        try:
            kwargs: Dict[str, Any] = {
                "host": host, "port": port, "username": username,
                "known_hosts": None, "connect_timeout": 15,
            }
            if private_key:
                try:
                    key = asyncssh.import_private_key(
                        private_key,
                        passphrase=key_passphrase if key_passphrase else None,
                    )
                    kwargs["client_keys"] = [key]
                except asyncssh.KeyImportError as e:
                    return False, f"❌ خطا در کلید SSH: {e}"
            elif password:
                kwargs["password"] = password
            else:
                return False, "❌ رمز یا کلید لازم است."

            conn = await asyncio.wait_for(asyncssh.connect(**kwargs), timeout=20)
            process = await conn.create_process(
                term_type="xterm-256color", term_size=(220, 50),
            )

            mgr = SessionOutputManager(self.bot, chat_id)

            # لاگ اتصال در DB
            log_id = 0
            try:
                from database.db import log_ssh_connect
                log_id = await log_ssh_connect(user_id, f"{host}:{port}")
            except Exception:
                pass

            session = UserSession(
                user_id=user_id, chat_id=chat_id,
                host=host, port=port, username=username,
                connection=conn, process=process, output_mgr=mgr,
                log_id=log_id,
            )
            session.reader_task = asyncio.create_task(self._reader(session))
            self.sessions[user_id] = session

            return True, f"✅ متصل شدی به <b>{username}@{host}:{port}</b>"

        except asyncio.TimeoutError:
            return False, "❌ Timeout: سرور پاسخ نداد."
        except asyncssh.PermissionDenied:
            return False, "❌ رمز یا کلید اشتباه است."
        except asyncssh.DisconnectError as e:
            return False, f"❌ سرور قطع کرد: {e.reason}"
        except (OSError, asyncssh.Error) as e:
            return False, f"❌ خطای اتصال: {e}"
        except Exception as e:
            logger.exception("Connect error")
            return False, f"❌ خطا: {e}"

    async def _reader(self, session: UserSession):
        try:
            while True:
                try:
                    data = await session.process.stdout.read(4096)
                    if not data:
                        if session.output_mgr:
                            await session.output_mgr.send_system("🔌 <b>سرور اتصال را قطع کرد.</b>")
                        break
                    session.update_sample(data)
                    new_mode = detect_terminal_mode(session._last_sample)
                    if new_mode != session.terminal_mode:
                        session.terminal_mode = new_mode
                        asyncio.create_task(self._send_mode_kb(session, new_mode))
                    if session.state == "active" and session.output_mgr:
                        await session.output_mgr.append(data)
                except asyncssh.ProcessError:
                    break
                except Exception as e:
                    logger.exception(f"Reader: {e}")
                    break
        finally:
            if session.user_id in self.sessions:
                asyncio.create_task(self._auto_close(session.user_id))

    async def _send_mode_kb(self, session: UserSession, mode: str):
        try:
            kb = get_keyboard_for_mode(mode)
            labels = {'nano': '📝 Nano', 'vim': '📝 Vim', 'normal': '🖥 Shell'}
            await self.bot.send_message(
                chat_id=session.chat_id,
                text=f"<i>— {labels.get(mode, mode)} —</i>",
                parse_mode="HTML", reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"Mode kb: {e}")

    async def _auto_close(self, user_id: int):
        await asyncio.sleep(1)
        if user_id in self.sessions:
            await self.close_session(user_id)

    async def send_command(self, user_id: int, command: str) -> bool:
        """دستور متنی - buffer جدید می‌سازد"""
        session = self.sessions.get(user_id)
        if not session or session.state != "active":
            return False
        try:
            if session.output_mgr:
                await session.output_mgr.new_command()
            session.process.stdin.write(command + "\n")
            session.touch()
            return True
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning(f"Write error: {e}")
            await self.close_session(user_id)
            return False

    async def send_raw(self, user_id: int, data: str) -> bool:
        """داده خام بدون newline - برای Ctrl+C و..."""
        session = self.sessions.get(user_id)
        if not session or session.state != "active":
            return False
        try:
            session.process.stdin.write(data)
            session.touch()
            return True
        except (BrokenPipeError, ConnectionError, OSError) as e:
            logger.warning(f"Raw write: {e}")
            await self.close_session(user_id)
            return False

    async def send_command_with_new_buffer(self, user_id: int, command: str) -> bool:
        """shortcut هایی که باید buffer جدید بسازند (pwd, ls -la و...)"""
        session = self.sessions.get(user_id)
        if not session or session.state != "active":
            return False
        try:
            if session.output_mgr:
                await session.output_mgr.new_command()
            session.process.stdin.write(command + "\n")
            session.touch()
            return True
        except (BrokenPipeError, ConnectionError, OSError) as e:
            await self.close_session(user_id)
            return False

    async def put_on_wait(self, user_id: int) -> bool:
        s = self.sessions.get(user_id)
        if not s or s.state != "active":
            return False
        s.state = "waiting"
        s.touch()
        return True

    async def resume(self, user_id: int) -> bool:
        s = self.sessions.get(user_id)
        if not s or s.state != "waiting":
            return False
        s.state = "active"
        s.touch()
        return True

    async def close_session(self, user_id: int) -> bool:
        async with self._lock:
            session = self.sessions.pop(user_id, None)
        if not session:
            return False

        # لاگ قطع اتصال
        if session.log_id:
            try:
                from database.db import log_ssh_disconnect
                await log_ssh_disconnect(session.log_id)
            except Exception:
                pass

        if session.reader_task and not session.reader_task.done():
            session.reader_task.cancel()
            try:
                await session.reader_task
            except (asyncio.CancelledError, Exception):
                pass

        if session.output_mgr:
            try:
                await session.output_mgr.stop()
            except Exception:
                pass

        try:
            if session.process:
                session.process.terminate()
        except Exception:
            pass

        try:
            if session.connection:
                session.connection.close()
                await session.connection.wait_closed()
        except Exception:
            pass

        logger.info(f"Session closed: {user_id}")
        return True

    # ─── SFTP ────────────────────────────────────────────────────

    async def sftp_list(self, user_id: int, path: str = ".") -> tuple:
        s = self.sessions.get(user_id)
        if not s or not s.connection:
            return False, [], ""
        try:
            async with s.connection.start_sftp_client() as sftp:
                real = await sftp.realpath(path)
                entries = await sftp.readdir(path)
                items = []
                for e in sorted(entries, key=lambda x: (
                    not bool(x.attrs.permissions and x.attrs.permissions & 0o40000),
                    x.filename
                )):
                    if e.filename in ('.', '..'):
                        continue
                    is_dir = bool(e.attrs.permissions and (e.attrs.permissions & 0o40000))
                    items.append({'name': e.filename, 'is_dir': is_dir, 'size': e.attrs.size or 0})
            return True, items, real
        except asyncssh.SFTPError as e:
            return False, [], str(e)
        except Exception as e:
            return False, [], str(e)

    async def sftp_upload_to_path(self, user_id: int, file_bytes: bytes,
                                   filename: str, remote_dir: str) -> tuple:
        s = self.sessions.get(user_id)
        if not s or not s.connection:
            return False, "❌ اتصال وجود ندارد."
        try:
            import os as _os
            safe = _os.path.basename(filename) or "file"
            remote_path = remote_dir.rstrip('/') + '/' + safe
            async with s.connection.start_sftp_client() as sftp:
                async with sftp.open(remote_path, 'wb') as f:
                    await f.write(file_bytes)
            s.touch()
            return True, f"✅ <code>{safe}</code> در <code>{remote_dir}</code> آپلود شد."
        except asyncssh.SFTPError as e:
            return False, f"❌ SFTP: {e}"
        except Exception as e:
            return False, f"❌ {e}"

    async def sftp_download(self, user_id: int, remote_path: str) -> tuple:
        """دانلود فایل از سرور"""
        s = self.sessions.get(user_id)
        if not s or not s.connection:
            return False, None, ""
        try:
            async with s.connection.start_sftp_client() as sftp:
                import io
                buf = io.BytesIO()
                async with sftp.open(remote_path, 'rb') as f:
                    data = await f.read()
                import os as _os
                fname = _os.path.basename(remote_path)
                return True, data, fname
        except asyncssh.SFTPError as e:
            return False, None, str(e)
        except Exception as e:
            return False, None, str(e)

    async def sftp_mkdir(self, user_id: int, path: str) -> tuple:
        s = self.sessions.get(user_id)
        if not s or not s.connection:
            return False, "❌ اتصال وجود ندارد."
        try:
            async with s.connection.start_sftp_client() as sftp:
                await sftp.makedirs(path, exist_ok=True)
            return True, f"✅ پوشه <code>{path}</code> ساخته شد."
        except asyncssh.SFTPError as e:
            return False, f"❌ {e}"

    async def sftp_create_file(self, user_id: int, path: str) -> tuple:
        s = self.sessions.get(user_id)
        if not s or not s.connection:
            return False, "❌ اتصال وجود ندارد."
        try:
            async with s.connection.start_sftp_client() as sftp:
                async with sftp.open(path, 'w') as f:
                    await f.write("")
            return True, f"✅ فایل <code>{path}</code> ساخته شد."
        except asyncssh.SFTPError as e:
            return False, f"❌ {e}"

    async def sftp_delete(self, user_id: int, path: str, is_dir: bool) -> tuple:
        s = self.sessions.get(user_id)
        if not s or not s.connection:
            return False, "❌ اتصال وجود ندارد."
        try:
            async with s.connection.start_sftp_client() as sftp:
                if is_dir:
                    await sftp.rmtree(path)
                else:
                    await sftp.remove(path)
            return True, f"✅ <code>{path}</code> حذف شد."
        except asyncssh.SFTPError as e:
            return False, f"❌ {e}"

    async def sftp_rename(self, user_id: int, src: str, dst: str) -> tuple:
        s = self.sessions.get(user_id)
        if not s or not s.connection:
            return False, "❌ اتصال وجود ندارد."
        try:
            async with s.connection.start_sftp_client() as sftp:
                await sftp.rename(src, dst)
            return True, f"✅ منتقل شد به <code>{dst}</code>."
        except asyncssh.SFTPError as e:
            return False, f"❌ {e}"

    async def get_stats(self) -> dict:
        active = sum(1 for s in self.sessions.values() if s.state == "active")
        waiting = sum(1 for s in self.sessions.values() if s.state == "waiting")
        return {"active": active, "waiting": waiting, "total": len(self.sessions)}

    async def shutdown(self):
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        for uid in list(self.sessions.keys()):
            try:
                await self.close_session(uid)
            except Exception:
                pass


_manager: Optional[SSHManager] = None


def get_manager() -> SSHManager:
    global _manager
    if _manager is None:
        raise RuntimeError("SSHManager not initialized.")
    return _manager


def init_manager(bot: Bot) -> SSHManager:
    global _manager
    _manager = SSHManager(bot)
    return _manager
