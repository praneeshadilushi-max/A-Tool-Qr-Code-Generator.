import os
import io
import qrcode
import time
import asyncio
import threading 
from datetime import datetime, date
from urllib.parse import urlparse

# Telegram Bot Library (Synchronous)
import telebot
from telebot import types

import asyncpg 

# SETTINGS
TOKEN = os.getenv("BOT_TOKEN")# Fallback token for testing

ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

SOFT_LIMIT_SECONDS = 8         
DAILY_LIMIT = 400          
MAX_TEXT_LENGTH = 500       

# List of banned keywords
BANNED_KEYWORDS = [
    "scam", "phishing", "malware", "illegal", "porn", "sex", "bomb", "fraud",
    "hack", "virus"
]

# GLOBAL SUPABASE SETUP 
DB_POOL = None
MAIN_LOOP = None 
# In-memory tracking
last_qr_time = {}     
active_users = set()   


# DATABASE ASYNC/SYNC WRAPPERS

def parse_supabase_url(url: str) -> dict:
  
    parsed_url = urlparse(url)
    if not all([parsed_url.scheme, parsed_url.username, parsed_url.password, parsed_url.hostname, parsed_url.path]):
        raise ValueError("Invalid SUPABASE_URL format. Must be a full postgresql:// URI.")
    
    return {
        'user': parsed_url.username,
        'password': parsed_url.password,
        'host': parsed_url.hostname,
        'port': parsed_url.port or 5432,
        'database': parsed_url.path.lstrip('/')
    }

async def init_db_pool():
    
    global DB_POOL
    
    supabase_url = os.getenv("SUPABASE_URL")
   
    
    if not supabase_url:
        print("An error occured.")
        return
# ... (Rest of the function continues)
    try:
        db_params = parse_supabase_url(supabase_url)
        DB_POOL = await asyncpg.create_pool(**db_params, min_size=3, max_size=10, timeout=10)
        print("Database Connection Pool successfully started.")
        
       
        await DB_POOL.execute("""
            CREATE TABLE IF NOT EXISTS qr_limits (
                user_id BIGINT PRIMARY KEY,
                daily_count INTEGER NOT NULL DEFAULT 0,
                last_date DATE NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
            );
        """)
        # 'qr_history' 
        await DB_POOL.execute("""
            CREATE TABLE IF NOT EXISTS qr_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                qr_data TEXT NOT NULL,
                time TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now())
            );
        """)
        
    except Exception as e:
        print(f"🚨 Database connection error: {e}")
        DB_POOL = None

# Async DB Functions (Inner Functions)
async def _async_get_user_limits(user_id: int) -> tuple[int, date]:
    
    if not DB_POOL: return 0, date.today()
    today = date.today()
    
    try:
        row = await DB_POOL.fetchrow("SELECT daily_count, last_date FROM qr_limits WHERE user_id = $1", user_id)
        if row is None:
            await DB_POOL.execute("INSERT INTO qr_limits (user_id, daily_count, last_date) VALUES ($1, $2, $3)", user_id, 0, today)
            return 0, today
        
        db_count, db_last_date = row['daily_count'], row['last_date']
        
        if db_last_date < today:
            await DB_POOL.execute("UPDATE qr_limits SET daily_count = 0, last_date = $1, updated_at = NOW() WHERE user_id = $2", today, user_id)
            return 0, today
        
        return db_count, db_last_date
    
    except Exception as e:
        print(f"🚨 Supabase data read error: {e}")
        return DAILY_LIMIT + 1, today

async def _async_update_user_limits(user_id: int, new_count: int, last_date: date):
    
    if not DB_POOL: return

    try:
        await DB_POOL.execute(
            """
            INSERT INTO qr_limits (user_id, daily_count, last_date)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id)
            DO UPDATE SET daily_count = $2, last_date = $3, updated_at = NOW()
            """,
            user_id, new_count, last_date
        )
    except Exception as e:
        print(f"🚨 Supabase data update error: {e}")

async def _async_add_history(user_id, username, qr_data):
   
    if not DB_POOL: return
    try:
        await DB_POOL.execute(
            "INSERT INTO qr_history (user_id, username, qr_data) VALUES ($1, $2, $3)",
            user_id, username, qr_data[:100] # Limit data for efficiency
        )
    except Exception as e:
        print(f"🚨 Supabase history collect error: {e}")

