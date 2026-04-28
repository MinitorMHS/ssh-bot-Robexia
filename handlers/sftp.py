"""
sftp.py - هندلر SFTP

معماری:
- وقتی کاربر وارد SFTP می‌شود، context.user_data["sftp_mode"] = True ست می‌شود
- terminal_handler قبل از پردازش چک می‌کند آیا sftp_mode فعال است
- اگر فعال بود، پیام به sftp_text_handler می‌رود نه به SSH
- هر متن ارسالی در SFTP به عنوان مسیر cd تفسیر می‌شود
"""

import logging
import posixpath
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from handlers.stats import save_user_and_track
from services.ssh_manager import get_manager
from keyboards.main_menu import MAIN_MENU, CANCEL_MENU
from keyboards.terminal_kb import SFTP_MENU

logger = logging.getLogger(__name__)
MAX_FILE_SIZE = 20 * 1024 * 1024

SFTP_HELP = (
    "📂 <b>حالت SFTP</b>\n\n"
    "📌 هر متنی که بفرستی به عنوان <b>مسیر</b> تفسیر می‌شه (مثل cd)\n"
    "مثال: <code>/var/www</code> یا <code>..</code>\n\n"
    "از دکمه‌های زیر استفاده کن:\n"
    "🔄 بروزرسانی — نمایش مجدد مسیر فعلی\n"
    "⬆️ پوشه بالاتر — رفتن به parent\n"
    "📁 تغییر مسیر — وارد کردن مسیر دلخواه\n"
    "🏠 Home — رفتن به /home\n"
    "➕ ساخت پوشه — در مسیر فعلی\n"
    "📄 ساخت فایل — در مسیر فعلی\n"
    "🗑 حذف — فایل یا پوشه\n"
    "✂️ انتقال — جابجایی فایل/پوشه\n"
    "📤 آپلود — ارسال فایل به سرور (تا 20MB)\n"
    "📥 دانلود — دریافت فایل از سرور\n"
    "❌ بستن SFTP — خروج"
)

SSH_CONNECTED_HELP = (
    "✅ <b>متصل شدی!</b>\n\n"
    "⌨️ هر پیامی که بفرستی به عنوان دستور اجرا می‌شه.\n"
    "📎 فایل بفرست → آپلود به سرور (SFTP)\n\n"
    "دکمه‌های کیبورد:\n"
    "⛔ Ctrl+C — توقف دستور جاری\n"
    "🚪 Ctrl+D — خروج از shell\n"
    "↹ Tab — تکمیل خودکار\n"
    "⬆ آخرین دستور — تاریخچه\n\n"
    "⏸ /wait — بک‌گراند (15 دقیقه)\n"
    "❌ /close — قطع اتصال\n\n"
    "⚠️ خروجی ترمینال هر <b>7 ثانیه</b> به‌روز می‌شه."
)


def _fmt_size(n: int) -> str:
    if n < 1024: return f"{n}B"
    if n < 1024**2: return f"{n//1024}KB"
    return f"{n//(1024**2)}MB"


def is_sftp_mode(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("sftp_mode"))


def enter_sftp(context: ContextTypes.DEFAULT_TYPE, path: str = "."):
    context.user_data["sftp_mode"] = True
    context.user_data["sftp_path"] = path
    context.user_data["sftp_state"] = "browse"  # browse|await_mkdir|await_mkfile|await_delete|await_move_src|await_move_dst|await_download


def exit_sftp(context: ContextTypes.DEFAULT_TYPE):
    for k in ("sftp_mode", "sftp_path", "sftp_state", "sftp_move_src"):
        context.user_data.pop(k, None)


async def show_dir(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int, path: str):
    """نمایش محتوای یک مسیر"""
    manager = get_manager()
    ok, items, real_path = await manager.sftp_list(user_id, path)

    if not ok:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ خطا در خواندن مسیر: <code>{real_path}</code>",
            parse_mode="HTML", reply_markup=SFTP_MENU,
        )
        return

    context.user_data["sftp_path"] = real_path
    context.user_data["sftp_state"] = "browse"

    lines = [f"📂 <code>{real_path}</code>\n"]
    dirs = [i for i in items if i['is_dir']]
    files = [i for i in items if not i['is_dir']]

    for d in dirs[:30]:
        lines.append(f"📁 <code>{d['name']}/</code>")
    for f in files[:30]:
        lines.append(f"📄 <code>{f['name']}</code> <i>({_fmt_size(f['size'])})</i>")
    if len(items) > 60:
        lines.append(f"\n<i>... {len(items)-60} مورد دیگر</i>")
    if not items:
        lines.append("<i>(خالی)</i>")

    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode="HTML",
        reply_markup=SFTP_MENU,
    )


