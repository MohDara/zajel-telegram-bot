import asyncio
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import traceback
from datetime import datetime
from typing import Dict, Optional, Tuple, List

# Ensure UTF-8 console output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.error import RetryAfter
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import db
import formatter
from zajel_client import ZajelClient, Course, StudentProfile, Transcript, get_local_now

from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Zajel Telegram Bot is active and running!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_health_check_server():
    port_str = os.getenv("PORT", "").strip()
    if not port_str:
        return
    try:
        port = int(port_str)
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"Cloud health check HTTP server listening on port {port}")
    except Exception as e:
        logger.warning(f"Could not start health check server on port {port_str}: {e}")


async def periodic_backup_loop():
    """Continuously creates verified database snapshots so data can always be restored."""
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        try:
            path = await asyncio.to_thread(db.backup_sqlite_db)
            if path:
                logger.info(f"Periodic verified backup created: {path}")
        except Exception as e:
            logger.error(f"Periodic backup failed: {e}")
        try:
            prune_user_sessions()
        except Exception:
            pass


async def post_init(application):
    start_health_check_server()
    application.create_task(periodic_backup_loop())


async def post_shutdown(application):
    """Final safety snapshot on graceful shutdown/rolling deploy."""
    try:
        path = await asyncio.to_thread(db.backup_sqlite_db)
        if path:
            logger.info(f"Shutdown backup created: {path}")
    except Exception as e:
        logger.error(f"Shutdown backup failed: {e}")

# Configure rotating file loggers
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT))
root_logger.addHandler(console_handler)

# General log file (max 2MB, up to 3 backups)
general_file_handler = RotatingFileHandler("bot.log", maxBytes=2*1024*1024, backupCount=3, encoding="utf-8")
general_file_handler.setLevel(logging.INFO)
general_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
root_logger.addHandler(general_file_handler)

# Dedicated error log file
error_file_handler = RotatingFileHandler("bot_errors.log", maxBytes=2*1024*1024, backupCount=3, encoding="utf-8")
error_file_handler.setLevel(logging.ERROR)
error_file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
root_logger.addHandler(error_file_handler)

logger = logging.getLogger("ZajelBot")

# Suppress HTTP request logging to prevent bot token exposure in log files
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USER_ID_STR = os.getenv("ALLOWED_USER_ID", "").strip()
ADMIN_USER_ID = int(ADMIN_USER_ID_STR) if ADMIN_USER_ID_STR.isdigit() else 0

# Conversation states for multi-student login
WAITING_USERNAME, WAITING_PASSWORD = range(2)

# Per-user cache: telegram_id -> {client, courses, profile, semester, timestamp}
user_sessions: Dict[int, dict] = {}
CACHE_TTL = 300  # 5 minutes
SESSION_MAX_IDLE = 24 * 60 * 60  # evict idle in-memory sessions after 24 hours
BACKUP_INTERVAL = 6 * 60 * 60    # automatic verified backup every 6 hours

# Anti-flooding rate limiting: user_id -> timestamp of last request
user_last_request_time: Dict[int, float] = {}
user_last_refresh_time: Dict[int, float] = {}
REQUEST_COOLDOWN = 1.5   # seconds between normal requests
REFRESH_COOLDOWN = 20.0  # seconds between manual /refresh requests


def prune_user_sessions():
    """Frees in-memory sessions that have been idle, keeping memory bounded."""
    now = datetime.now().timestamp()
    stale = [
        uid for uid, s in user_sessions.items()
        if now - s.get("last_used", s.get("timestamp", 0)) > SESSION_MAX_IDLE
    ]
    for uid in stale:
        user_sessions.pop(uid, None)
    return len(stale)