async def _async_get_today_history():
    
    if not DB_POOL: return []
    today_start = datetime.combine(date.today(), datetime.min.time())
    
    try:
        records = await DB_POOL.fetch(
            "SELECT user_id, username, qr_data, time FROM qr_history WHERE time >= $1",
            today_start
        )
        return records
    except Exception as e:
        print(f"🚨 Supabase history read error: {e}")
        return []

async def _async_cleanup_all_history():
   
    if not DB_POOL: return False
    try:
        await DB_POOL.execute("TRUNCATE qr_history")
        return True
    except Exception as e:
        print(f"🚨 Supabase history clean error: {e}")
        return False

async def _async_cleanup_percentage_history(percentage: int) -> int:
   
    if not DB_POOL: return 0
    
    try:
       
        total_count = await DB_POOL.fetchval("SELECT COUNT(*) FROM qr_history")
        if total_count == 0:
            return 0

        
        limit_to_delete = int(total_count * (percentage / 100.0))
        if limit_to_delete == 0:
            return 0
        
        
        deleted_records = await DB_POOL.fetch("""
            DELETE FROM qr_history
            WHERE id IN (
                SELECT id 
                FROM qr_history 
                ORDER BY time ASC
                LIMIT $1
            )
            RETURNING id
        """, limit_to_delete)
        
        return len(deleted_records)
        
    except Exception as e:
        print(f"🚨 Supabase percentage clean error ({percentage}%): {e}")
        return -1


# Synchronous Wrappers for telebot (Outer Functions)
def add_history(user_id, username, qr_data):
   
    global MAIN_LOOP
    if not MAIN_LOOP:
        print("Error: MAIN_LOOP not initialized.")
        return
        
    try:
        MAIN_LOOP.run_until_complete(_async_add_history(user_id, username, qr_data))
    except Exception as e:
        print(f"History Add Error: {e}")

def get_user_limits(user_id):
    
    global MAIN_LOOP
    if not MAIN_LOOP: return DAILY_LIMIT + 1, date.today()
    
    try:
        # returns tuple (count, date)
        return MAIN_LOOP.run_until_complete(_async_get_user_limits(user_id)) 
    except Exception as e:
        print(f"Get Limits Error: {e}")
        return DAILY_LIMIT + 1, date.today()

def get_user_today_count(user_id) -> int:
    
    count, _ = get_user_limits(user_id)
    return count

def update_user_limits(user_id, new_count, last_date):
    
    global MAIN_LOOP
    if not MAIN_LOOP: return
    
    try:
        MAIN_LOOP.run_until_complete(_async_update_user_limits(user_id, new_count, last_date))
    except Exception as e:
        print(f"Update Limits Error: {e}")

def get_today_history():
    
    global MAIN_LOOP
    if not MAIN_LOOP: return []
    
    try:
        return MAIN_LOOP.run_until_complete(_async_get_today_history())
    except Exception as e:
        print(f"Get History Error: {e}")
        return []

def cleanup_all_history():
    
    global MAIN_LOOP
    if not MAIN_LOOP: return False
    
    try:
        return MAIN_LOOP.run_until_complete(_async_cleanup_all_history())
    except Exception as e:
        print(f"Cleanup Error: {e}")
        return False
        
def cleanup_percentage_history(percentage: int) -> int:
  
    global MAIN_LOOP
    if not MAIN_LOOP: return -1
    
    try:
        return MAIN_LOOP.run_until_complete(_async_cleanup_percentage_history(percentage))
    except Exception as e:
        print(f"Cleanup Percentage Error: {e}")
        return -1


bot = telebot.TeleBot(TOKEN)

def is_text_malicious(text):
    
    if len(text) > MAX_TEXT_LENGTH:
        return True, "TEXT_TOO_LONG"
    
    text_lower = text.lower()
    
    # 2. Banned Keyword Check (High Risk filtering)
    if any(keyword in text_lower for keyword in BANNED_KEYWORDS):
        return True, "BANNED_KEYWORD"

    return False, None

# KEYBOARDS
def main_menu_keyboard():
    
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn1 = types.KeyboardButton("🌀 Generate QR Code")
    btn2 = types.KeyboardButton("📅 History")
    btn3 = types.KeyboardButton("ℹ️ About")
    btn4 = types.KeyboardButton("🔒 Privacy & Policies")
    btn5 = types.KeyboardButton("📞 Contact")
    btn6 = types.KeyboardButton("🆘 Help")
    keyboard.add(btn1, btn2)
    keyboard.add(btn3, btn4)
    keyboard.add(btn5, btn6)
    return keyboard

