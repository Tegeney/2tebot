import os
import re
import json
import logging
import asyncio
import sqlite3
from io import BytesIO
from base64 import b64decode
from cachetools import TTLCache
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    ConversationHandler,
    CallbackQueryHandler,
)
import aiohttp

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Load sensitive data from environment variables
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8014071686:AAGPQzkfEr3it1VxPzBy-2m5htPgUDw4n8E")
ZYTE_API_KEY = os.getenv("ZYTE_API_KEY", "10d1991606c540669fc91202a70ba7e0")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@amharictutorialclass")
ADMIN_IDS = {723559736}  # Replace with your Telegram user ID(s)

# API Base URLs for different regions
REGION_BASE_URLS = {
    "sw": "https://sw.ministry.et/student-result",
    "aa": "https://aa.ministry.et/student-result",
    "amhara": "https://amhara.ministry.et/student-result",
    "oromia": "https://oromia.ministry.et/student-result",
}

# Zyte Proxy URL
ZYTE_PROXY_URL = "https://api.zyte.com/v1/extract"

# Conversation states
LANGUAGE, REGION, REGISTRATION, FIRST_NAME, FEEDBACK = range(5)

# Cache with 1-hour expiration
student_cache = TTLCache(maxsize=100, ttl=3600)

# SQLite database for subscribers, feedback, and usage logs
def init_db():
    conn = sqlite3.connect("bot_data.db")
    conn.execute("CREATE TABLE IF NOT EXISTS subscribers (user_id INTEGER PRIMARY KEY)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            timestamp TEXT,
            replied INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            timestamp TEXT
        )
    """)
    conn.close()

def load_subscribers():
    conn = sqlite3.connect("bot_data.db")
    cursor = conn.execute("SELECT user_id FROM subscribers")
    subscribers = {row[0] for row in cursor.fetchall()}
    conn.close()
    return subscribers

def add_subscriber(user_id):
    conn = sqlite3.connect("bot_data.db")
    conn.execute("INSERT OR IGNORE INTO subscribers (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def save_feedback(user_id, message):
    conn = sqlite3.connect("bot_data.db")
    conn.execute(
        "INSERT INTO feedback (user_id, message, timestamp) VALUES (?, ?, ?)",
        (user_id, message, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

subscribed_users = set()

# Fetch student data asynchronously
async def fetch_student_data(region: str, registration: str, first_name: str) -> dict:
    cache_key = (region, registration, first_name)
    if cache_key in student_cache:
        return student_cache[cache_key]

    base_url = REGION_BASE_URLS.get(region)
    if not base_url:
        logger.error(f"Invalid region: {region}")
        return None

    url = f"{base_url}/{registration}?first_name={first_name}&qr="
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                ZYTE_PROXY_URL,
                auth=aiohttp.BasicAuth(ZYTE_API_KEY, ""),
                json={"url": url, "httpResponseBody": True, "geolocation": "ET"},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response.raise_for_status()
                data = await response.json()
                http_response_body = b64decode(data["httpResponseBody"])
                result = json.loads(http_response_body.decode("utf-8"))
                student_cache[cache_key] = result
                return result
        except Exception as e:
            logger.error(f"Error fetching student data: {e}")
            return None

# Fetch student photo asynchronously
async def fetch_student_photo(photo_url: str) -> BytesIO:
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                ZYTE_PROXY_URL,
                auth=aiohttp.BasicAuth(ZYTE_API_KEY, ""),
                json={"url": photo_url, "httpResponseBody": True, "geolocation": "ET"},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response.raise_for_status()
                data = await response.json()
                image_bytes = b64decode(data["httpResponseBody"])
                return BytesIO(image_bytes)
        except Exception as e:
            logger.error(f"Error fetching photo via proxy: {e}")
            return None

# Calculate result statistics
def calculate_result_stats(student_data: dict) -> str:
    courses = student_data.get("courses", [])
    total_courses = len(courses)
    scores = [float(course.get('score', 0)) for course in courses if 'score' in course and course['score'].isdigit()]
    avg_score = sum(scores) / len(scores) if scores else 0
    passed = len([course for course in courses if course.get('status', '').lower() == 'pass']) if courses and 'status' in courses[0] else total_courses

    stats = (
        f"📊 <b>Result Statistics</b>\n"
        f"📚 Total Courses: {total_courses}\n"
    )
    if scores:
        stats += f"📈 Average Score: {avg_score:.2f}\n"
    if courses and 'status' in courses[0]:
        stats += f"✅ Passed: {passed}\n🚫 Failed: {total_courses - passed}"
    else:
        stats += "ℹ️ Pass/Fail status not available"
    
    return stats

# Check if user is a member of the channel
async def is_user_member(update: Update, context: CallbackContext) -> bool:
    try:
        chat_member = await context.bot.get_chat_member(chat_id=CHANNEL_ID, user_id=update.effective_user.id)
        return chat_member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Error checking channel membership: {e}")
        return False

# Notify admins
async def notify_admins(context: CallbackContext, message: str):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=message, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

# Fetch and send results with photo and statistics
async def fetch_results(update: Update, context: CallbackContext) -> None:
    user_data = context.user_data
    if 'message_ids' not in user_data:
        user_data['message_ids'] = []
    
    region = user_data.get('region', '').strip()
    registration = user_data.get('registration', '').strip()
    first_name = user_data.get('first_name', '').strip().lower()

    if not region or not registration or not first_name:
        error_msg = await update.message.reply_text("❌ Missing required information")
        user_data['message_ids'].append(error_msg.message_id)
        return

    conn = sqlite3.connect("bot_data.db")
    conn.execute(
        "INSERT INTO usage_logs (user_id, action, timestamp) VALUES (?, ?, ?)",
        (update.effective_user.id, "result_lookup", datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    loading_message = await update.message.reply_text("⬜⬜⬜⬜ (0%)")
    user_data['message_ids'].append(loading_message.message_id)

    student_data = await fetch_student_data(region, registration, first_name)
    if not student_data:
        await loading_message.edit_text("🔴 No data found. Please check your details and try again.")
        return

    await loading_message.edit_text("🟩⬜⬜⬜ (25%)")

    student = student_data.get("student", {})
    courses = student_data.get("courses", [])
    
    message = (
        f"🎓 <b>Student Result</b>\n\n"
        f"👤 <b>Name:</b> {student.get('name', 'N/A')}\n"
        f"🎂 <b>Age:</b> {student.get('age', 'N/A')}\n"
        f"🏫 <b>School:</b> {student.get('school', 'N/A')}\n"
        f"📍 <b>Woreda:</b> {student.get('woreda', 'N/A')}\n"
        f"🚻 <b>Gender:</b> {student.get('gender', 'N/A')}\n"
        f"📚 <b>Courses:</b>\n"
    )
    for course in courses:
        message += f"📖 • <b>{course.get('name', 'N/A')}</b>\n"

    await loading_message.edit_text("🟩🟩⬜⬜ (50%)")

    photo_bytes = None
    if 'photo' in student and student['photo']:
        photo_url = student['photo'].replace("\\", "")
        photo_bytes = await fetch_student_photo(photo_url)

    await loading_message.edit_text("🟩🟩🟩⬜ (75%)")

    if photo_bytes:
        photo_message = await update.message.reply_photo(
            photo=photo_bytes,
            caption=message,
            parse_mode='HTML'
        )
        user_data['message_ids'].append(photo_message.message_id)
    else:
        result_message = await update.message.reply_text(
            message + "\n📷 <i>Photo unavailable</i>",
            parse_mode='HTML'
        )
        user_data['message_ids'].append(result_message.message_id)

    stats_message_text = calculate_result_stats(student_data)
    stats_message = await update.message.reply_text(stats_message_text, parse_mode='HTML')
    user_data['message_ids'].append(stats_message.message_id)

    await loading_message.edit_text("🟩🟩🟩🟩 (100%)")
    await loading_message.edit_text("✅ Request completed!")
    menu_message = await update.message.reply_text(
        "🎓 Here are your results and stats above. What would you like to do next?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_menu")],
            [InlineKeyboardButton("🔔 Subscribe for Updates", callback_data="subscribe")],
            [InlineKeyboardButton("📤 Share Result", switch_inline_query=message)],
        ]),
    )
    user_data['message_ids'].append(menu_message.message_id)

# Input validation
def validate_registration(registration: str) -> bool:
    return re.match(r"^\d{6,10}$", registration) is not None

def validate_first_name(first_name: str) -> bool:
    return re.match(r"^[A-Za-z\s-]+$", first_name) is not None

# Keyboards
def language_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English 🇬🇧", callback_data="language_en")],
        [InlineKeyboardButton("Amharic 🇪🇹", callback_data="language_am")],
    ])

def region_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Addis Ababa", callback_data="region_aa")],
        [InlineKeyboardButton("Amhara", callback_data="region_amhara")],
        [InlineKeyboardButton("Oromia", callback_data="region_oromia")],
        [InlineKeyboardButton("South West", callback_data="region_sw")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_language")],
    ])

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌟 Ethiopian Student Results 🌟", callback_data="noop")],
        [InlineKeyboardButton("📚 Check Result", callback_data="check_result"),
         InlineKeyboardButton("ℹ️ About Bot", callback_data="about")],
        [InlineKeyboardButton("🌐 Switch Language", callback_data="change_to_amharic"),
         InlineKeyboardButton("📝 Send Feedback", callback_data="feedback")],
        [InlineKeyboardButton("❤️ Credits", callback_data="creator")],
    ])

def main_menu_keyboard_amharic():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌟 የኢትዮጵያ ተማሪ ውጤት ማሳያማሳያ🌟", callback_data="noop")],
        [InlineKeyboardButton("📚 ውጤት ለማየት", callback_data="check_result_amharic"),
         InlineKeyboardButton("ℹ️ ስለ ቦቱ", callback_data="about_amharic")],
        [InlineKeyboardButton("🌐 ቋንቋ ለመቀየር", callback_data="change_to_english"),
         InlineKeyboardButton("📝 አስተያየት ለመላክ", callback_data="feedback_amharic")],
        [InlineKeyboardButton("❤️ ተገኔ", callback_data="creator_amharic")],
    ])

# Handlers
async def start(update: Update, context: CallbackContext) -> int:
    if not await is_user_member(update, context):
        error_msg = await update.message.reply_text(
            f"🚫 You must join our channel to use this bot.\n\nPlease join {CHANNEL_ID} and try again."
        )
        context.user_data['message_ids'] = [update.message.message_id, error_msg.message_id]
        return ConversationHandler.END

    user_id = update.effective_user.id
    username = update.effective_user.username
    if username:
        profile_link = f"<a href='https://t.me/{username}'>@{username}</a>"
        notification = (
            f"🆕 <b>New User Joined</b>\n"
            f"👤 <b>ID:</b> {user_id}\n"
            f"🔗 <b>Profile:</b> {profile_link}"
        )
    else:
        notification = (
            f"🆕 <b>New User Joined</b>\n"
            f"👤 <b>ID:</b> {user_id}\n"
            f"🔗 <b>Profile:</b> No username set"
        )
    await notify_admins(context, notification)

    context.user_data['message_ids'] = [update.message.message_id]
    lang_msg = await update.message.reply_text(
        "🌍 Please choose your language:\n\n🌍 እባክዎ ቋንቋዎን ይምረጡ:",
        reply_markup=language_menu_keyboard()
    )
    context.user_data['message_ids'].append(lang_msg.message_id)
    return LANGUAGE

async def select_language(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    user_data = context.user_data
    if 'message_ids' not in user_data:
        user_data['message_ids'] = []

    language_map = {"language_en": "en", "language_am": "am"}
    language_key = query.data
    if language_key in language_map:
        user_data['language'] = language_map[language_key]
        text = (
            "🎓 Welcome to the Ethiopian Student Results Bot!\n\nPlease select your region:"
            if language_map[language_key] == "en" else
            "🎓 እንኳን ወደ ኢትዮጵያ የተማሪ ውጤት ቦት በደህና መጡ!\n\nእባክዎ ክልልዎን ይምረጡ:"
        )
        await query.edit_message_text(text, reply_markup=region_menu_keyboard())
        return REGION
    await query.edit_message_text("❌ Invalid language selection. Please try again.")
    return LANGUAGE

async def select_region(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    user_data = context.user_data
    if 'message_ids' not in user_data:
        user_data['message_ids'] = []

    region_map = {"region_aa": "aa", "region_amhara": "amhara", "region_oromia": "oromia", "region_sw": "sw"}
    if query.data == "back_to_language":
        await query.edit_message_text(
            "🌍 Please choose your language:\n\n🌍 እባክዎ ቋንቋዎን ይምረጡ:",
            reply_markup=language_menu_keyboard()
        )
        return LANGUAGE
    region_key = query.data
    if region_key in region_map:
        user_data['region'] = region_map[region_key]
        text = "Please provide your registration number:" if user_data.get('language') == "en" else "እባክዎ የምዝገባ ቁጥርዎን ያስገቡ:"
        await query.edit_message_text(text)
        return REGISTRATION
    await query.edit_message_text("❌ Invalid region selection. Please try again.", reply_markup=region_menu_keyboard())
    return REGION

async def get_registration(update: Update, context: CallbackContext) -> int:
    user_data = context.user_data
    if 'message_ids' not in user_data:
        user_data['message_ids'] = []
    user_data['message_ids'].append(update.message.message_id)

    registration = update.message.text
    if not validate_registration(registration):
        text = "❌ Invalid registration number. Try again." if user_data.get('language') == "en" else "❌ የማያገለግል የምዝገባ ቁጥር። እባክዎ ደግመው ይሞክሩ።"
        error_msg = await update.message.reply_text(text)
        user_data['message_ids'].append(error_msg.message_id)
        return REGISTRATION
    user_data['registration'] = registration
    text = "📝 Now please enter your first name:" if user_data.get('language') == "en" else "📝 እባክዎ የእርስዎን የመጀመሪያ ስም ያስገቡ:"
    prompt_msg = await update.message.reply_text(text)
    user_data['message_ids'].append(prompt_msg.message_id)
    return FIRST_NAME

async def get_first_name(update: Update, context: CallbackContext) -> int:
    user_data = context.user_data
    if 'message_ids' not in user_data:
        user_data['message_ids'] = []
    user_data['message_ids'].append(update.message.message_id)

    first_name = update.message.text
    if not validate_first_name(first_name):
        text = "❌ Invalid first name. Try again." if user_data.get('language') == "en" else "❌ የማያገለግል የመጀመሪያ ስም። እባክዎ ደግመው ይሞክሩ።"
        error_msg = await update.message.reply_text(text)
        user_data['message_ids'].append(error_msg.message_id)
        return FIRST_NAME
    user_data['first_name'] = first_name
    await fetch_results(update, context)
    return ConversationHandler.END

async def feedback_start(update: Update, context: CallbackContext) -> int:
    user_data = context.user_data
    if 'message_ids' not in user_data:
        user_data['message_ids'] = []

    query = update.callback_query
    if query:
        await query.answer()
        lang = user_data.get('language', 'en')
        text = "📝 Please type your feedback:" if lang == "en" else "📝 እባክዎ አስተያየትዎን ይፃፉ:"
        await query.edit_message_text(text)
    else:
        lang = user_data.get('language', 'en')
        text = "📝 Please type your feedback:" if lang == "en" else "📝 እባክዎ አስተያየትዎን ይፃፉ:"
        prompt_msg = await update.message.reply_text(text)
        user_data['message_ids'].append(update.message.message_id)
        user_data['message_ids'].append(prompt_msg.message_id)
    return FEEDBACK

async def receive_feedback(update: Update, context: CallbackContext) -> int:
    user_data = context.user_data
    if 'message_ids' not in user_data:
        user_data['message_ids'] = []
    user_data['message_ids'].append(update.message.message_id)

    user_id = update.effective_user.id
    username = update.effective_user.username or "No username"
    feedback_text = update.message.text.strip()

    if not feedback_text:
        error_msg = await update.message.reply_text(
            "❌ Feedback cannot be empty. Please try again."
            if user_data.get('language', 'en') == "en"
            else "❌ አስተያየት ባዶ መሆን አይችልም። እባክዎ ደግመው ይሞክሩ።"
        )
        user_data['message_ids'].append(error_msg.message_id)
        return FEEDBACK

    try:
        save_feedback(user_id, feedback_text)
        lang = user_data.get('language', 'en')
        success_msg = await update.message.reply_text(
            "✅ Thank you for your feedback!" if lang == "en" else "✅ ለአስተያየትዎ እናመሰግናለን!",
            reply_markup=main_menu_keyboard() if lang == "en" else main_menu_keyboard_amharic()
        )
        user_data['message_ids'].append(success_msg.message_id)
        await notify_admins(
            context,
            f"📬 <b>New Feedback</b>\n👤 <b>ID:</b> {user_id}\n🔗 <b>Username:</b> @{username}\n📝 <b>Message:</b> {feedback_text}"
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error saving feedback: {e}")
        error_msg = await update.message.reply_text(
            "❌ An error occurred while submitting your feedback. Please try again later."
            if user_data.get('language', 'en') == "en"
            else "❌ አስተያየትዎን በማስገባት ላይ ስህተት ተከስቷል። እባክዎ ቆይተው ይሞክሩ።"
        )
        user_data['message_ids'].append(error_msg.message_id)
        return FEEDBACK

async def check_result_start(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    user_data = context.user_data
    if 'message_ids' not in user_data:
        user_data['message_ids'] = []
    
    lang = user_data.get('language', 'en')
    text = "Please select your region:" if lang == "en" else "እባክዎ ክልልዎን ይምረጡ:"
    await query.edit_message_text(text, reply_markup=region_menu_keyboard())
    return REGION

async def button_handler(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    user_data = context.user_data
    if 'message_ids' not in user_data:
        user_data['message_ids'] = []
    lang = user_data.get('language', 'en')
    chat_id = update.effective_chat.id

    if query.data == "noop":
        return ConversationHandler.END
    elif query.data in ["check_result", "check_result_amharic"]:
        # This is now handled by check_result_start in ConversationHandler
        return await check_result_start(update, context)
    elif query.data == "change_to_amharic":
        user_data['language'] = "am"
        await query.edit_message_text(
            "🌟 እንኳን ደህና መጡ! እባክዎ አማራጭ ይምረጡ:",
            reply_markup=main_menu_keyboard_amharic()
        )
    elif query.data == "change_to_english":
        user_data['language'] = "en"
        await query.edit_message_text(
            "🌟 Welcome back! Please choose an option:",
            reply_markup=main_menu_keyboard()
        )
    elif query.data in ["about", "about_amharic"]:
        text = "ℹ️ About the bot\n\nThis bot helps Ethiopian students check their results." if lang == "en" else "ℹ️ ስለ ቦቱ\n\nይህ ቦት ለኢትዮጵያውያን ተማሪዎች ውጤታቸውን ለማየት ይረዳቸዋል።"
        await query.edit_message_text(text, reply_markup=main_menu_keyboard() if lang == "en" else main_menu_keyboard_amharic())
    elif query.data in ["creator", "creator_amharic"]:
        text = "❤️ Created by t.me/Tegene" if lang == "en" else "❤️ በ t.me/Tegene የተሰራ"
        await query.edit_message_text(text, reply_markup=main_menu_keyboard() if lang == "en" else main_menu_keyboard_amharic())
    elif query.data in ["feedback", "feedback_amharic"]:
        return await feedback_start(update, context)
    elif query.data == "back_to_menu":
        if 'message_ids' in user_data:
            for msg_id in user_data['message_ids']:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception as e:
                    logger.error(f"Error deleting message {msg_id}: {e}")
        
        user_data.clear()
        text = "🌟 Welcome back! Please choose an option:"
        new_menu_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=main_menu_keyboard() if lang == "en" else main_menu_keyboard_amharic()
        )
        user_data['message_ids'] = [new_menu_msg.message_id]
    elif query.data == "subscribe":
        user_id = update.effective_user.id
        add_subscriber(user_id)
        subscribed_users.add(user_id)
        text = "🔔 You are now subscribed to receive updates when your final marks are released!" if lang == "en" else "🔔 የመጨረሻ ክፍል ውጤቶች ሲለቀቁ ለማሳወቅ ተመዝግበዋል!"
        await query.edit_message_text(text)
    return ConversationHandler.END

async def broadcast(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("ℹ️ Usage: /broadcast <message>")
        return
    message = " ".join(context.args)
    for user_id in subscribed_users:
        try:
            await context.bot.send_message(chat_id=user_id, text=f"📢 Update: {message}")
        except Exception as e:
            logger.error(f"Failed to send message to {user_id}: {e}")
    await update.message.reply_text(f"✅ Broadcast sent to {len(subscribed_users)} users.")

async def reply_to_feedback(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("ℹ️ Usage: /reply <feedback_id> <message>")
        return
    try:
        feedback_id = int(context.args[0])
        reply_message = " ".join(context.args[1:])
        
        conn = sqlite3.connect("bot_data.db")
        cursor = conn.execute("SELECT user_id FROM feedback WHERE id = ? AND replied = 0", (feedback_id,))
        result = cursor.fetchone()
        if result:
            user_id = result[0]
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📩 Admin reply to your feedback:\n\n{reply_message}"
            )
            conn.execute("UPDATE feedback SET replied = 1 WHERE id = ?", (feedback_id,))
            conn.commit()
            await update.message.reply_text(f"✅ Reply sent to feedback ID {feedback_id}")
        else:
            await update.message.reply_text("❌ Feedback ID not found or already replied")
        conn.close()
    except ValueError:
        await update.message.reply_text("❌ Feedback ID must be a number")
    except Exception as e:
        logger.error(f"Error replying to feedback: {e}")
        await update.message.reply_text("❌ An error occurred")

async def stats(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("🚫 You are not authorized to view stats.")
        return

    conn = sqlite3.connect("bot_data.db")
    subscribers = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
    feedback_count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    lookups = conn.execute("SELECT COUNT(*) FROM usage_logs WHERE action = 'result_lookup'").fetchone()[0]
    active_users = conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM usage_logs WHERE timestamp > ?",
        ((datetime.now() - timedelta(hours=24)).isoformat(),)
    ).fetchone()[0]
    conn.close()

    stats_message = (
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Subscribers: {subscribers}\n"
        f"📝 Feedback Received: {feedback_count}\n"
        f"🔍 Result Lookups: {lookups}\n"
        f"🕒 Active Users (24h): {active_users}"
    )
    await update.message.reply_text(stats_message, parse_mode='HTML')

async def error_handler(update: Update, context: CallbackContext) -> None:
    logger.error(f"Error: {context.error}")
    error_msg = await update.message.reply_text("❌ An error occurred. Please try again later.")
    context.user_data.setdefault('message_ids', []).append(error_msg.message_id)

def main() -> None:
    init_db()
    global subscribed_users
    subscribed_users = load_subscribers()

    application = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('feedback', feedback_start),
            CallbackQueryHandler(feedback_start, pattern="^(feedback|feedback_amharic)$"),
            CallbackQueryHandler(check_result_start, pattern="^(check_result|check_result_amharic)$"),
        ],
        states={
            LANGUAGE: [CallbackQueryHandler(select_language)],
            REGION: [CallbackQueryHandler(select_region)],
            REGISTRATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_registration)],
            FIRST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_first_name)],
            FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_feedback)],
        },
        fallbacks=[],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("reply", reply_to_feedback))
    application.add_handler(CommandHandler("stats", stats))
    application.add_error_handler(error_handler)

    webhook_url = os.getenv("WEBHOOK_URL", f"https://twotebot.onrender.com/{TOKEN}")
    port = int(os.getenv("PORT", 5000))

    if webhook_url:
        logger.info("Starting bot in webhook mode...")
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=TOKEN,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        logger.info("Starting bot in polling mode...")
        application.run_polling()

if __name__ == '__main__':
    main()
