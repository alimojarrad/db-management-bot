import os
import json
import uuid
from datetime import date
import mysql.connector
from mysql.connector import Error
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import telebot
from telebot import types as telebot_types
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 1. CONFIGURATION & CREDENTIALS
# ==========================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Parse multiple admin IDs from a comma-separated string
admin_ids_raw = os.getenv("ADMIN_CHAT_IDS", " ")

ADMIN_CHAT_IDS = [int(x.strip()) for x in admin_ids_raw.split(",") if x.strip()]


DB_CONFIG = {
    "host": os.getenv("host"),
    "port": os.getenv("port", "3306"),
    "user": os.getenv("user"),
    "password": os.getenv("password"),
    "database": os.getenv("database")
}

DATABASE_SCHEMA = """
Table: customers (id INT PRIMARY KEY, name VARCHAR(100), phone_number VARCHAR(20), mail VARCHAR(255))
Table: orders (id INT PRIMARY KEY, customer_id INT, order_date TIMESTAMP, total_amount DECIMAL(10,2), FOREIGN KEY (customer_id) REFERENCES customers(id))
"""

# ==========================================
# 2. UNIFIED PERSISTENT STORAGE 
# ==========================================

DATA_FILE = "bot_data.json"
MAX_HISTORY_LENGTH = 10 
DAILY_VIEWER_LIMIT = 5

PRESET_MENU_COMMANDS = [
    "List customers",
    "List orders",
    "Show today's income",
    "Count total orders"
]

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as file:
        try:
            return json.load(file)
        except json.JSONDecodeError:
            return {}

def save_data(data_dict):
    with open(DATA_FILE, "w", encoding="utf-8") as file:
        json.dump(data_dict, file, indent=4)

bot_data = load_data()

def init_user(chat_id: int):
    cid = str(chat_id)
    if cid not in bot_data:
        bot_data[cid] = {
            "profile": None, 
            "history": [],
            "governance_enabled": True,
            "rate_limit": {"date": str(date.today()), "count": 0}
        }
    return bot_data[cid]

registration_data = {}
pending_queries = {}
# NEW: Tracks auth messages sent to admins so we can delete/edit them later
pending_auth_requests = {} 

# ==========================================
# 3. INITIALIZE CLIENTS
# ==========================================

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 4. GEMINI SCHEMAS & API CALLS
# ==========================================

class RelevanceCheckResponse(BaseModel):
    is_relevant: bool = Field(description="True if related to querying/updating database or system admin. False if casual chat.")

class AgentSQLResponse(BaseModel):
    intent: str = Field(description="Strictly either 'READ' or 'WRITE'")
    sql: str = Field(description="The raw MySQL query ready for execution")
    explanation: str = Field(description="A clear, non-technical explanation of exactly what this query will change or read. Use plain English.")

def check_message_relevance(user_request: str) -> bool:
    prompt = f"Analyze if this message is relevant to a database bot or system admin. Ignore casual chat.\nMessage: {user_request}"
    response = client.models.generate_content(
        model="gemini-3-flash-preview", 
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=RelevanceCheckResponse, temperature=0.1),
    )
    return response.parsed.is_relevant

def get_sql_from_gemini(user_request: str, history: str) -> AgentSQLResponse:
    prompt = f"""
You are a MySQL database agent. Translate the user's request into a valid MySQL query.
Database Schema:
{DATABASE_SCHEMA}
Recent Chat History:
{history if history else "No previous history."}
Rules:
1. Only valid MySQL.
2. Intent: READ (SELECT) or WRITE (INSERT/UPDATE/DELETE).
3. No DROP, TRUNCATE, ALTER, GRANT.
4. No multiple statements.
5. UPDATE/DELETE must have WHERE.
User Request: {user_request}
"""
    response = client.models.generate_content(
        model="gemini-3-flash-preview", 
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=AgentSQLResponse, temperature=0.1),
    )
    return response.parsed

def summarize_data_with_gemini(data: list, user_request: str, history: str) -> str:
    prompt = f"Recent History: {history}\nUser asked: {user_request}\nDB returned: {data}\nSummarize friendly in Markdown. Do not mention SQL implementation."
    response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    return response.text