def check_rate_limit(user_id: int, is_refresh: bool = False) -> Tuple[bool, str]:
    """Protects bot and university server from aggressive flooding."""
    now = datetime.now().timestamp()
    if is_refresh:
        last_refresh = user_last_refresh_time.get(user_id, 0)
        if now - last_refresh < REFRESH_COOLDOWN:
            remaining = int(REFRESH_COOLDOWN - (now - last_refresh))
            return True, f"يرجى الانتظار {remaining} ثانية قبل طلب تحديث البيانات مرة أخرى لتجنب ضغط سيرفر الجامعة."
        user_last_refresh_time[user_id] = now
        user_last_request_time[user_id] = now
        return False, ""

    last_req = user_last_request_time.get(user_id, 0)
    if now - last_req < REQUEST_COOLDOWN:
        return True, "يرجى الانتظار لحظة قبل إرسال طلب جديد."
    user_last_request_time[user_id] = now
    return False, ""


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("محاضرات اليوم"), KeyboardButton("البرنامج الأسبوعي")],
        [KeyboardButton("كشف العلامات"), KeyboardButton("سجل الغيابات")],
        [KeyboardButton("الرسائل الهامة"), KeyboardButton("الواجبات والمشاريع 📝")],
        [KeyboardButton("بوابة مودل Moodle"), KeyboardButton("تحديث البيانات")],
        [KeyboardButton("تسجيل الخروج")]
    ],
    resize_keyboard=True,
    is_persistent=True
)