def back_button_keyboard():
   
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    back_btn = types.KeyboardButton("🔙 Back")
    keyboard.add(back_btn)
    return keyboard

# LIMIT CHECK FUNCTIONS
def can_generate_qr(user_id):
    
    now = datetime.now()
    last_time = last_qr_time.get(user_id)
    
    # 1. Soft limit check (8 seconds)
    if last_time:
        diff = (now - last_time).total_seconds()
        if diff < SOFT_LIMIT_SECONDS:
            return False, "soft", SOFT_LIMIT_SECONDS - int(diff) # Return remaining seconds
            
    # 2. Daily limit check (400 QR) 
    count, _ = get_user_limits(user_id)
    if count >= DAILY_LIMIT:
        return False, "daily", 0
        
    return True, None, 0

def update_qr_tracking(user_id):
    
    now = datetime.now()
    last_qr_time[user_id] = now
    
   
    count, current_date = get_user_limits(user_id)
    new_count = count + 1
    update_user_limits(user_id, new_count, current_date)


def start_live_countdown(chat_id):
   
    try:
        # Initial message
        countdown_msg = bot.send_message(chat_id, f"⏱️ Please wait **{SOFT_LIMIT_SECONDS} seconds** to generate another Qr code.", parse_mode="Markdown")

        for i in range(SOFT_LIMIT_SECONDS - 1, 0, -1):
            time.sleep(1) # Wait 1 second
            # Edit message for countdown
            bot.edit_message_text(f"⏱️ Please wait **{i} seconds** to generate another Qr code.", chat_id, countdown_msg.message_id, parse_mode="Markdown")

        time.sleep(1) # Wait for the last second

        # Final message update
        bot.edit_message_text("✅ Now you can generate another Qr code. Send text to generate another Qr code.", chat_id, countdown_msg.message_id)

    except telebot.apihelper.ApiTelegramException as e:
        # Handle cases where the message is too old or was deleted by the user
        if 'message is not modified' not in str(e) and 'message to edit not found' not in str(e):
            print(f"Error during countdown: {e}")
    except Exception as e:
        print(f"General error in countdown: {e}")

# BASIC COMMANDS
@bot.message_handler(commands=['start'])
def start_message(message):
    text = (
        "👋 Welcome!\n\n"
        "You can use your **text** to generate Qr code using this bot.\n\n"
        "⬇️ You can see the menu of this bot below."
    )
    bot.send_message(message.chat.id, text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

@bot.message_handler(commands=['menu'])
def menu_message(message):
    bot.send_message(
        message.chat.id,
        "📋 This is the main menu. Please select an option below:",
        reply_markup=main_menu_keyboard()
    )

# INFO COMMANDS
@bot.message_handler(commands=['about'], func=lambda m: m.text == "ℹ️ About" or m.text == "/about")
def about_message(message):
    text = (
        "ℹ️ *About Ak-Tool Qr Code Generator Bot.*\n\n"
        "This bot is designed to provide you to a fast, reliable and secure way to convert text, url  and some other types of data into quality qr codes.\n\n"
        f"⏱️ **Soft Limit (තත්පර {SOFT_LIMIT_SECONDS})**:\n"
        f"📅 **Daily Limit ({DAILY_LIMIT} QR)**: සියලුම පරිශීලකයින්ට සාධාරණ සේවාවක් ලබා දීම සඳහා.\n\n"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=back_button_keyboard())

@bot.message_handler(commands=['privacy'], func=lambda m: m.text == "🔒 Privacy & Policies" or m.text == "/privacy")
def privacy_message(message):
    text = (
        "🔒 *Privacy Policy & Terms of Use*\n\n"
        "1. Data Collection and Storage\n"
        "Our Bot collects the following data to maintain service stability and comply with regulations:\n"
        "• **User ID:** Used for managing limits and tracking history.\n"
        "• **QR Code Content (Text Data):** Stored temporarily in a **Supabase PostgreSQL Database** for monitoring and preventing illegal use.\n\n"
        "🔒 **Privacy Guarantee:** Your data will **not be shared** with any external third parties.\n\n"
        "2. Terms of Use and Limits\n"
        "By using this Bot, you agree to the following terms:\n"
        "🚫 **Prohibited Content:** It is strictly forbidden to create QR Codes for illegal, harmful, or fraudulent content (*scam/phishing*).\n"
        "• **Moderation:** We use **strict filters** to screen and reject prohibited content.\n\n"
        "i. Service Limits:\n"
        f"⏱️ **Soft Limit:** A time gap of **{SOFT_LIMIT_SECONDS} seconds** must be maintained between each QR code generation to ensure server stability.\n"
        f"📅 **Daily Limit:** You are limited to a maximum of **{DAILY_LIMIT} QR codes** per day to ensure fair access for all users.\n\n"
        "⚠️ Service suspension may apply for repeated violations of these terms."
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=back_button_keyboard())

@bot.message_handler(commands=['contact'], func=lambda m: m.text == "📞 Contact" or m.text == "/contact")
def contract_message(message):
    text = (
        "📞 *Contact Info*\n\n"
        "Developer: Akeesha Hewage.\n\n"
        "Do you have any questions or problems? You can contact us using the methods below.\n\n"
        "01.Email: praneeshadilushi@gmail.com\n"
        "02.Telegram: @praneeshaAk\n\n"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=back_button_keyboard())

@bot.message_handler(commands=['help'], func=lambda m: m.text == "🆘 Help" or m.text == "/help")
def help_message(message):
    text = (
        "🆘 *Help*\n\n"
        "This section provides assistance and information on how to use the bot \n\n"
        "📖 User Commands.\n"
        "1. /generate → Start Qr code generating.\n"
        "2. /stop→ To stop Qr code generation proccess.\n"
        "3. /history → To watch today count of Qr code generating you.\n"
         "4. /about → About this bot.\n"
         "5. /privacy →To read privacy policies and terms in use of our bot.\n"
         "6. /help→ To get kowledge and help about user commands in this bot.\n"
         "7. /download → To get know steps of download Qr codes have you generating. \n"
         "8. /contact→ To contact bot admin to get some help or get some knowledge about bot.\n\n"
         "📌️ If you have any question about steps of download Qr codes use /download command.\n\n"
         "📌️ If you have any question about this bot you can contact us. Use /contact command or contact button in menu to watch contact information."
         )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=back_button_keyboard())