async def sftp_entry(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int):
    """ورود به SFTP - از my_hosts یا fast_ssh فراخوانی می‌شود"""
    enter_sftp(context, ".")
    await context.bot.send_message(
        chat_id=chat_id, text=SFTP_HELP,
        parse_mode="HTML", reply_markup=SFTP_MENU,
    )
    await show_dir(context, user_id, chat_id, ".")


async def handle_sftp_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    هندلر اصلی پیام‌های SFTP.
    این تابع از terminal_message_handler صدا زده می‌شود اگر sftp_mode فعال باشد.
    """
    await save_user_and_track(update)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text if update.message else None
    doc = update.message.document if update.message else None

    cur = context.user_data.get("sftp_path", ".")
    state = context.user_data.get("sftp_state", "browse")
    manager = get_manager()

    # ─── دکمه‌های SFTP ──────────────────────────────────────────

    if text == "🔄 بروزرسانی":
        await show_dir(context, user_id, chat_id, cur)
        return

    if text == "⬆️ پوشه بالاتر":
        parent = posixpath.dirname(cur) if cur != "/" else "/"
        await show_dir(context, user_id, chat_id, parent)
        return

    if text == "🏠 Home" or text == "🏠 برگشت به home":
        await show_dir(context, user_id, chat_id, "/root" if True else "~")
        # بهتره مستقیم ~ بدیم
        await show_dir(context, user_id, chat_id, "~")
        return

    if text == "📁 تغییر مسیر":
        context.user_data["sftp_state"] = "await_cd"
        await update.message.reply_html(
            f"📁 مسیر فعلی: <code>{cur}</code>\n\n"
            "مسیر جدید را بفرست:\n\n"
            "💡 <b>نکته:</b> برای ست کردن مسیر حتما از دو اسلش // استفاده کنید: <code>//home</code>",
            reply_markup=CANCEL_MENU,
        )
        return

    if text == "➕ ساخت پوشه":
        context.user_data["sftp_state"] = "await_mkdir"
        await update.message.reply_html(
            f"📁 نام پوشه جدید در <code>{cur}</code>:",
            reply_markup=CANCEL_MENU,
        )
        return

    if text == "📄 ساخت فایل":
        context.user_data["sftp_state"] = "await_mkfile"
        await update.message.reply_html(
            f"📄 نام فایل جدید در <code>{cur}</code>:",
            reply_markup=CANCEL_MENU,
        )
        return

    if text == "🗑 حذف":
        context.user_data["sftp_state"] = "await_delete"
        await update.message.reply_html(
            f"🗑 نام فایل یا پوشه‌ای که می‌خوای حذف کنی در <code>{cur}</code>:",
            reply_markup=CANCEL_MENU,
        )
        return

    if text == "✂️ انتقال/تغییر نام":
        context.user_data["sftp_state"] = "await_move_src"
        await update.message.reply_html(
            f"✂️ نام فایل/پوشه‌ای که می‌خوای انتقال بدی در <code>{cur}</code>:",
            reply_markup=CANCEL_MENU,
        )
        return

    if text == "📤 آپلود فایل":
        context.user_data["sftp_state"] = "await_upload"
        await update.message.reply_html(
            f"📤 فایل رو بفرست (تا 20MB) - در <code>{cur}</code> آپلود می‌شه:",
            reply_markup=CANCEL_MENU,
        )
        return

    if text == "📥 دانلود فایل":
        context.user_data["sftp_state"] = "await_download"
        await update.message.reply_html(
            f"📥 نام فایلی که می‌خوای دانلود کنی از <code>{cur}</code>:",
            reply_markup=CANCEL_MENU,
        )
        return

    if text == "❌ بستن SFTP":
        await manager.close_session(user_id)
        exit_sftp(context)
        await update.message.reply_html("🔌 <b>SFTP بسته شد.</b>", reply_markup=MAIN_MENU)
        return

    if text == "🚫 لغو":
        context.user_data["sftp_state"] = "browse"
        await show_dir(context, user_id, chat_id, cur)
        return

    # ─── دریافت آپلود (فایل) ─────────────────────────────────────
    if doc:
        if state == "await_upload" or state == "browse":
            await _handle_upload(update, context, user_id, chat_id, cur, doc)
        return

    # ─── state های انتظار ────────────────────────────────────────
    if not text:
        return

    if state == "await_cd":
        new_path = text if text.startswith('/') else posixpath.join(cur, text)
        await show_dir(context, user_id, chat_id, new_path)
        return

    if state == "await_mkdir":
        full = posixpath.join(cur, text)
        ok, msg = await manager.sftp_mkdir(user_id, full)
        await update.message.reply_html(msg)
        await show_dir(context, user_id, chat_id, cur)
        return

    if state == "await_mkfile":
        full = posixpath.join(cur, text)
        ok, msg = await manager.sftp_create_file(user_id, full)
        await update.message.reply_html(msg)
        await show_dir(context, user_id, chat_id, cur)
        return

    if state == "await_delete":
        full = posixpath.join(cur, text)
        # تشخیص پوشه/فایل
        _, items, _ = await manager.sftp_list(user_id, cur)
        is_dir = any(i['name'] == text and i['is_dir'] for i in items)
        context.user_data["sftp_pending_delete"] = {"path": full, "is_dir": is_dir, "name": text}
        await update.message.reply_html(
            f"⚠️ مطمئنی می‌خوای <code>{text}</code> رو {'پوشه' if is_dir else 'فایل'} حذف کنی؟",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ بله حذف کن", callback_data="sftp_del:yes"),
                InlineKeyboardButton("❌ انصراف", callback_data="sftp_del:no"),
            ]]),
        )
        return

    if state == "await_move_src":
        context.user_data["sftp_move_src"] = posixpath.join(cur, text)
        context.user_data["sftp_state"] = "await_move_dst"
        await update.message.reply_html(
            f"✂️ مسیر مقصد را بفرست (مطلق یا نسبی):",
            reply_markup=CANCEL_MENU,
        )
        return

    if state == "await_move_dst":
        src = context.user_data.get("sftp_move_src", "")
        dst = text if text.startswith('/') else posixpath.join(cur, text)
        ok, msg = await manager.sftp_rename(user_id, src, dst)
        await update.message.reply_html(msg)
        context.user_data.pop("sftp_move_src", None)
        await show_dir(context, user_id, chat_id, cur)
        return

    if state == "await_download":
        full = posixpath.join(cur, text)
        status = await update.message.reply_html(f"⏳ دانلود <code>{text}</code>...")
        ok, data, fname = await manager.sftp_download(user_id, full)
        if not ok:
            await status.edit_text(f"❌ {fname}", parse_mode="HTML")
            return
        if len(data) > MAX_FILE_SIZE:
            await status.edit_text("❌ فایل بزرگتر از 20MB است.")
            return
        try:
            import io
            await context.bot.send_document(
                chat_id=chat_id,
                document=io.BytesIO(data), filename=fname,
                caption=f"📥 <code>{full}</code>", parse_mode="HTML",
                read_timeout=120, write_timeout=120, connect_timeout
            )
            await status.delete()
        except Exception as e:
            await status.edit_text(f"❌ خطا: {e}")
        context.user_data["sftp_state"] = "browse"
        return

    # ─── هر متن دیگری = cd به اون مسیر ─────────────────────────
    if state == "browse":
        new_path = text if text.startswith('/') else posixpath.join(cur, text)
        await show_dir(context, user_id, chat_id, new_path)
        return


async def _handle_upload(update, context, user_id, chat_id, cur, doc):
    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_html(f"❌ فایل بیش از 20MB است.")
        return
    status = await update.message.reply_html("⏳ دانلود از تلگرام...")
    try:
        f = await doc.get_file()
        ba = await f.download_as_bytearray()
    except Exception as e:
        await status.edit_text(f"❌ {e}")
        return
    await status.edit_text("⏳ آپلود به سرور...")
    manager = get_manager()
    ok, msg = await manager.sftp_upload_to_path(user_id, bytes(ba), doc.file_name or "file", cur)
    await status.edit_text(msg, parse_mode="HTML")
    context.user_data["sftp_state"] = "browse"
    await show_dir(context, user_id, chat_id, cur)


async def sftp_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":")[1]
    uid = query.from_user.id
    chat_id = query.message.chat_id
    cur = context.user_data.get("sftp_path", ".")

    if choice == "yes":
        info = context.user_data.get("sftp_pending_delete", {})
        if info:
            manager = get_manager()
            ok, msg = await manager.sftp_delete(uid, info["path"], info.get("is_dir", False))
            await query.edit_message_text(msg, parse_mode="HTML")
    else:
        await query.edit_message_text("❌ لغو شد.")

    context.user_data.pop("sftp_pending_delete", None)
    context.user_data["sftp_state"] = "browse"
    await show_dir(context, uid, chat_id, cur)