def get_main_keyboard(user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    """Returns main menu keyboard for all students."""
    return MAIN_KEYBOARD


def get_user_client(telegram_id: int) -> Optional[ZajelClient]:
    user = db.get_user(telegram_id)
    if not user:
        return None
    username, password, _ = user
    now = datetime.now().timestamp()
    if telegram_id not in user_sessions or user_sessions[telegram_id].get("username") != username:
        client = ZajelClient(username, password)
        user_sessions[telegram_id] = {
            "client": client,
            "username": username,
            "courses": None,
            "profile": None,
            "semester": "",
            "timestamp": 0,
            "last_used": now
        }
    else:
        user_sessions[telegram_id]["last_used"] = now
    return user_sessions[telegram_id]["client"]


async def get_cached_data(telegram_id: int, force_refresh: bool = False):
    """
    Fetches cached schedule and profile or requests them from Zajel in a background thread
    so the asyncio event loop is never blocked.
    """
    client = get_user_client(telegram_id)
    if not client:
        return None, "", None

    session = user_sessions.get(telegram_id, {})
    now = datetime.now().timestamp()

    if not force_refresh and (session.get("courses") is not None) and (now - session.get("timestamp", 0) < CACHE_TTL):
        return session.get("profile"), session.get("semester", ""), session.get("courses")

    # Run blocking HTTP scraping in thread pool
    profile = await asyncio.to_thread(client.get_student_profile)
    sem_name, courses = await asyncio.to_thread(client.get_schedule)

    if courses is not None:
        session["courses"] = courses
        session["semester"] = sem_name
        session["profile"] = profile
        session["timestamp"] = now
        if profile and profile.name:
            db.update_student_name(telegram_id, profile.name)

    return profile, sem_name, courses


# --- START & LOGIN CONVERSATION ---

async def start_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user_id = update.effective_user.id
    existing_user = db.get_user(user_id)

    if existing_user:
        await update.message.reply_text(
            "أهلاً بك مجدداً في بوت زاجل الجامعي.\n\n"
            "اختر الخدمة المطلوبة من القائمة أدناه:",
            reply_markup=get_main_keyboard(user_id)
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "أهلاً بك في بوت زاجل الجامعي.\n\n"
        "تمت ترقية النظام إلى سيرفرات أسرع، وإضافة ميزة كشف العلامات وسجل الدرجات.\n"
        "قريباً: ميزة العلامات اليومية فور رصدها.\n\n"
        "لتسجيل الدخول، يرجى إرسال رقمك الجامعي:",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAITING_USERNAME


async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or len(text) < 6:
        await update.message.reply_text("يرجى إدخال رقم جامعي صحيح (أرقام فقط):")
        return WAITING_USERNAME

    user_id = update.effective_user.id
    if db.is_username_registered(text, exclude_telegram_id=user_id):
        await update.message.reply_text(
            "هذا الرقم الجامعي مسجل بالفعل لدى حساب آخر في البوت.\n"
            "لأسباب أمنية وتجنباً لتكرار الحسابات، لا يمكن ربط نفس الرقم الجامعي بأكثر من حساب تيليجرام واحد.\n\n"
            "إذا كنت صاحب هذا الحساب وتواجه مشكلة، يرجى التواصل مع مسؤول البوت."
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["login_username"] = text
    await update.message.reply_text(
        f"تم استلام الرقم الجامعي: {text}\n"
        "الآن يرجى إرسال كلمة المرور الخاصة بحسابك على زاجل:"
    )
    return WAITING_PASSWORD


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text.strip()
    chat_id = update.effective_chat.id
    msg_id = update.message.message_id
    user_id = update.effective_user.id
    username = context.user_data.get("login_username", "")

    # Immediately delete the message containing the password for student privacy
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass

    limited, limit_msg = check_rate_limit(user_id)
    if limited:
        await update.message.reply_text(limit_msg)
        return WAITING_PASSWORD

    status_msg = await update.message.reply_text("جاري التحقق من الحساب وتسجيل الدخول عبر زاجل...")

    # Test login in thread pool to prevent blocking the event loop
    client = ZajelClient(username, password)
    success = await asyncio.to_thread(client.login)

    if not success:
        await status_msg.edit_text(
            "تعذر تسجيل الدخول. الرقم الجامعي أو كلمة المرور غير صحيحة.\n\n"
            "يرجى الضغط على /start للمحاولة مرة أخرى."
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Fetch profile in thread pool
    profile = await asyncio.to_thread(client.get_student_profile)
    student_name = profile.name if profile else ""

    # Save encrypted credentials in database before creating a session
    try:
        db.save_user(user_id, username, password, student_name)
    except Exception as e:
        err = f"{type(e).__name__}: {e}".lower()
        logger.error(f"Failed to save user {user_id}: {e}\n{traceback.format_exc()}")
        if "unique" in err or "integrity" in err or "duplicate" in err:
            await status_msg.edit_text(
                "هذا الرقم الجامعي مسجل بالفعل لدى حساب آخر في البوت.\n"
                "لأسباب أمنية وتجنباً لتكرار الحسابات، لا يمكن ربط نفس الرقم الجامعي بأكثر من حساب تيليجرام واحد.\n\n"
                "إذا كنت صاحب هذا الحساب وتواجه مشكلة، يرجى التواصل مع مسؤول البوت."
            )
        else:
            await status_msg.edit_text(
                "تعذر حفظ بياناتك في قاعدة البيانات. لم يتم تسجيل الدخول، يرجى المحاولة لاحقاً."
            )
        context.user_data.clear()
        return ConversationHandler.END

    # Initialize user session
    user_sessions[user_id] = {
        "client": client,
        "username": username,
        "courses": None,
        "profile": profile,
        "semester": "",
        "timestamp": 0,
        "last_used": datetime.now().timestamp()
    }

    welcome_name = f" يا {student_name}" if student_name else ""
    await status_msg.edit_text(
        f"تم تسجيل الدخول بنجاح{welcome_name}.\n\n"
        "يمكنك الآن استخدام القائمة أدناه للوصول إلى كافة خدماتك الأكاديمية:",
    )
    await update.message.reply_text(
        "القائمة الرئيسية:",
        reply_markup=get_main_keyboard(user_id)
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("تم إلغاء عملية تسجيل الدخول. يمكنك الضغط على /start في أي وقت.")
    return ConversationHandler.END


# --- FEATURE HANDLERS ---

async def check_user_logged_in(update: Update) -> bool:
    user_id = update.effective_user.id
    if not db.get_user(user_id):
        await update.message.reply_text(
            "أهلاً بك في بوت زاجل الجامعي.\n\n"
            "تمت ترقية النظام إلى سيرفرات أسرع وإضافة ميزة كشف العلامات، وقريباً ميزة العلامات اليومية.\n\n"
            "يرجى الضغط على /start لتسجيل الدخول بحساب زاجل.",
            reply_markup=ReplyKeyboardRemove()
        )
        return False
    return True


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    limited, limit_msg = check_rate_limit(user_id)
    if limited:
        await update.message.reply_text(limit_msg)
        return

    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        profile, _, courses = await get_cached_data(user_id)
        if courses is None:
            await update.message.reply_text("تعذر جلب الجدول من زاجل. يرجى المحاولة لاحقاً.")
            return

        if len(courses) == 0:
            await update.message.reply_text("لا توجد مساقات مسجلة حالياً لهذا الفصل.", reply_markup=get_main_keyboard(user_id))
            return

        client = get_user_client(user_id)
        student_name = profile.name if profile else "عزيزي الطالب"
        day_name, items = await asyncio.to_thread(client.get_today_classes, courses)
        text = formatter.format_today_classes(student_name, day_name, items)
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id))
    except Exception as e:
        logger.error(f"Error in today_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب محاضرات اليوم.")


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    limited, limit_msg = check_rate_limit(user_id)
    if limited:
        await update.message.reply_text(limit_msg)
        return

    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        profile, sem_name, courses = await get_cached_data(user_id)
        if courses is None:
            await update.message.reply_text("تعذر جلب البرنامج الدراسي من زاجل.")
            return

        if len(courses) == 0:
            await update.message.reply_text(f"لا توجد مساقات مسجلة حالياً لفصل ({sem_name}).", reply_markup=get_main_keyboard(user_id))
            return

        student_name = profile.name if profile else "عزيزي الطالب"
        text = formatter.format_full_schedule(student_name, sem_name, courses)
        chunks = formatter.split_message(text)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=get_main_keyboard(user_id))
    except Exception as e:
        logger.error(f"Error in schedule_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب البرنامج الدراسي.")


async def grades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    limited, limit_msg = check_rate_limit(user_id)
    if limited:
        await update.message.reply_text(limit_msg)
        return

    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        session = user_sessions.get(user_id, {})
        now = datetime.now().timestamp()
        transcript = session.get("transcript")

        if not transcript or (now - session.get("transcript_time", 0) > CACHE_TTL):
            client = get_user_client(user_id)
            if not client:
                await update.message.reply_text("تعذر الاتصال بخادم زاجل.")
                return
            transcript = await asyncio.to_thread(client.get_transcript)
            if transcript:
                session["transcript"] = transcript
                session["transcript_time"] = now
                if transcript.student_name:
                    db.update_student_name(user_id, transcript.student_name)

        if not transcript:
            await update.message.reply_text("تعذر جلب كشف العلامات من زاجل. يرجى المحاولة لاحقاً.")
            return

        text = formatter.format_transcript(transcript)
        chunks = formatter.split_message(text)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=get_main_keyboard(user_id))
    except Exception as e:
        logger.error(f"Error in grades_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب كشف العلامات.")


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await grades_command(update, context)


async def absences_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    limited, limit_msg = check_rate_limit(user_id)
    if limited:
        await update.message.reply_text(limit_msg)
        return

    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        _, _, courses = await get_cached_data(user_id)
        if courses is None:
            await update.message.reply_text("تعذر جلب سجل الغيابات من زاجل.")
            return

        if len(courses) == 0:
            await update.message.reply_text("لا توجد مساقات مسجلة حالياً لعرض الغيابات.", reply_markup=get_main_keyboard(user_id))
            return

        text = formatter.format_absences(courses)
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id))
    except Exception as e:
        logger.error(f"Error in absences_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب سجل الغيابات.")