@bot.message_handler(commands=['download'])
def download_message(message):
    text = (
        "⬇️ *Download QR Codes As Images*\n\n"
        "01. Click on Qr image.\n"
        "02. Click on three dots icon in upper side.\n"
        "03. Select **Save to Gallery**"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=back_button_keyboard())

# QR GENERATION MODE 
@bot.message_handler(commands=['generate'], func=lambda m: m.text == "🌀 Generate QR Code" or m.text == "/generate")
def start_generate(message):
    user_id = message.from_user.id
    active_users.add(user_id)
    bot.reply_to(
        message,
        "Now you can send **text** — this bot can generate a qr code for your text!\nYou can use /stop command any time to stop qr code generation.",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['stop'])
def stop_generate(message):
    user_id = message.from_user.id
    if user_id in active_users:
        active_users.remove(user_id)
        bot.reply_to(message,  "🛑 QR generation has been stopped.")
    else:
        bot.reply_to(message, "⚠️ You are not currently in QR generation mode.")

@bot.message_handler(commands=['history'], func=lambda m: m.text == "📅 History" or m.text == "/history")
def show_history(message):
    user_id = message.from_user.id
    count = get_user_today_count(user_id) 
    
    bot.reply_to(message, f"📅 **You have generated** {count} / {DAILY_LIMIT} Qr codes today.", parse_mode="Markdown")

# ADMIN COMMANDS
@bot.message_handler(commands=['adminhistory'])
def admin_history(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "🚫 You are not admin!")
        return

    today_records = get_today_history()
    
    if not today_records:
        bot.send_message(message.chat.id, "No history data stored today.")
        return

    # Count QR codes per user
    user_counts = {}
    for r in today_records:
        uid = r['user_id']
        uname = r.get('username', 'N/A')
        key = (uid, uname) 
        user_counts[key] = user_counts.get(key, 0) + 1

    text = f"📊 *User QR Summary for {date.today().strftime('%Y-%m-%d')}*\n\n"
    for (uid, uname), count in user_counts.items():
        display_name = f"@{uname}" if uname != 'N/A' else "No Username"
        text += f"👤 *{display_name}* | 🆔 `{uid}`\nQR Codes: {count}\n\n"

    bot.send_message(message.chat.id, text, parse_mode="Markdown")


