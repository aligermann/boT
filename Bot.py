#!/usr/bin/env python3
"""
Telegram File Management Bot - Python version for Railway
Converted from Cloudflare Workers JS source with full feature parity.
Supports colored buttons (Bot API 9.4+): primary / success / danger
"""

import os
import json
import random
import string
import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, List
from functools import partial

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8788775336:AAG1EN9l8bDR6km6M5fYfi7auNuDEkvLoYE")
MAIN_ADMIN = int(os.getenv("MAIN_ADMIN", "7764565509"))
# Use /tmp on free hosts that have read-only filesystem
_default_db = "/tmp/bot_data.db" if os.path.exists("/tmp") and os.access("/tmp", os.W_OK) else "bot_data.db"
DB_PATH = os.getenv("DB_PATH", _default_db)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================== DATABASE (pure sqlite3 - no external deps) ==================
def _get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db_sync():
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                step TEXT DEFAULT 'NONE',
                temp_data TEXT DEFAULT '',
                last_prompt_time INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS admins (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS files (
                uid TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                data TEXT NOT NULL
            );
        """)
        conn.commit()


async def init_db():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_db_sync)


async def get_user(user_id: int) -> Dict[str, Any]:
    def _q():
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT step, temp_data, last_prompt_time FROM users WHERE id = ?",
                (str(user_id),)
            ).fetchone()
            if row:
                return {"step": row[0], "temp_data": row[1], "last_prompt_time": row[2]}
            return {"step": "NONE", "temp_data": "", "last_prompt_time": 0}
    return await asyncio.get_event_loop().run_in_executor(None, _q)


async def set_user(user_id: int, step: Optional[str] = None, temp_data: Optional[str] = None):
    def _q():
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT step, temp_data, last_prompt_time FROM users WHERE id = ?",
                (str(user_id),)
            ).fetchone()
            if row:
                cur_step, cur_temp, _ = row[0], row[1], row[2]
            else:
                cur_step, cur_temp = "NONE", ""
            new_step = step if step is not None else cur_step
            new_temp = temp_data if temp_data is not None else cur_temp
            ts = int(datetime.now().timestamp())
            conn.execute(
                """
                INSERT INTO users (id, step, temp_data, last_prompt_time)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    step = excluded.step,
                    temp_data = excluded.temp_data,
                    last_prompt_time = excluded.last_prompt_time
                """,
                (str(user_id), new_step, new_temp, ts)
            )
            conn.commit()
    await asyncio.get_event_loop().run_in_executor(None, _q)


async def get_all_users_id() -> List[str]:
    def _q():
        with _get_conn() as conn:
            rows = conn.execute("SELECT id FROM users").fetchall()
            return [r[0] for r in rows]
    return await asyncio.get_event_loop().run_in_executor(None, _q)


async def get_setting(key: str) -> Optional[str]:
    def _q():
        with _get_conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None
    return await asyncio.get_event_loop().run_in_executor(None, _q)


async def set_setting(key: str, value: str):
    def _q():
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value)
            )
            conn.commit()
    await asyncio.get_event_loop().run_in_executor(None, _q)


async def delete_setting(key: str):
    def _q():
        with _get_conn() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            conn.commit()
    await asyncio.get_event_loop().run_in_executor(None, _q)


async def get_admin_role(user_id: int) -> Optional[str]:
    if user_id == MAIN_ADMIN:
        return "owner"
    def _q():
        with _get_conn() as conn:
            row = conn.execute("SELECT role FROM admins WHERE id = ?", (str(user_id),)).fetchone()
            return row[0] if row else None
    return await asyncio.get_event_loop().run_in_executor(None, _q)


async def add_admin(user_id: str, role: str):
    def _q():
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO admins (id, role) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET role = excluded.role",
                (str(user_id), role)
            )
            conn.commit()
    await asyncio.get_event_loop().run_in_executor(None, _q)


async def remove_admin(user_id: str):
    def _q():
        with _get_conn() as conn:
            conn.execute("DELETE FROM admins WHERE id = ?", (str(user_id),))
            conn.commit()
    await asyncio.get_event_loop().run_in_executor(None, _q)


async def get_sub_admins_with_roles() -> List[Dict[str, str]]:
    def _q():
        with _get_conn() as conn:
            rows = conn.execute("SELECT id, role FROM admins").fetchall()
            return [{"user_id": r[0], "role": r[1]} for r in rows]
    return await asyncio.get_event_loop().run_in_executor(None, _q)


async def get_channels() -> List[str]:
    def _q():
        with _get_conn() as conn:
            rows = conn.execute("SELECT id FROM channels").fetchall()
            return [r[0] for r in rows]
    return await asyncio.get_event_loop().run_in_executor(None, _q)


async def add_channel(channel_id: str):
    def _q():
        with _get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO channels (id) VALUES (?)", (str(channel_id),))
            conn.commit()
    await asyncio.get_event_loop().run_in_executor(None, _q)


async def remove_channel(channel_id: str):
    def _q():
        with _get_conn() as conn:
            conn.execute("DELETE FROM channels WHERE id = ?", (str(channel_id),))
            conn.commit()
    await asyncio.get_event_loop().run_in_executor(None, _q)


async def save_file_entry(uid: str, ftype: str, data: dict):
    def _q():
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO files (uid, type, data) VALUES (?, ?, ?)",
                (uid, ftype, json.dumps(data, ensure_ascii=False))
            )
            conn.commit()
    await asyncio.get_event_loop().run_in_executor(None, _q)


async def get_file_entry(uid: str) -> Optional[Dict]:
    def _q():
        with _get_conn() as conn:
            row = conn.execute("SELECT type, data FROM files WHERE uid = ?", (uid,)).fetchone()
            if row:
                return {"type": row[0], "data": json.loads(row[1])}
            return None
    return await asyncio.get_event_loop().run_in_executor(None, _q)


async def delete_file_entry(uid: str):
    def _q():
        with _get_conn() as conn:
            conn.execute("DELETE FROM files WHERE uid = ?", (uid,))
            conn.commit()
    await asyncio.get_event_loop().run_in_executor(None, _q)


# ================== HELPERS ==================
def generate_random_string(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def get_file_info(message: Message) -> Optional[Dict]:
    if message.photo:
        last = message.photo[-1]
        return {
            "file_id": last.file_id,
            "file_unique_id": last.file_unique_id,
            "type": "photo",
        }
    if message.video:
        return {
            "file_id": message.video.file_id,
            "file_unique_id": message.video.file_unique_id,
            "type": "video",
        }
    if message.audio:
        return {
            "file_id": message.audio.file_id,
            "file_unique_id": message.audio.file_unique_id,
            "type": "audio",
        }
    if message.document:
        return {
            "file_id": message.document.file_id,
            "file_unique_id": message.document.file_unique_id,
            "type": "document",
        }
    if message.voice:
        return {
            "file_id": message.voice.file_id,
            "file_unique_id": message.voice.file_unique_id,
            "type": "voice",
        }
    return None


def btn(text: str, callback_data: str = None, url: str = None, style: str = None) -> InlineKeyboardButton:
    """Create styled button. style: primary / success / danger (ignored on older clients/libs)"""
    kwargs = {"text": text}
    if callback_data:
        kwargs["callback_data"] = callback_data
    if url:
        kwargs["url"] = url
    if style:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        # Older python-telegram-bot without style support
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


# ================== ADMIN PANEL ==================
async def show_admin_panel(chat_id: int, role: str, message_id: int = None, context: ContextTypes.DEFAULT_TYPE = None):
    buttons = [
        [
            btn("آپلود تکی", "upl_single", style="primary"),
            btn("آپلود چندتایی", "upl_multi", style="primary"),
        ],
        [btn("مدیریت و جستجوی فایل", "man_search", style="primary")],
    ]
    if role in ("full", "owner"):
        buttons.append([
            btn("ارسال پیام همگانی", "broadcast", style="success"),
            btn("مشاهده آمار", "stats", style="primary"),
        ])
        buttons.append([btn("تنظیمات سیستم", "settings", style="primary")])

    text = (
        f"سلام. وقتتون بخیر.\n"
        f"سطح دسترسی شما در حال حاضر: **{role.upper()}**\n\n"
        f"جهت ادامه کار، یکی از گزینه‌های زیر را انتخاب نمایید:"
    )
    markup = InlineKeyboardMarkup(buttons)

    if message_id and context:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup,
            )
        except TelegramError:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup,
            )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
        )


async def ask_for_multi_caption(chat_id: int, target_msg_id: int, file_index: int, context: ContextTypes.DEFAULT_TYPE):
    markup = InlineKeyboardMarkup([
        [btn("ادامه بدون کپشن (این فایل)", "multi_skip_one", style="primary")],
        [btn("برای هیچکدام کپشن نمیخوام", "multi_skip_all", style="danger")],
        [btn("اتمام بارگذاری و دریافت لینک", "multi_finish", style="success")],
    ])
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"لطفاً متن توضیحات (کپشن) را برای این فایل بفرستید:\n\n(وضعیت: در حال بررسی فایل شماره {file_index + 1})",
        reply_to_message_id=target_msg_id,
        reply_markup=markup,
    )


async def process_send_file(chat_id: int, uid: str, context: ContextTypes.DEFAULT_TYPE):
    entry = await get_file_entry(uid)
    if not entry:
        await context.bot.send_message(chat_id=chat_id, text="متأسفانه فایل مورد نظر پیدا نشد.")
        return

    sent_msg_ids = []

    if entry["type"] == "single":
        d = entry["data"]
        file_type = d.get("file_type") or d.get("type")
        method_map = {
            "photo": context.bot.send_photo,
            "video": context.bot.send_video,
            "audio": context.bot.send_audio,
            "document": context.bot.send_document,
            "voice": context.bot.send_voice,
        }
        send_func = method_map.get(file_type, context.bot.send_document)
        kwargs = {"chat_id": chat_id, "caption": d.get("caption", "")}
        kwargs[file_type] = d["file_id"]
        res = await send_func(**kwargs)
        if res and res.message_id:
            sent_msg_ids.append(res.message_id)
    else:
        inf = await context.bot.send_message(
            chat_id=chat_id,
            text="لطفاً چند لحظه صبر کنید، فایل‌ها در حال ارسال هستند..."
        )
        if inf and inf.message_id:
            sent_msg_ids.append(inf.message_id)

        files = entry["data"].get("files", [])
        for f in files:
            method_map = {
                "photo": context.bot.send_photo,
                "video": context.bot.send_video,
                "audio": context.bot.send_audio,
                "document": context.bot.send_document,
                "voice": context.bot.send_voice,
            }
            send_func = method_map.get(f["type"], context.bot.send_document)
            kwargs = {"chat_id": chat_id, "caption": f.get("caption", "")}
            kwargs[f["type"]] = f["file_id"]
            res = await send_func(**kwargs)
            if res and res.message_id:
                sent_msg_ids.append(res.message_id)
            await asyncio.sleep(0.1)

    warn = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "توجه داشته باشید که فایل‌های فرستاده شده تا ۳۰ ثانیه دیگر به دلایل امنیتی و فنی "
            "به صورت خودکار حذف می‌شوند. برای حفظ آن‌ها، لطفاً سریعاً پیام‌ها را در بخش "
            "پیام‌های ذخیره‌شده (Saved Messages) تلگرام خود ذخیره یا فوروارد کنید.\n\n"
            "(اگر فایل‌ها پاک شدند، می‌توانید مجدداً از طریق لینک اقدام کنید.)"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )
    if warn and warn.message_id:
        sent_msg_ids.append(warn.message_id)

    if sent_msg_ids:
        async def delete_later():
            await asyncio.sleep(29)
            try:
                await context.bot.delete_messages(chat_id=chat_id, message_ids=sent_msg_ids)
            except Exception:
                for mid in sent_msg_ids:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)

        asyncio.create_task(delete_later())


# ================== HANDLERS ==================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    chat_id = message.chat_id
    from_id = message.from_user.id
    text = message.text or ""

    await set_user(from_id)  # touch last time
    user_data = await get_user(from_id)
    admin_role = await get_admin_role(from_id)
    is_admin = admin_role is not None

    # /start <uid>
    if text.startswith("/start ") and len(text.split()) > 1:
        uid = text.split(maxsplit=1)[1].strip()
        channels = await get_channels()
        not_joined = False
        keys = []

        for i, chn in enumerate(channels):
            try:
                member = await context.bot.get_chat_member(chat_id=int(chn), user_id=from_id)
                status = member.status if member else ""
            except Exception:
                status = ""
            if status not in ("member", "administrator", "creator"):
                try:
                    chat = await context.bot.get_chat(chat_id=int(chn))
                    link = f"https://t.me/{chat.username}" if chat.username else (chat.invite_link or "")
                    if not link:
                        try:
                            link = await context.bot.export_chat_invite_link(chat_id=int(chn))
                        except Exception:
                            link = "#"
                except Exception:
                    link = "#"
                keys.append([btn(f"ورود به کانال شماره {i + 1}", url=link, style="primary")])
                not_joined = True

        if not_joined:
            bot_info = await context.bot.get_me()
            keys.append([
                btn(
                    "تایید عضویت و دریافت فایل",
                    url=f"https://t.me/{bot_info.username}?start={uid}",
                    style="success",
                )
            ])
            await context.bot.send_message(
                chat_id=chat_id,
                text="برای دریافت فایل درخواستی، ابتدا باید در کانال‌های زیر عضو شوید. پس از عضویت، دکمه تایید را بزنید:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keys),
            )
            return

        react_chan = await get_setting("reaction_channel_link")
        if react_chan:
            current_time = int(datetime.now().timestamp())
            cb_data = f"check_rxn_{uid}_{current_time}"
            msg_text = (
                f"کاربر گرامی، برای نهایی‌سازی دریافت فایل، لطفاً به ۵ تا ۱۰ پست آخر کانال زیر "
                f"واکنش (ریکشن) نشان دهید و سپس روی دکمه بررسی و تایید کلیک کنید.\n\n"
                f"کانال مورد نظر: {react_chan}"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg_text,
                reply_markup=InlineKeyboardMarkup([
                    [btn("بررسی و تایید انجام کار", cb_data, style="success")]
                ]),
            )
            return

        await process_send_file(chat_id, uid, context)
        return

    # Admin panel
    if is_admin and (text in ("/start", "/admin", "/panel") or text == "/start"):
        await set_user(from_id, "NONE", "")
        await show_admin_panel(chat_id, admin_role, context=context)
        return

    if not is_admin:
        await context.bot.send_message(
            chat_id=chat_id,
            text="سلام. وقت بخیر. برای دریافت فایل، لطفاً از روی لینک اختصاصی که در اختیار دارید وارد شوید.",
        )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    chat_id = message.chat_id
    from_id = message.from_user.id
    text = message.text

    await set_user(from_id)
    user_data = await get_user(from_id)
    state = user_data["step"]
    admin_role = await get_admin_role(from_id)
    is_admin = admin_role is not None

    if not is_admin:
        return

    # WAITING_NEW_ADMIN
    if state == "WAITING_NEW_ADMIN" and admin_role == "owner":
        if text and text.isdigit():
            await set_user(from_id, "NONE", text)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"شناسه عددی **{text}** با موفقیت ثبت شد.\n\nلطفاً سطح دسترسی مورد نظر را برای این ادمین جدید انتخاب کنید:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [btn("ادمین آپلودر", f"setrole_uploader_{text}", style="primary")],
                    [btn("ادمین با دسترسی کامل", f"setrole_full_{text}", style="success")],
                    [btn("بازگشت", "settings", style="danger")],
                ]),
            )
            return
        await context.bot.send_message(chat_id=chat_id, text="لطفاً فقط شناسه عددی ارسال کنید.")
        return

    # WAITING_CHANNEL (public)
    if state == "WAITING_CHANNEL" and admin_role in ("owner", "full"):
        if message.forward_from_chat and message.forward_from_chat.type == "channel":
            await add_channel(str(message.forward_from_chat.id))
            await set_user(from_id, "NONE")
            await context.bot.send_message(chat_id=chat_id, text="کانال عمومی با موفقیت به سیستم اضافه شد.")
            await show_admin_panel(chat_id, admin_role, context=context)
            return
        await context.bot.send_message(
            chat_id=chat_id,
            text="لطفاً یک پیام از کانال عمومی مورد نظرتان را به این چت فوروارد کنید.",
        )
        return

    # WAITING_PRIVATE_LINK
    if state == "WAITING_PRIVATE_LINK" and admin_role in ("owner", "full"):
        if text:
            await set_user(from_id, "WAITING_PRIVATE_ID", text)
            await context.bot.send_message(
                chat_id=chat_id,
                text="لینک دعوت دریافت شد. در مرحله بعد، لطفاً شناسه عددی (Channel ID) این کانال خصوصی را فرستاده و تایید کنید:",
            )
            return

    # WAITING_PRIVATE_ID
    if state == "WAITING_PRIVATE_ID" and admin_role in ("owner", "full"):
        if text and text.lstrip("-").isdigit():
            await add_channel(text)
            await set_user(from_id, "NONE")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"کانال خصوصی با شناسه عددی {text} با موفقیت به سیستم اضافه شد.",
            )
            await show_admin_panel(chat_id, admin_role, context=context)
            return
        await context.bot.send_message(
            chat_id=chat_id,
            text="خطا: لطفاً فقط شناسه عددی صحیح و معتبر را ارسال کنید.",
        )
        return

    # WAITING_REACTION_LINK
    if state == "WAITING_REACTION_LINK" and admin_role in ("owner", "full"):
        if text:
            await set_setting("reaction_channel_link", text)
            await set_user(from_id, "NONE")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"لینک کانال واکنش (ریکشن) با موفقیت تنظیم شد:\n{text}",
            )
            await show_admin_panel(chat_id, admin_role, context=context)
            return

    # WAITING_BROADCAST
    if state == "WAITING_BROADCAST" and admin_role in ("owner", "full"):
        all_users = await get_all_users_id()
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"عملیات ارسال پیام به {len(all_users)} کاربر آغاز شد. لطفاً منتظر بمانید...",
        )
        await set_user(from_id, "NONE")

        async def do_broadcast():
            for uid in all_users:
                try:
                    if text:
                        await context.bot.send_message(chat_id=int(uid), text=text)
                    elif message.photo:
                        await context.bot.send_photo(
                            chat_id=int(uid),
                            photo=message.photo[-1].file_id,
                            caption=message.caption or "",
                        )
                    elif message.video:
                        await context.bot.send_video(
                            chat_id=int(uid),
                            video=message.video.file_id,
                            caption=message.caption or "",
                        )
                    elif message.document:
                        await context.bot.send_document(
                            chat_id=int(uid),
                            document=message.document.file_id,
                            caption=message.caption or "",
                        )
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
            try:
                await context.bot.send_message(
                    chat_id=MAIN_ADMIN,
                    text="ارسال همگانی پیام با موفقیت به پایان رسید.",
                )
            except Exception:
                pass

        asyncio.create_task(do_broadcast())
        await show_admin_panel(chat_id, admin_role, context=context)
        return

    # UPLOAD_SINGLE
    if state == "UPLOAD_SINGLE":
        file = get_file_info(message)
        if file:
            await set_user(from_id, "UPLOAD_SINGLE_CAPTION", json.dumps(file))
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "فایل شما دریافت شد.\n\n"
                    "اکنون می‌توانید متن توضیحات (کپشن) مورد نظرتان را بفرستید یا در صورت تمایل، "
                    "دکمه ثبت بدون کپشن را انتخاب کنید:"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [btn("ثبت بدون کپشن", "no_caption", style="primary")],
                    [btn("لغو و بازگشت به پنل", "cancel", style="danger")],
                ]),
            )
            return

    # UPLOAD_SINGLE_CAPTION
    if state == "UPLOAD_SINGLE_CAPTION":
        try:
            temp = json.loads(user_data["temp_data"])
        except Exception:
            temp = {}
        caption = text or message.caption or ""
        uid = generate_random_string(8)
        final_data = {
            "file_id": temp["file_id"],
            "file_unique_id": temp["file_unique_id"],
            "file_type": temp["type"],
            "caption": caption,
        }
        await save_file_entry(uid, "single", final_data)
        await set_user(from_id, "NONE", "")
        bot_info = await context.bot.get_me()
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"فایل با موفقیت در سیستم آپلود شد.\n\nلینک دسترسی:\nhttps://t.me/{bot_info.username}?start={uid}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await show_admin_panel(chat_id, admin_role, context=context)
        return

    # UPLOAD_MULTI / UPLOAD_MULTI_CAPTION
    if state in ("UPLOAD_MULTI", "UPLOAD_MULTI_CAPTION"):
        file = get_file_info(message)
        if file:
            file["message_id"] = message.message_id
            file["caption"] = message.caption or ""
            try:
                temp = json.loads(user_data["temp_data"]) if user_data["temp_data"] else {"index": 0, "files": []}
            except Exception:
                temp = {"index": 0, "files": []}
            temp["files"].append(file)

            if state == "UPLOAD_MULTI":
                await set_user(from_id, "UPLOAD_MULTI_CAPTION", json.dumps(temp))
                await ask_for_multi_caption(chat_id, file["message_id"], temp["index"], context)
                return
            else:
                await set_user(from_id, "UPLOAD_MULTI_CAPTION", json.dumps(temp))
                if temp["index"] == len(temp["files"]) - 1:
                    await ask_for_multi_caption(chat_id, file["message_id"], temp["index"], context)
                return

        elif text and state == "UPLOAD_MULTI_CAPTION":
            try:
                temp = json.loads(user_data["temp_data"]) if user_data["temp_data"] else None
            except Exception:
                temp = None
            if temp and temp["index"] < len(temp["files"]):
                temp["files"][temp["index"]]["caption"] = text
                temp["index"] += 1
                await set_user(from_id, "UPLOAD_MULTI_CAPTION", json.dumps(temp))

                if temp["index"] < len(temp["files"]):
                    await ask_for_multi_caption(
                        chat_id, temp["files"][temp["index"]]["message_id"], temp["index"], context
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="کپشن این فایل ثبت شد. فایل دیگری در صف نمانده. می‌توانید فایل‌های جدید بفرستید یا بارگذاری را تمام کنید:",
                        reply_markup=InlineKeyboardMarkup([
                            [btn("اتمام بارگذاری", "multi_finish", style="success")]
                        ]),
                    )
                return

    # MANAGE_SEARCH
    if state == "MANAGE_SEARCH":
        found_id = text.strip() if text else ""
        entry = await get_file_entry(found_id)
        if entry:
            await set_user(from_id, "NONE", found_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"مشخصات فایل پیدا شده:\n\nشناسه فایل: `{found_id}`\nنوع ذخیره‌سازی: {entry['type']}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [btn("حذف این فایل", f"del_{found_id}", style="danger")],
                    [btn("بازگشت", "home", style="primary")],
                ]),
            )
            return
        await context.bot.send_message(chat_id=chat_id, text="متأسفانه فایلی با این شناسه پیدا نشد.")
        return


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    chat_id = query.message.chat_id
    from_id = query.from_user.id
    message_id = query.message.message_id

    user_data = await get_user(from_id)
    admin_role = await get_admin_role(from_id)

    # Reaction check (must answer with alert before anything else)
    if data.startswith("check_rxn_"):
        parts = data.split("_")
        if len(parts) >= 4:
            file_uid = parts[2]
            timestamp = int(parts[3])
            diff = int(datetime.now().timestamp()) - timestamp
            if diff < 23:
                await query.answer(
                    "لطفاً ابتدا مراحل ذکر شده را با دقت انجام دهید و سپس مجدداً این دکمه را برای تایید فشار دهید.",
                    show_alert=True,
                )
                return
            await query.answer()
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
            await process_send_file(chat_id, file_uid, context)
        return

    if not admin_role:
        await query.answer()
        return

    # home / cancel
    if data in ("home", "cancel"):
        await query.answer()
        await set_user(from_id, "NONE", "")
        await show_admin_panel(chat_id, admin_role, message_id, context)
        return

    # setrole_
    if data.startswith("setrole_") and admin_role == "owner":
        await query.answer()
        parts = data.split("_")
        if len(parts) >= 3:
            role_type = parts[1]
            new_admin_id = parts[2]
            await add_admin(new_admin_id, role_type)
            role_name = "آپلودر" if role_type == "uploader" else "با دسترسی کامل"
            await set_user(from_id, "NONE")
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"ادمین جدید با شناسه عددی ({new_admin_id}) و سطح دسترسی **{role_name}** با موفقیت در سیستم ثبت شد.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [btn("بازگشت به پنل", "home", style="primary")]
                ]),
            )
        return

    # stats
    if data == "stats" and admin_role in ("owner", "full"):
        users = await get_all_users_id()
        channels = await get_channels()
        admins = await get_sub_admins_with_roles()
        # count files
        def _count_files():
            with _get_conn() as conn:
                row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
                return row[0] if row else 0
        files_count = await asyncio.get_event_loop().run_in_executor(None, _count_files)
        react = await get_setting("reaction_channel_link")
        text = (
            f"📊 **آمار سیستم**\n\n"
            f"👤 کاربران ثبت‌شده: **{len(users)}** نفر\n"
            f"📁 فایل‌های ذخیره‌شده: **{files_count}** مورد\n"
            f"📢 کانال‌های عضویت اجباری: **{len(channels)}** کانال\n"
            f"👮 ادمین‌های فرعی: **{len(admins)}** نفر\n"
            f"🔒 قفل واکنش: {'فعال ✅' if react else 'غیرفعال ❌'}"
        )
        await query.answer("آمار به‌روز شد", show_alert=False)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("بروزرسانی آمار", "stats", style="primary")],
                [btn("بازگشت به پنل", "home", style="danger")],
            ]),
        )
        return

    # Answer remaining callbacks so Telegram stops the loading spinner
    try:
        await query.answer()
    except Exception:
        pass

    # upl_single
    if data == "upl_single":
        await set_user(from_id, "UPLOAD_SINGLE")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="بخش آپلود تکی فعال شد.\nلطفاً فایل مد نظر خود را برای ربات بفرستید:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت به پنل", "home", style="danger")]
            ]),
        )
        return

    # no_caption
    if data == "no_caption" and user_data["step"] == "UPLOAD_SINGLE_CAPTION":
        try:
            temp = json.loads(user_data["temp_data"])
        except Exception:
            temp = {}
        uid = generate_random_string(8)
        final_data = {
            "file_id": temp.get("file_id"),
            "file_unique_id": temp.get("file_unique_id"),
            "file_type": temp.get("type"),
            "caption": "",
        }
        await save_file_entry(uid, "single", final_data)
        await set_user(from_id, "NONE", "")
        bot_info = await context.bot.get_me()
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"فایل بدون کپشن با موفقیت ذخیره شد.\n\nلینک اختصاصی شما:\nhttps://t.me/{bot_info.username}?start={uid}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await show_admin_panel(chat_id, admin_role, context=context)
        return

    # upl_multi
    if data == "upl_multi":
        await set_user(from_id, "UPLOAD_MULTI", json.dumps({"index": 0, "files": []}))
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "بخش آپلود گروهی فعال شد.\n"
                "لطفاً فایل‌های خود را بفرستید. سیستم پس از دریافت، دقیقاً روی هر فایل ریپلای زده و از شما متن کپشن را می‌خواهد:"
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("اتمام بارگذاری", "multi_finish", style="success")],
                [btn("بازگشت به پنل", "home", style="danger")],
            ]),
        )
        return

    # multi_skip_one
    if data == "multi_skip_one":
        try:
            temp = json.loads(user_data["temp_data"])
        except Exception:
            temp = None
        if temp and temp["index"] < len(temp["files"]):
            temp["index"] += 1
            await set_user(from_id, "UPLOAD_MULTI_CAPTION", json.dumps(temp))
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
            if temp["index"] < len(temp["files"]):
                await ask_for_multi_caption(
                    chat_id, temp["files"][temp["index"]]["message_id"], temp["index"], context
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="از کپشن این فایل گذشتیم. فایل دیگری در صف نیست. می‌توانید فایل جدید بفرستید یا بارگذاری را تمام کنید:",
                    reply_markup=InlineKeyboardMarkup([
                        [btn("اتمام بارگذاری", "multi_finish", style="success")]
                    ]),
                )
        return

    # multi_skip_all
    if data == "multi_skip_all":
        try:
            temp = json.loads(user_data["temp_data"])
        except Exception:
            temp = {"files": []}
        temp["index"] = len(temp.get("files", []))
        await set_user(from_id, "UPLOAD_MULTI_CAPTION", json.dumps(temp))
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text="کپشن‌دهی برای تمامی فایل‌های صف لغو شد. جهت دریافت لینک آلبوم روی دکمه زیر کلیک کنید:",
            reply_markup=InlineKeyboardMarkup([
                [btn("اتمام بارگذاری", "multi_finish", style="success")]
            ]),
        )
        return

    # multi_finish
    if data == "multi_finish":
        try:
            temp = json.loads(user_data["temp_data"]) if user_data["temp_data"] else {"files": []}
        except Exception:
            temp = {"files": []}
        batch = temp.get("files", [])
        if not batch:
            try:
                await context.bot.send_message(chat_id=chat_id, text="⚠️ هیچ فایلی برای ذخیره‌سازی فرستاده نشده است.")
            except Exception:
                pass
            return
        uid = generate_random_string(8)
        await save_file_entry(uid, "batch", {"files": batch})
        await set_user(from_id, "NONE", "")
        bot_info = await context.bot.get_me()
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"آلبوم فایل‌ها با موفقیت ایجاد و ذخیره شد.\n\nلینک اختصاصی دریافت:\nhttps://t.me/{bot_info.username}?start={uid}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت به پنل", "home", style="primary")]
            ]),
        )
        return

    # man_search
    if data == "man_search":
        await set_user(from_id, "MANAGE_SEARCH")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="جهت جستجو یا حذف، لطفاً شناسه فایل مورد نظر را ارسال کنید:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت به پنل", "home", style="danger")]
            ]),
        )
        return

    # del_
    if data.startswith("del_") and not any(x in data for x in ("react", "chan", "admin")):
        id_to_delete = data[4:]
        await delete_file_entry(id_to_delete)
        # already answered above
        await set_user(from_id, "NONE")
        await show_admin_panel(chat_id, admin_role, message_id, context)
        return

    # broadcast
    if data == "broadcast" and admin_role in ("owner", "full"):
        await set_user(from_id, "WAITING_BROADCAST")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="بخش ارسال همگانی فعال شد.\nلطفاً پیام خود را بفرستید تا برای همه کاربران ارسال شود:",
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت", "home", style="danger")]
            ]),
        )
        return

    # settings
    if data == "settings" and admin_role in ("owner", "full"):
        keys = []
        if admin_role == "owner":
            keys.append([
                btn("افزودن ادمین جدید", "add_admin", style="success"),
                btn("حذف ادمین", "rem_admin", style="danger"),
            ])
        keys.append([btn("مدیریت قفل واکنش", "reaction_lock", style="primary")])
        keys.append([btn("مدیریت کانال‌های عضویت اجباری", "chans", style="primary")])
        keys.append([btn("بازگشت", "home", style="danger")])
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="تنظیمات پیشرفته سیستم:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keys),
        )
        return

    # add_admin
    if data == "add_admin" and admin_role == "owner":
        await set_user(from_id, "WAITING_NEW_ADMIN")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="لطفاً شناسه عددی (User ID) کاربر مورد نظر برای ارتقا به مدیریت را ارسال کنید:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت", "settings", style="danger")]
            ]),
        )
        return

    # rem_admin
    if data == "rem_admin" and admin_role == "owner":
        admins = await get_sub_admins_with_roles()
        keys = []
        for adm in admins:
            if adm["user_id"] != str(MAIN_ADMIN):
                keys.append([
                    btn(f"حذف: {adm['user_id']} ({adm['role']})", f"deladmin_{adm['user_id']}", style="danger")
                ])
        keys.append([btn("بازگشت", "settings", style="primary")])
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="لیست ادمین‌ها. برای حذف هر ادمین، روی گزینه مربوط به آن کلیک کنید:",
            reply_markup=InlineKeyboardMarkup(keys),
        )
        return

    # deladmin_
    if data.startswith("deladmin_") and admin_role == "owner":
        target = data[9:]
        await remove_admin(target)
        # already answered above
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="تغییرات با موفقیت اعمال شد و دسترسی کاربر لغو گردید.",
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت", "settings", style="primary")]
            ]),
        )
        return

    # chans
    if data == "chans" and admin_role in ("owner", "full"):
        channels = await get_channels()
        keys = [[btn(f"حذف کانال: {chn}", f"delchan_{chn}", style="danger")] for chn in channels]
        keys.append([btn("افزودن کانال جدید", "add_chan_wait", style="success")])
        keys.append([btn("بازگشت", "settings", style="primary")])
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="لیست کانال‌های متصل به سیستم عضویت اجباری:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keys),
        )
        return

    # add_chan_wait
    if data == "add_chan_wait":
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="لطفاً نوع کانالی که می‌خواهید اضافه کنید را مشخص نمایید:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("کانال عمومی (روش فوروارد)", "mode_chan_public", style="primary")],
                [btn("کانال خصوصی (ثبت دستی)", "mode_chan_private", style="primary")],
                [btn("بازگشت", "settings", style="danger")],
            ]),
        )
        return

    # mode_chan_public
    if data == "mode_chan_public":
        await set_user(from_id, "WAITING_CHANNEL")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "لطفاً یک پیام از کانال عمومی مد نظرتان را به این صفحه فوروارد کنید.\n\n"
                "توجه: برای بررسی وضعیت عضویت کاربران، حتماً ربات را از قبل در آن کانال به عنوان مدیر اضافه کنید."
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت", "settings", style="danger")]
            ]),
        )
        return

    # mode_chan_private
    if data == "mode_chan_private":
        await set_user(from_id, "WAITING_PRIVATE_LINK")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "لطفاً ابتدا لینک دعوت کانال خصوصی را بفرستید.\n\n"
                "توجه: حتماً ربات باید در آن کانال دسترسی مدیریت داشته باشد تا فرآیند کار به درستی انجام شود."
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت", "settings", style="danger")]
            ]),
        )
        return

    # delchan_
    if data.startswith("delchan_"):
        await remove_channel(data[8:])
        # already answered above
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="لیست کانال‌ها با موفقیت به‌روزرسانی شد.",
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت", "chans", style="primary")]
            ]),
        )
        return

    # reaction_lock
    if data == "reaction_lock" and admin_role in ("owner", "full"):
        curr = await get_setting("reaction_channel_link")
        txt = f"تنظیمات قفل واکنش (ریکشن):\n\n{f'لینک فعلی کانال ریکشن: {curr}' if curr else 'در حال حاضر هیچ لینکی ثبت نشده است.'}"
        keys = [[btn("ثبت لینک جدید", "set_react_link", style="success")]]
        if curr:
            keys.append([btn("حذف لینک واکنش", "del_react_link", style="danger")])
        keys.append([btn("بازگشت", "settings", style="primary")])
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=txt,
            reply_markup=InlineKeyboardMarkup(keys),
        )
        return

    # set_react_link
    if data == "set_react_link":
        await set_user(from_id, "WAITING_REACTION_LINK")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="لطفاً لینک کانال مد نظرتان را برای قفل واکنش (ریکشن) ارسال کنید:",
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت", "settings", style="danger")]
            ]),
        )
        return

    # del_react_link
    if data == "del_react_link":
        await delete_setting("reaction_channel_link")
        # already answered above
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="تنظیمات ذخیره شد و لینک واکنش برداشته شد.",
            reply_markup=InlineKeyboardMarkup([
                [btn("بازگشت", "settings", style="primary")]
            ]),
        )
        return


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mcm = update.my_chat_member
    if not mcm:
        return
    new_status = mcm.new_chat_member.status
    chat_id = mcm.chat.id
    chat_title = mcm.chat.title or "Channel"

    if new_status in ("left", "kicked"):
        channels = await get_channels()
        if str(chat_id) in channels:
            await remove_channel(str(chat_id))
            try:
                await context.bot.send_message(
                    chat_id=MAIN_ADMIN,
                    text=(
                        f"گزارش سیستم:\nربات از کانال **{chat_title}** با آیدی عددی `{chat_id}` خارج یا اخراج شد.\n"
                        f"به همین دلیل، این کانال از لیست عضویت اجباری برداشته شد."
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    print(f"[ERROR] {context.error}", flush=True)


def main():
    print("=" * 50, flush=True)
    print("Telegram File Bot starting...", flush=True)
    print(f"Token starts with: {BOT_TOKEN[:15]}...", flush=True)
    print(f"Main Admin ID: {MAIN_ADMIN}", flush=True)
    print("=" * 50, flush=True)

    try:
        asyncio.run(init_db())
        print("[OK] Database initialized successfully", flush=True)
    except Exception as e:
        print(f"[FATAL] Database init failed: {e}", flush=True)
        raise

    async def post_init(app: Application):
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
            me = await app.bot.get_me()
            print(f"[OK] Connected as @{me.username} (id={me.id})", flush=True)
            print("[OK] Webhook cleared. Polling is active.", flush=True)
            # Notify main admin that bot is online
            try:
                await app.bot.send_message(
                    chat_id=MAIN_ADMIN,
                    text=f"✅ ربات با موفقیت آنلاین شد.\n\n@{me.username}\nآیدی: `{me.id}`",
                    parse_mode="Markdown"
                )
            except Exception as notify_err:
                print(f"[WARN] Could not notify admin: {notify_err}", flush=True)
        except Exception as e:
            print(f"[WARN] post_init: {e}", flush=True)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", start_command))
    application.add_handler(CommandHandler("panel", start_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_error_handler(error_handler)

    print("[OK] Handlers registered. Starting polling...", flush=True)
    logger.info("Bot starting with polling...")

    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    except Exception as e:
        print(f"[FATAL] Polling failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