async def messages_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    limited, limit_msg = check_rate_limit(user_id)
    if limited:
        await update.message.reply_text(limit_msg)
        return

    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        client = get_user_client(user_id)
        messages = await asyncio.to_thread(client.get_important_messages)
        text = formatter.format_messages(messages)
        await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id))
    except Exception as e:
        logger.error(f"Error in messages_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب الرسائل الهامة.")


async def moodle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    limited, limit_msg = check_rate_limit(user_id)
    if limited:
        await update.message.reply_text(limit_msg)
        return

    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        client = get_user_client(user_id)
        sso_url = await asyncio.to_thread(client.get_moodle_sso_url)

        buttons = []
        if sso_url:
            buttons.append([InlineKeyboardButton("الانتقال المباشر إلى مودل (SSO)", url=sso_url)])
        else:
            buttons.append([InlineKeyboardButton("فتح موقع مودل", url="https://moodle.najah.edu")])

        buttons.append([InlineKeyboardButton("عرض الواجبات والمشاريع المستحقة 📝", callback_data="view_activities")])

        keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(
            "بوابة التعليم الإلكتروني (Moodle)\n\n"
            "يمكنك الدخول إلى مودل أو استعراض الواجبات عبر الخيارات أدناه:",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error in moodle_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب رابط مودل.")


async def activities_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches upcoming due activities (assignments, quizzes) from Moodle, ignoring lectures."""
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    limited, limit_msg = check_rate_limit(user_id)
    if limited:
        if update.effective_message:
            await update.effective_message.reply_text(limit_msg)
        return

    await update.effective_chat.send_action(ChatAction.TYPING)
    status_msg = await update.effective_message.reply_text("جاري جلب الواجبات والمشاريع المستحقة من مودل...")

    try:
        client = get_user_client(user_id)
        if not client:
            await status_msg.edit_text("تعذر الاتصال بحسابك في زاجل ومودل.")
            return

        session = user_sessions.get(user_id, {})
        now = datetime.now().timestamp()

        # Check in-memory cache (TTL = 300s)
        if session.get("activities") is not None and (now - session.get("activities_time", 0) < CACHE_TTL):
            activities = session["activities"]
        else:
            activities = await asyncio.to_thread(client.get_upcoming_activities)
            session["activities"] = activities
            session["activities_time"] = now

        profile = session.get("profile")
        if not profile:
            profile = await asyncio.to_thread(client.get_student_profile)
            session["profile"] = profile

        student_name = profile.name if profile and profile.name else "عزيزي الطالب"
        text = formatter.format_upcoming_activities(student_name, activities)

        sso_url = await asyncio.to_thread(client.get_moodle_sso_url)

        # Build inline buttons: SSO quick login + actionable item links
        inline_buttons = []
        if sso_url and sso_url.startswith(("http://", "https://")):
            inline_buttons.append([InlineKeyboardButton("🔑 تسجيل الدخول السريع لمودل (SSO)", url=sso_url)])

        for act in activities[:5]:
            btn_url = act.action_url or act.url
            if btn_url and btn_url.startswith(("http://", "https://")):
                short_title = act.name[:28] + ("..." if len(act.name) > 28 else "")
                inline_buttons.append([InlineKeyboardButton(f"تسليم: {short_title}", url=btn_url)])

        reply_markup = InlineKeyboardMarkup(inline_buttons) if inline_buttons else None

        chunks = formatter.split_message(text)
        await status_msg.delete()
        for idx, chunk in enumerate(chunks):
            kb = reply_markup if idx == len(chunks) - 1 else None
            await update.effective_message.reply_text(chunk, reply_markup=kb)

    except Exception as e:
        logger.error(f"Error in activities_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await status_msg.edit_text("حدث خطأ أثناء جلب الواجبات والمشاريع من مودل. يرجى المحاولة لاحقاً.")


async def activities_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "view_activities":
        await activities_command(update, context)


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    limited, limit_msg = check_rate_limit(user_id, is_refresh=True)
    if limited:
        await update.message.reply_text(limit_msg)
        return

    await update.effective_chat.send_action(ChatAction.TYPING)
    msg = await update.message.reply_text("جاري الاتصال بخادم زاجل وتحديث بياناتك...")

    try:
        if user_id in user_sessions:
            user_sessions[user_id]["courses"] = None
            user_sessions[user_id]["transcript"] = None
            user_sessions[user_id]["activities"] = None
            user_sessions[user_id]["timestamp"] = 0
            user_sessions[user_id]["transcript_time"] = 0
            user_sessions[user_id]["activities_time"] = 0

        profile, sem_name, courses = await get_cached_data(user_id, force_refresh=True)
        if courses is not None:
            if courses:
                await msg.edit_text(
                    f"تم تحديث البيانات بنجاح.\n"
                    f"تم تحميل {len(courses)} مساقات لفصل ({sem_name}).\n"
                    "تم تجديد كشف العلامات والغيابات مباشرة من خادم زاجل."
                )
            else:
                await msg.edit_text(
                    f"تم تحديث البيانات بنجاح.\n"
                    f"لا توجد مساقات مسجلة لفصل ({sem_name}).\n"
                    "تم تجديد كشف العلامات والغيابات مباشرة من خادم زاجل."
                )
            await update.effective_message.reply_text("تم تجديد القائمة الرئيسية:", reply_markup=get_main_keyboard(user_id))
        else:
            await msg.edit_text("تعذر تحديث البيانات من زاجل حالياً. يرجى المحاولة لاحقاً.")
    except Exception as e:
        logger.error(f"Error in refresh_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await msg.edit_text("حدث خطأ أثناء التحديث.")


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    deleted = db.delete_user(user_id)
    if user_id in user_sessions:
        del user_sessions[user_id]

    if deleted:
        await update.message.reply_text(
            "تم تسجيل الخروج وحذف بياناتك من البوت بنجاح.\n\n"
            "لتسجيل الدخول مرة أخرى في أي وقت، أرسل /start.",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await update.message.reply_text(
            "أنت لست مسجلاً في البوت حالياً.",
            reply_markup=ReplyKeyboardRemove()
        )


# --- ADMIN COMMANDS ---

async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_USER_ID == 0 or user_id != ADMIN_USER_ID:
        return

    if os.path.exists("bot_errors.log") and os.path.getsize("bot_errors.log") > 0:
        with open("bot_errors.log", "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            recent_errors = "".join(lines[-25:])
        await update.message.reply_text(f"Recent Errors:\n\n{recent_errors[-3800:]}")
    else:
        await update.message.reply_text("No errors recorded in bot_errors.log.")


async def logfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_USER_ID == 0 or user_id != ADMIN_USER_ID:
        return

    target_file = "bot_errors.log" if os.path.exists("bot_errors.log") and os.path.getsize("bot_errors.log") > 0 else "bot.log"
    if os.path.exists(target_file):
        with open(target_file, "rb") as doc:
            await update.message.reply_document(document=doc, filename=target_file)
    else:
        await update.message.reply_text("No log files found.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_USER_ID == 0 or user_id != ADMIN_USER_ID:
        return

    count = db.get_user_count()
    db_engine = "PostgreSQL (Cloud Database)" if db.DATABASE_URL else "SQLite (WAL Mode & Auto-Backup)"

    await update.message.reply_text(
        f"Bot Status Report (Aggregate Only):\n"
        f"- Registered Students: {count}\n"
        f"- Active Sessions in Memory: {len(user_sessions)}\n"
        f"- Storage Engine: {db_engine}\n"
        f"- Server Time: {get_local_now().strftime('%Y-%m-%d %H:%M:%S')} (Palestine Time)\n"
        f"- Privacy: Zero-Knowledge (No student names or personal records stored)"
    )


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_USER_ID == 0 or user_id != ADMIN_USER_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "يرجى كتابة نص الرسالة بعد الأمر.\n"
            "مثال:\n"
            "/broadcast تنبيه: تم الإعلان عن جدول الامتحانات النصفية"
        )
        return

    broadcast_text = " ".join(context.args).strip()
    all_ids = db.get_all_telegram_ids()

    if not all_ids:
        await update.message.reply_text("لا يوجد مستخدمون لإرسال الإشعار إليهم.")
        return

    status_msg = await update.message.reply_text(f"جاري إرسال الإشعار إلى {len(all_ids)} طالب...")

    success_count = 0
    fail_count = 0
    announcement_msg = f"إشعار عام من إدارة البوت:\n\n{broadcast_text}"

    for tid in all_ids:
        try:
            await context.bot.send_message(chat_id=tid, text=announcement_msg)
            success_count += 1
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1.0)
            try:
                await context.bot.send_message(chat_id=tid, text=announcement_msg)
                success_count += 1
            except Exception as retry_err:
                logger.warning(f"Failed to send broadcast to {tid} after retry: {retry_err}")
                fail_count += 1
        except Exception as e:
            logger.warning(f"Failed to send broadcast to {tid}: {e}")
            fail_count += 1
        # Throttle to stay well below Telegram flood limits
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"تقرير الإرسال الجماعي:\n\n"
        f"- إجمالي المستلمين: {len(all_ids)}\n"
        f"- تم التسليم بنجاح: {success_count}\n"
        f"- فشل الإرسال (حظر أو خطأ): {fail_count}"
    )


async def clear_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_USER_ID == 0 or user_id != ADMIN_USER_ID:
        return

    count = len(user_sessions)
    user_sessions.clear()
    await update.message.reply_text(
        f"تم تفريغ الذاكرة المؤقتة بنجاح ({count} جلسات).\n"
        "سيتم جلب كافة البيانات مباشرة من خادم زاجل في الطلبات القادمة."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global unhandled error handler"""
    err_str = str(context.error) if context.error else ""
    if "Conflict" in err_str and "getUpdates" in err_str:
        # Standard zero-downtime rolling deploy handover on cloud container platforms
        logger.info("Temporary polling handover during zero-downtime container deploy.")
        return

    logger.error(f"Exception while handling an update: {context.error}\n{traceback.format_exc()}")
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("حدث خطأ غير متوقع أثناء معالجة طلبك. تم تسجيل الخطأ وسيقوم المشرف بمراجعته.")
        except Exception:
            pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = formatter.format_help()
    if ADMIN_USER_ID > 0 and user_id == ADMIN_USER_ID:
        admin_tools = (
            "\n\nأوامر المشرف الخاصة (إحصائية فقط):\n"
            "/status - تقرير حالة الخادم والعدد الإجمالي للطلاب\n"
            "/broadcast <رسالة> - إرسال إشعار جماعي لكافة الطلاب\n"
            "/clear_cache - تفريغ الذاكرة المؤقتة\n"
            "/logs - عرض آخر الأخطاء المسجلة\n"
            "/logfile - تحميل ملف السجل كملف مستند"
        )
        text += admin_tools
    await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id))


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "محاضرات اليوم":
        await today_command(update, context)
    elif text == "البرنامج الأسبوعي":
        await schedule_command(update, context)
    elif text in ["كشف العلامات", "بطاقة الطالب"]:
        await grades_command(update, context)
    elif text == "سجل الغيابات":
        await absences_command(update, context)
    elif text == "الرسائل الهامة":
        await messages_command(update, context)
    elif text == "بوابة مودل Moodle":
        await moodle_command(update, context)
    elif text in ["الواجبات والمشاريع 📝", "الواجبات والمشاريع", "الواجبات والأنشطة", "الواجبات والأنشطة 📝", "الواجبات", "الواجبات القادمة", "المشاريع والواجبات"]:
        await activities_command(update, context)
    elif text == "تحديث البيانات":
        await refresh_command(update, context)
    elif text == "تسجيل الخروج":
        await logout_command(update, context)
    else:
        await help_command(update, context)