@bot.message_handler(commands=['adminhistory_clean'])
def admin_clean_db(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "🚫 You are not admin!")
        return

    keyboard = types.InlineKeyboardMarkup()
   
    confirm_btn = types.InlineKeyboardButton("✅ Confirm Delete ALL", callback_data="confirm_clean_all")
    cancel_btn = types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_clean")
    keyboard.add(confirm_btn, cancel_btn)
    bot.send_message(
        message.chat.id,
        "⚠️ Do you want delete data of**History Table**? This cannot return!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@bot.message_handler(commands=['adminhistory_clean_25', 'adminhistory_clean_50', 'adminhistory_clean_75', 'adminhistory_clean_90'])
def admin_clean_percentage(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "🚫 You are not admin!")
        return

   
    try:
        command_part = message.text.split('_')[-1]
        percentage = int(command_part.replace('%', ''))
    except:
        bot.reply_to(message, "⚠️ Invalid clean command format. Use /adminhistory_clean_XX%")
        return

    keyboard = types.InlineKeyboardMarkup()
    
    confirm_btn = types.InlineKeyboardButton(f"✅ Confirm Delete {percentage}% (Oldest)", callback_data=f"confirm_clean_{percentage}")
    cancel_btn = types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_clean")
    keyboard.add(confirm_btn, cancel_btn)
    
    bot.send_message(
        message.chat.id,
        f"⚠️ ** Do you want to delete {percentage}% old data**? This operation cannot returned!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# CALLBACK HANDLER
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    # Only allow Admin to clean the DB
    if call.data.startswith("confirm_clean") or call.data == "cancel_clean":
        if call.from_user.id != ADMIN_ID:
            bot.answer_callback_query(call.id, "🚫 You are not authorized.")
            return
        
    bot.answer_callback_query(call.id) # Answer the query to remove the 'loading' status

    if call.data == "confirm_clean_all":
        # Full clean operation (Existing /adminhistory_clean)
        if cleanup_all_history():
            
            bot.edit_message_text(
                "✅*Database Cleaned!* \n\n All data has been cleaned.",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown"
            )
        else:
            bot.edit_message_text("❌ A database clean operation error occurred.", call.message.chat.id, call.message.message_id)

    elif call.data.startswith("confirm_clean_"):
        # Percentage clean operation (New commands)
        try:
            percentage = int(call.data.split('_')[-1])
        except ValueError:
            bot.edit_message_text("❌ Clean error cannot be identified.", call.message.chat.id, call.message.message_id)
            return

        bot.edit_message_text(f"⏳ {percentage}% of data delecting now...", call.message.chat.id, call.message.message_id)
        
        # Call the new percentage cleanup function
        deleted_count = cleanup_percentage_history(percentage) 

        if deleted_count >= 0:
            bot.edit_message_text(
                f"✅ Database Cleaned! {percentage}% of the oldest data (a total of {deleted_count} records) has been deleted ",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown"
            )
        else:
            bot.edit_message_text(f"❌ Database clean operation has been occur error. (Deleted: {deleted_count})", call.message.chat.id, call.message.message_id)


    elif call.data == "cancel_clean":
        bot.edit_message_text(
            "❌ Database clean admin operation has been cancelled.",
            call.message.chat.id,
            call.message.message_id
        )

    elif call.data == "why_soft_limit":
        bot.send_message(
            call.message.chat.id,
            f"⏱️ Soft Limit (seconds {SOFT_LIMIT_SECONDS}): This limit ensures server stability and fair usage for everyone. Its purpose is to prevent the server from becoming overloaded.",
            parse_mode="Markdown"
        )

    elif call.data == "why_daily_limit":
        bot.send_message(
            call.message.chat.id,
            f"📅 Daily Limit ({DAILY_LIMIT} QR): This limit restricts QR code generation to {DAILY_LIMIT} per day in order to control spam and ensure uninterrupted service for all users.",
            parse_mode="Markdown"
        )

# === BACK BUTTON HANDLER ===
@bot.message_handler(func=lambda m: m.text == "🔙 Back")
def go_back_to_menu(message):
   
    user_id = message.from_user.id
    if user_id in active_users:
        active_users.remove(user_id) # Exit the generate mode
    menu_message(message) # Go back to main menu

# MAIN MENU BUTTON HANDLERS
@bot.message_handler(func=lambda m: m.text == "🌀 Generate QR Code")
def open_generate_from_button(message):
    start_generate(message)

@bot.message_handler(func=lambda m: m.text == "📅 History")
def open_history_from_button(message):
    show_history(message)

@bot.message_handler(func=lambda m: m.text == "ℹ️ About")
def open_about_from_button(message):
    about_message(message)

@bot.message_handler(func=lambda m: m.text == "🔒 Privacy & Policies")
def open_privacy_from_button(message):
    privacy_message(message)

@bot.message_handler(func=lambda m: m.text == "📞 Contact")
def open_contact_from_button(message):
    contract_message(message)

@bot.message_handler(func=lambda m: m.text == "🆘 Help")
def open_help_from_button(message):
    help_message(message)

# === QR TEXT HANDLER (සීමාවන් සහිතව) ===
@bot.message_handler(func=lambda m: True and m.content_type == 'text')
def generate_qr_from_text(message):
    user_id = message.from_user.id
    user_name = message.from_user.username or message.from_user.first_name
    text = message.text

    # Ignore messages if not in active mode and not a known command/button
    if user_id not in active_users:
        if text not in ["🌀 Generate QR Code", "📅 History", "ℹ️ About", "🔒 Privacy & Policies", "📞 Contact", "🆘 Help", "🔙 Back"]:
             bot.reply_to(message, "⚠️ Please click the **🌀 Generate QR Code** button on the menu or use the /generate command to start."
, parse_mode="Markdown")
        return

    # Content Moderation Check (AdsGram Compliance)
    is_harmful, reason = is_text_malicious(text)
    if is_harmful:
        if user_id != ADMIN_ID:
             bot.send_message(ADMIN_ID, f"🚨 CONTENT VIOLATION attempt by User ID: {user_id}. Reason: {reason}. Text: `{text[:100]}...`")
        bot.reply_to(
            message,
            f"❌ **Content rejected.**\n"
            f"This Bot does not allow the generation of QR Codes for illegal, harmful or fraudulent content (including scams and phishing).\n"
            "Please adhere to the Terms of Use.",
            parse_mode="Markdown"
        )
        return
    # End Moderation Check

    #  Limit Checks
    can_generate, reason, remaining_time = can_generate_qr(user_id)
    keyboard = types.InlineKeyboardMarkup()

    if not can_generate:
        if reason == "soft":
            keyboard.add(types.InlineKeyboardButton("Why This Limit", callback_data="why_soft_limit"))
            bot.reply_to(message, f"❕Please wait another **{remaining_time} seconds**.", reply_markup=keyboard, parse_mode="Markdown")
        elif reason == "daily":
            keyboard.add(types.InlineKeyboardButton("Why This Limit", callback_data="why_daily_limit"))
            bot.reply_to(message, f"🛑 Daily Limit Reached. You have already generated **{DAILY_LIMIT}** QR codes. Please return tomorrow. If you want know about daily limit click button below.", reply_markup=keyboard, parse_mode="Markdown")
        return

    # Generation Logic
    # Update tracking before generation (Updates Supabase)
    update_qr_tracking(user_id)

    # 1. Generate QR Image to BytesIO (in-memory)
    qr_img = qrcode.make(text)
    bio = io.BytesIO() # Use io.BytesIO explicitly
    qr_img.save(bio, format='PNG')
    bio.seek(0)
    
    # 2. Send 'Generating' status and then the final QR
    msg = bot.send_message(message.chat.id, "⏳ Please wait, generating Qr code...")

    # Send QR photo
    caption_text = text if len(text) < 200 else f"{text[:200]}..."

    bot.send_photo(message.chat.id, bio, caption=f"✅ Qr code has been successfully generated.\n\n📝 Text: `{caption_text}`", parse_mode="Markdown")
    
    # Delete the temporary status message
    try:
        bot.delete_message(message.chat.id, msg.message_id)
    except Exception:
        pass # Ignore if deletion fails

    # 3. Add to database history (Adds to Supabase History table)
    add_history(user_id, user_name, text)

    # 4. Start the 8-second live countdown in the chat
    threading.Thread(target=start_live_countdown, args=(message.chat.id,)).start()

# START BOT
if __name__ == '__main__':
    # Database Initialization (Async Setup)
    try:
        # 1. Get the current event loop
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        MAIN_LOOP = loop 
        # Set the main loop globally
        
        # 2. Run the DB pool initialization on the main loop
        MAIN_LOOP.run_until_complete(init_db_pool()) 
        print("✅ Database Setup Complete. Starting Bot Polling...")
    except Exception as e:
        print(f"❌ Critical Error during Database Setup: {e}")

    # Bot Polling (Synchronous)
    print("Bot is running...")
    bot.polling(none_stop=True, interval=0)