# ==========================================
# 5. DATABASE EXECUTION
# ==========================================

def execute_query(sql: str, fetch_results: bool = False):
    connection, cursor = None, None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql)
        if fetch_results:
            return True, cursor.fetchall()
        else:
            connection.commit()
            return True, cursor.rowcount
    except Error as e:
        return False, str(e)
    finally:
        if cursor: cursor.close()
        if connection and connection.is_connected(): connection.close()

# ==========================================
# 6. ONBOARDING & REGISTRATION FLOW
# ==========================================

@bot.message_handler(commands=["start"])
def start_command(message):
    chat_id = message.chat.id
    user_data = init_user(chat_id)
    profile = user_data.get("profile")

    if profile and profile.get("role") in ["admin", "viewer"]:
        markup = telebot_types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        markup.add(*[telebot_types.KeyboardButton(cmd) for cmd in PRESET_MENU_COMMANDS])
        bot.send_message(chat_id, f"👋 Welcome back, {profile['name']}! (Role: **{profile['role'].upper()}**)\n\nUse the quick menu, or send custom queries using `/msg <prompt>`.", parse_mode="Markdown", reply_markup=markup)
    elif profile and profile.get("role") == "pending":
        bot.send_message(chat_id, "⏳ Your access request is currently pending review by the administrator(s).")
    else:
        msg = bot.send_message(chat_id, "👋 Welcome!\nYou are not authorized yet. Please fill out this short form.\n\n👤 *What is your full name?*", parse_mode="Markdown", reply_markup=telebot_types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_name_step)

def process_name_step(message):
    chat_id = message.chat.id
    registration_data[chat_id] = {"name": message.text.strip()}
    msg = bot.send_message(chat_id, "📞 *What is your phone number?*", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_phone_step)

def process_phone_step(message):
    chat_id = message.chat.id
    registration_data[chat_id]["phone"] = message.text.strip()
    msg = bot.send_message(chat_id, "🎭 *Which role are you requesting?* (Type `Viewer` or `Admin`)", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_role_step)

def process_role_step(message):
    chat_id = message.chat.id
    registration_data[chat_id]["wanted_role"] = message.text.strip()
    msg = bot.send_message(chat_id, "📝 *Briefly describe why you need access:*", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_desc_step)

def process_desc_step(message):
    chat_id = message.chat.id
    registration_data[chat_id]["desc"] = message.text.strip()

    user_data = init_user(chat_id)
    user_data["profile"] = {
        "name": registration_data[chat_id]["name"],
        "phone": registration_data[chat_id]["phone"],
        "wanted_role": registration_data[chat_id]["wanted_role"],
        "desc": registration_data[chat_id]["desc"],
        "role": "pending"
    }
    save_data(bot_data)

    bot.send_message(chat_id, "✅ *Your request has been submitted!*\nThe administrators have been notified.", parse_mode="Markdown")

    if ADMIN_CHAT_ID:
        keyboard = telebot_types.InlineKeyboardMarkup()
        keyboard.row(
            telebot_types.InlineKeyboardButton("✅ Approve ADMIN", callback_data=f"auth_admin_{chat_id}"),
            telebot_types.InlineKeyboardButton("👀 Approve VIEWER", callback_data=f"auth_viewer_{chat_id}")
        )
        keyboard.row(telebot_types.InlineKeyboardButton("❌ Reject", callback_data=f"auth_reject_{chat_id}"))

        admin_msg = (
            f"🚨 **New Access Request**\n\n"
            f"👤 **Name:** {user_data['profile']['name']}\n"
            f"🎭 **Requested:** {user_data['profile']['wanted_role']}\n"
            f"📝 **Reason:** {user_data['profile']['desc']}\n"
        )
        
        # Keep track of message IDs for each admin
        pending_auth_requests[chat_id] = []
        
        for admin_id in ADMIN_CHAT_IDS:
            try:
                sent_msg = bot.send_message(admin_id, admin_msg, parse_mode="Markdown", reply_markup=keyboard)
                pending_auth_requests[chat_id].append({"admin_id": admin_id, "message_id": sent_msg.message_id})
            except Exception as e:
                print(f"Failed to send auth request to Admin {admin_id}: {e}")

