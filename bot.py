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
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import db
import formatter
from zajel_client import ZajelClient, Course, StudentProfile

from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Zajel Telegram Bot is active and running!")

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

load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USER_ID_STR = os.getenv("ALLOWED_USER_ID", "123456789").strip()
ADMIN_USER_ID = int(ADMIN_USER_ID_STR) if ADMIN_USER_ID_STR.isdigit() else 123456789

# Conversation states for multi-student login
WAITING_USERNAME, WAITING_PASSWORD = range(2)

# Per-user cache: telegram_id -> {client, courses, profile, semester, timestamp}
user_sessions: Dict[int, dict] = {}
CACHE_TTL = 300  # 5 minutes

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("محاضرات اليوم"), KeyboardButton("البرنامج الأسبوعي")],
        [KeyboardButton("بطاقة الطالب"), KeyboardButton("سجل الغيابات")],
        [KeyboardButton("الرسائل الهامة"), KeyboardButton("بوابة مودل Moodle")],
        [KeyboardButton("تحديث البيانات"), KeyboardButton("تسجيل الخروج")]
    ],
    resize_keyboard=True,
    is_persistent=True
)


def get_user_client(telegram_id: int) -> Optional[ZajelClient]:
    user = db.get_user(telegram_id)
    if not user:
        return None
    username, password, _ = user
    if telegram_id not in user_sessions or user_sessions[telegram_id].get("username") != username:
        client = ZajelClient(username, password)
        user_sessions[telegram_id] = {
            "client": client,
            "username": username,
            "courses": None,
            "profile": None,
            "semester": "",
            "timestamp": 0
        }
    return user_sessions[telegram_id]["client"]


async def get_cached_data(telegram_id: int, force_refresh: bool = False):
    client = get_user_client(telegram_id)
    if not client:
        return None, "", None

    session = user_sessions.get(telegram_id, {})
    now = datetime.now().timestamp()

    if not force_refresh and session.get("courses") and (now - session.get("timestamp", 0) < CACHE_TTL):
        return session.get("profile"), session.get("semester", ""), session.get("courses")

    profile = client.get_student_profile()
    sem_name, courses = client.get_schedule()

    if courses:
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
        username, _, name = existing_user
        display_name = name or username
        await update.message.reply_text(
            f"أهلاً بك مجدداً يا {display_name}.\n\n"
            "اختر الخدمة المطلوبة من القائمة أدناه:",
            reply_markup=MAIN_KEYBOARD
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "أهلاً بك في بوت خدمات زاجل - جامعة النجاح الوطنية.\n\n"
        "لتسجيل الدخول لأول مرة، يرجى إرسال رقمك الجامعي:",
        reply_markup=ReplyKeyboardRemove()
    )
    return WAITING_USERNAME