def main():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("TELEGRAM_BOT_TOKEN is missing in .env!")
        sys.exit(1)

    print("Starting Multi-User Zajel Telegram Bot...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    # Login conversation handler
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_entry)],
        states={
            WAITING_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username)],
            WAITING_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_login), CommandHandler("start", start_entry)],
        allow_reentry=True
    )
    app.add_handler(login_conv)

    # User Command handlers
    app.add_handler(CommandHandler("today", today_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("grades", grades_command))
    app.add_handler(CommandHandler("transcript", grades_command))
    app.add_handler(CommandHandler("profile", grades_command))
    app.add_handler(CommandHandler("absences", absences_command))
    app.add_handler(CommandHandler("messages", messages_command))
    app.add_handler(CommandHandler("moodle", moodle_command))
    app.add_handler(CommandHandler("activities", activities_command))
    app.add_handler(CommandHandler("assignments", activities_command))
    app.add_handler(CommandHandler("due", activities_command))
    app.add_handler(CommandHandler("projects", activities_command))
    app.add_handler(CommandHandler("tasks", activities_command))
    app.add_handler(CommandHandler("homework", activities_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("help", help_command))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(activities_callback, pattern="^view_activities$"))

    # Admin Command handlers (Aggregate telemetry and broadcast only)
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("logfile", logfile_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("clear_cache", clear_cache_command))

    # Global Error handler
    app.add_error_handler(error_handler)

    # Text router for keyboard buttons
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    print("Bot is live and polling!")
    app.run_polling()


if __name__ == "__main__":
    main()