# ==========================================
# 7. HELP & SETTINGS COMMANDS
# ==========================================

@bot.message_handler(commands=["help"])
def help_command(message):
    help_text = (
        "📖 **Available Commands**\n\n"
        "• `/start` - Start the bot, register, or refresh the quick menu\n"
        "• `/help` - Show this list of available commands\n"
        "• `/msg <prompt>` or `/message <prompt>` - Send a natural language prompt\n"
        "• `/settings` - Toggle deterministic governance (*Admin only*)"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=["settings"])
def settings_menu(message):
    chat_id = message.chat.id
    user_data = init_user(chat_id)
    profile = user_data.get("profile")

    if not profile or profile.get("role") != "admin":
        bot.send_message(chat_id, "❌ Only administrators can adjust governance settings.")
        return

    is_enabled = user_data["governance_enabled"]
    status_text = "🟢 ENABLED" if is_enabled else "🔴 DISABLED"
    btn_text = "Disable Governance 🔴" if is_enabled else "Enable Governance 🟢"

    keyboard = telebot_types.InlineKeyboardMarkup()
    keyboard.add(telebot_types.InlineKeyboardButton(btn_text, callback_data="toggle_gov"))

    bot.send_message(chat_id, f"🛡 **Governance Settings**\n\nCurrent Status: **{status_text}**", parse_mode="Markdown", reply_markup=keyboard)

# ==========================================
# 8. DATABASE QUERY PROCESSOR (CORE PIPELINE)
# ==========================================