async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or len(text) < 6:
        await update.message.reply_text("يرجى إدخال رقم جامعي صحيح (أرقام فقط):")
        return WAITING_USERNAME

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

    status_msg = await update.message.reply_text("جاري التحقق من الحساب وتسجيل الدخول عبر زاجل...")

    # Test login
    client = ZajelClient(username, password)
    success = client.login()

    if not success:
        await status_msg.edit_text(
            "تعذر تسجيل الدخول. الرقم الجامعي أو كلمة المرور غير صحيحة.\n\n"
            "يرجى الضغط على /start للمحاولة مرة أخرى."
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Fetch profile to store student name
    profile = client.get_student_profile()
    student_name = profile.name if profile else ""

    # Save encrypted credentials in database
    db.save_user(user_id, username, password, student_name)

    # Initialize user session
    user_sessions[user_id] = {
        "client": client,
        "username": username,
        "courses": None,
        "profile": profile,
        "semester": "",
        "timestamp": 0
    }

    welcome_name = f" يا {student_name}" if student_name else ""
    await status_msg.edit_text(
        f"تم تسجيل الدخول بنجاح{welcome_name}.\n\n"
        "يمكنك الآن استخدام القائمة أدناه للوصول إلى كافة خدماتك الأكاديمية:",
    )
    await update.message.reply_text(
        "القائمة الرئيسية:",
        reply_markup=MAIN_KEYBOARD
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
            "أنت لست مسجلاً في البوت حالياً.\n"
            "يرجى إرسال /start لتسجيل الدخول بحساب زاجل.",
            reply_markup=ReplyKeyboardRemove()
        )
        return False
    return True


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        profile, _, courses = await get_cached_data(user_id)
        if not courses:
            await update.message.reply_text("تعذر جلب الجدول من زاجل. يرجى المحاولة لاحقاً.")
            return

        client = get_user_client(user_id)
        student_name = profile.name if profile else "عزيزي الطالب"
        day_name, items = client.get_today_classes(courses)
        text = formatter.format_today_classes(student_name, day_name, items)
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.error(f"Error in today_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب محاضرات اليوم.")


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        profile, sem_name, courses = await get_cached_data(user_id)
        if not courses:
            await update.message.reply_text("تعذر جلب البرنامج الدراسي من زاجل.")
            return

        student_name = profile.name if profile else "عزيزي الطالب"
        text = formatter.format_full_schedule(student_name, sem_name, courses)
        chunks = formatter.split_message(text)
        for chunk in chunks:
            await update.message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.error(f"Error in schedule_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب البرنامج الدراسي.")


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        profile, _, _ = await get_cached_data(user_id)
        if not profile:
            await update.message.reply_text("تعذر جلب بيانات الطالب من زاجل.")
            return

        text = formatter.format_student_profile(profile)
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.error(f"Error in profile_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب بطاقة الطالب.")


async def absences_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        _, _, courses = await get_cached_data(user_id)
        if not courses:
            await update.message.reply_text("تعذر جلب سجل الغيابات من زاجل.")
            return

        text = formatter.format_absences(courses)
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.error(f"Error in absences_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب سجل الغيابات.")


async def messages_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        client = get_user_client(user_id)
        messages = client.get_important_messages()
        text = formatter.format_messages(messages)
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        logger.error(f"Error in messages_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب الرسائل الهامة.")


async def moodle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        client = get_user_client(user_id)
        sso_url = client.get_moodle_sso_url()

        if sso_url:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("الانتقال المباشر إلى مودل", url=sso_url)]
            ])
            await update.message.reply_text(
                "بوابة التعليم الإلكتروني (Moodle)\n\n"
                "تم إنشاء رابط دخول آمن لحسابك. اضغط على الزر أدناه للدخول مباشرة:",
                reply_markup=keyboard
            )
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("فتح موقع مودل", url="https://moodle.najah.edu")]
            ])
            await update.message.reply_text(
                "بوابة التعليم الإلكتروني (Moodle)\n\n"
                "يمكنك فتح مودل من الزر أدناه:",
                reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Error in moodle_command for user {user_id}: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("حدث خطأ أثناء جلب رابط مودل.")


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_user_logged_in(update):
        return

    user_id = update.effective_user.id
    await update.effective_chat.send_action(ChatAction.TYPING)
    msg = await update.message.reply_text("جاري الاتصال بخادم زاجل وتحديث بياناتك...")

    try:
        profile, sem_name, courses = await get_cached_data(user_id, force_refresh=True)
        if courses:
            await msg.edit_text(
                f"تم تحديث البيانات بنجاح.\n"
                f"تم تحميل {len(courses)} مساقات لفصل ({sem_name})."
            )
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
    if user_id != ADMIN_USER_ID:
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
    if user_id != ADMIN_USER_ID:
        return

    target_file = "bot_errors.log" if os.path.exists("bot_errors.log") and os.path.getsize("bot_errors.log") > 0 else "bot.log"
    if os.path.exists(target_file):
        with open(target_file, "rb") as doc:
            await update.message.reply_document(document=doc, filename=target_file)
    else:
        await update.message.reply_text("No log files found.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_USER_ID:
        return

    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    count = conn.cursor().execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()

    await update.message.reply_text(
        f"Bot Status Report:\n"
        f"- Registered Users: {count}\n"
        f"- Active Sessions in Memory: {len(user_sessions)}\n"
        f"- Server Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global unhandled error handler"""
    logger.error(f"Exception while handling an update: {context.error}\n{traceback.format_exc()}")
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("حدث خطأ غير متوقع أثناء معالجة طلبك. تم تسجيل الخطأ وسيقوم المشرف بمراجعته.")
        except Exception:
            pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = formatter.format_help()
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "محاضرات اليوم":
        await today_command(update, context)
    elif text == "البرنامج الأسبوعي":
        await schedule_command(update, context)
    elif text == "بطاقة الطالب":
        await profile_command(update, context)
    elif text == "سجل الغيابات":
        await absences_command(update, context)
    elif text == "الرسائل الهامة":
        await messages_command(update, context)
    elif text == "بوابة مودل Moodle":
        await moodle_command(update, context)
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
    start_health_check_server()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

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
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("absences", absences_command))
    app.add_handler(CommandHandler("messages", messages_command))
    app.add_handler(CommandHandler("moodle", moodle_command))
    app.add_handler(CommandHandler("refresh", refresh_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("help", help_command))

    # Admin Command handlers
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("logfile", logfile_command))
    app.add_handler(CommandHandler("status", status_command))

    # Global Error handler
    app.add_error_handler(error_handler)

    # Text router for keyboard buttons
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    print("Bot is live and polling!")
    app.run_polling()


if __name__ == "__main__":
    main()