def process_database_query(chat_id: int, user_text: str):
    user_data = init_user(chat_id)
    profile = user_data.get("profile")

    if not profile or profile.get("role") not in ["admin", "viewer"]:
        bot.send_message(chat_id, "❌ You are not authorized. Type /start to request access.")
        return

    user_role = profile["role"]

    # 1. RATE LIMIT CHECK
    if user_role == "viewer":
        today_str = str(date.today())
        user_usage = user_data["rate_limit"]
        
        if user_usage.get("date") != today_str:
            user_usage = {"date": today_str, "count": 0}

        if user_usage["count"] >= DAILY_VIEWER_LIMIT:
            bot.send_message(chat_id, f"🛑 **Daily Limit Reached**\nViewers are limited to {DAILY_VIEWER_LIMIT} requests/day.", parse_mode="Markdown")
            return

    status_msg = bot.send_message(chat_id, "🧠 Analyzing request...")

    try:
        # 2. RELEVANCE CHECK
        if not check_message_relevance(user_text):
            bot.edit_message_text("⚠️ Please provide a database-related prompt.", chat_id, status_msg.message_id)
            return

        # 3. INCREMENT RATE LIMIT
        if user_role == "viewer":
            user_usage["count"] += 1
            user_data["rate_limit"] = user_usage
            save_data(bot_data)

        # 4. GENERATE SQL
        history_string = "\n".join(user_data["history"])
        agent_response = get_sql_from_gemini(user_text, history_string)
        sql = agent_response.sql.strip()
        sql_upper = sql.upper()

        # 5. RBAC & GOVERNANCE
        if agent_response.intent == "WRITE" and user_role != "admin":
            bot.edit_message_text("❌ **Access Denied:** Viewers cannot modify data.", chat_id, status_msg.message_id, parse_mode="Markdown")
            return

        if user_data["governance_enabled"]:
            if any(k in sql_upper for k in ["DROP", "TRUNCATE", "ALTER", "GRANT"]) or ";" in sql.rstrip(";"):
                bot.edit_message_text("❌ Security Alert: Destructive/Multiple queries blocked.", chat_id, status_msg.message_id)
                return
            if agent_response.intent == "WRITE" and (sql_upper.startswith("UPDATE") or sql_upper.startswith("DELETE")) and "WHERE" not in sql_upper:
                bot.edit_message_text("❌ Security Alert: Unbounded UPDATE/DELETE blocked.", chat_id, status_msg.message_id)
                return

        # 6. READ EXECUTION
        if agent_response.intent == "READ":
            bot.edit_message_text("🔍 Fetching data...", chat_id, status_msg.message_id)
            success, result = execute_query(sql, fetch_results=True)

            if success:
                if not result:
                    bot.edit_message_text("🔍 No matching records found.", chat_id, status_msg.message_id)
                    return

                summary = summarize_data_with_gemini(result, user_text, history_string)
                user_data["history"].extend([f"User: {user_text}", f"Agent: {summary}"])
                user_data["history"] = user_data["history"][-MAX_HISTORY_LENGTH:]
                save_data(bot_data)

                try:
                    bot.edit_message_text(summary, chat_id, status_msg.message_id, parse_mode="Markdown")
                except Exception:
                    bot.edit_message_text(summary, chat_id, status_msg.message_id)
            else:
                bot.edit_message_text(f"⚠️ DB Error:\n{result}", chat_id, status_msg.message_id)

        # 7. WRITE EXECUTION (HITL)
        elif agent_response.intent == "WRITE":
            query_id = str(uuid.uuid4())[:8]
            pending_queries[query_id] = {"sql": sql, "chat_id": chat_id, "user_text": user_text}
            
            kb = telebot_types.InlineKeyboardMarkup()
            kb.row(telebot_types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{query_id}"), telebot_types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{query_id}"))
            bot.edit_message_text(f"⚠️ Action Required\n\n{agent_response.explanation}\n\nApprove change?", chat_id, status_msg.message_id, reply_markup=kb)

    except Exception as e:
        error_str = str(e)
        
        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "Quota exceeded" in error_str:
            user_alert = "🛠 **Server Under Maintenance**\n\nOur systems are currently experiencing high traffic. Please try again in a few minutes."
            admin_alert = f"🚨 **API Quota Exceeded (429)** 🚨\n\nUser `{chat_id}` attempted a query but the Gemini API limit was reached.\n\n*Error Trace:*\n`{error_str[:3000]}`"
        else:
            user_alert = "❌ **An unexpected error occurred.**\n\nThe server is under maintenance or encountered a bug. The system administrator has been notified."
            admin_alert = f"🚨 **System Error** 🚨\n\nUser `{chat_id}` encountered an error.\n\n*Error Trace:*\n`{error_str[:3000]}`"
        
        bot.edit_message_text(user_alert, chat_id, status_msg.message_id, parse_mode="Markdown")
        
        # Broadcast error to ALL admins
        
        for admin_id in ADMIN_CHAT_IDS:
                
            try:
                bot.send_message(int(admin_id), admin_alert, parse_mode="Markdown")
            except Exception:
                pass

# ==========================================
# 9. MESSAGE HANDLERS (/msg, preset, commands)
# ==========================================

@bot.message_handler(commands=["msg", "message"])
def handle_custom_message_command(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.send_message(message.chat.id, "⚠️ **Please provide a prompt.**\nExample: `/msg list orders`", parse_mode="Markdown")
        return
    process_database_query(message.chat.id, parts[1].strip())

@bot.message_handler(func=lambda msg: msg.text and msg.text.startswith("/"))
def handle_unknown_slash_command(message):
    bot.send_message(message.chat.id, "❓ **Unknown command.** Type `/help` to see available commands.", parse_mode="Markdown")

@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_general_text(message):
    text = message.text.strip()
    if text in PRESET_MENU_COMMANDS:
        process_database_query(message.chat.id, text)
    else:
        bot.send_message(message.chat.id, f"ℹ️ **Use `/msg` for custom queries.**\nExample: `/msg {text}`", parse_mode="Markdown")

# ==========================================
# 10. CALLBACK BUTTON HANDLER
# ==========================================

@bot.callback_query_handler(func=lambda call: True)
def handle_button_callback(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    cid = str(chat_id)
    data = call.data

    # --- Governance Toggle ---
    if data == "toggle_gov":
        user_data = init_user(chat_id)
        new_state = not user_data["governance_enabled"]
        user_data["governance_enabled"] = new_state
        save_data(bot_data)

        kb = telebot_types.InlineKeyboardMarkup()
        kb.add(telebot_types.InlineKeyboardButton("Disable 🔴" if new_state else "Enable 🟢", callback_data="toggle_gov"))
        bot.edit_message_text(f"🛡 **Governance Settings**\n\nCurrent Status: **{'🟢 ENABLED' if new_state else '🔴 DISABLED'}**", chat_id, call.message.message_id, parse_mode="Markdown", reply_markup=kb)
        return

    # --- User Access Approvals (Multi-Admin) ---
    if data.startswith("auth_"):
        parts = data.split("_")
        action = parts[1]
        target_user_id = parts[2]
        target_user_id_int = int(target_user_id)
        admin_name = call.from_user.first_name

        user_data = bot_data.get(target_user_id)
        
        # Check if another admin already handled this
        if not user_data or not user_data.get("profile") or user_data["profile"].get("role") != "pending":
            bot.edit_message_text("⚠️ This request has already been handled by another administrator.", chat_id, call.message.message_id)
            return

        # Process the role assignment
        status_text = ""
        notification_text = ""
        
        if action == "admin":
            bot_data[target_user_id]["profile"]["role"] = "admin"
            status_text = f"✅ Approved as **ADMIN** by {admin_name}."
            notification_text = "🎉 You are now an **ADMIN**! Type /start"
        elif action == "viewer":
            bot_data[target_user_id]["profile"]["role"] = "viewer"
            status_text = f"✅ Approved as **VIEWER** by {admin_name}."
            notification_text = "🎉 You are now a **VIEWER**! Type /start"
        elif action == "reject":
            bot_data[target_user_id]["profile"] = None
            status_text = f"❌ Rejected by {admin_name}."
            notification_text = "❌ Access declined."
            
        save_data(bot_data)
        bot.send_message(target_user_id_int, notification_text, parse_mode="Markdown")
        
        # Update messages for ALL admins to remove buttons and show who handled it
        auth_msgs = pending_auth_requests.get(target_user_id_int, [])
        for a_msg in auth_msgs:
            a_id = a_msg["admin_id"]
            m_id = a_msg["message_id"]
            try:
                if a_id == chat_id:
                    bot.edit_message_text(status_text, a_id, m_id, parse_mode="Markdown")
                else:
                    bot.edit_message_text(f"🔒 *Request Resolved*\nThis request was handled by {admin_name}.", a_id, m_id, parse_mode="Markdown")
            except Exception:
                pass # Message might have been deleted manually by the admin
                
        # Clean up memory
        if target_user_id_int in pending_auth_requests:
            del pending_auth_requests[target_user_id_int]
            
        return

    # --- SQL Write Approvals ---
    try:
        action, query_id = data.split("_", 1)
    except ValueError:
        return

    pending = pending_queries.get(query_id)
    if not pending:
        bot.edit_message_text("❌ Action expired.", chat_id, call.message.message_id)
        return

    if action == "approve":
        success, result = execute_query(pending["sql"], fetch_results=False)
        if success:
            bot.edit_message_text("✅ Action completed.", chat_id, call.message.message_id)
            user_data = init_user(chat_id)
            user_data["history"].extend([f"User: {pending['user_text']}", f"Agent: Executed -> {pending['sql']}"])
            user_data["history"] = user_data["history"][-MAX_HISTORY_LENGTH:]
            save_data(bot_data)
        else:
            bot.edit_message_text(f"⚠️ Failed:\n{result}", chat_id, call.message.message_id)
    elif action == "reject":
        bot.edit_message_text("❌ Cancelled.", chat_id, call.message.message_id)
    
    del pending_queries[query_id]

# ==========================================
# 11. MAIN
# ==========================================
if __name__ == "__main__":
    print("🚀 Database Agent is running...")
    bot.infinity_polling(skip_pending=True)