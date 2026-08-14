import os
import json
import uuid
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
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# Azure VM MySQL configuration
DB_CONFIG = {
    "host": os.getenv("host"),
    "port": os.getenv("port", "3306"),
    "user": os.getenv("user"),
    "password": os.getenv("password"),
    "database": os.getenv("database")
}

# ==========================================
# 2. DATABASE SCHEMA
# ==========================================

DATABASE_SCHEMA = """
Table: customers (
    id INT PRIMARY KEY,
    name VARCHAR(100),
    phone_number VARCHAR(20),
    mail VARCHAR(255)
)

Table: orders (
    id INT PRIMARY KEY,
    customer_id INT,
    order_date TIMESTAMP,
    total_amount DECIMAL(10,2),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
)
"""

# ==========================================
# 3. PERSISTENT STORAGE & CACHE LOGIC
# ==========================================

HISTORY_FILE = "user_history.json"
GOVERNANCE_FILE = "governance.json"
USERS_FILE = "users.json"
MAX_HISTORY_LENGTH = 10  # Keeps last 10 messages (5 turns)

def load_json(filepath):
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8") as file:
        try:
            data = json.load(file)
            return {int(k): v for k, v in data.items()}
        except json.JSONDecodeError:
            return {}

def save_json(filepath, data_dict):
    with open(filepath, "w", encoding="utf-8") as file:
        json.dump(data_dict, file, indent=4)

# Initialize data from files
user_history = load_json(HISTORY_FILE)
governance_state = load_json(GOVERNANCE_FILE)
users_db = load_json(USERS_FILE)

# Temporary in-memory registration buffer
registration_data = {}
pending_queries = {}

# ==========================================
# 4. INITIALIZE CLIENTS
# ==========================================

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 5. GEMINI SCHEMAS & API CALLS
# ==========================================

class AgentSQLResponse(BaseModel):
    intent: str = Field(
        description="Strictly either 'READ' or 'WRITE'"
    )
    sql: str = Field(
        description="The raw MySQL query ready for execution"
    )
    explanation: str = Field(
        description=(
            "A clear, non-technical explanation of exactly "
            "what this query will change or read. Use plain English."
        )
    )

def get_sql_from_gemini(user_request: str, history: str) -> AgentSQLResponse:
    prompt = f"""
You are a MySQL database agent.
Translate the user's request into a valid MySQL query.

Database Schema:
{DATABASE_SCHEMA}

Recent Chat History (Use this to understand context if the user uses pronouns):
{history if history else "No previous history."}

Rules:
1. Only generate valid MySQL.
2. The intent must be either READ or WRITE.
3. READ is for SELECT queries.
4. WRITE is for INSERT, UPDATE, or DELETE.
5. Never generate DROP, TRUNCATE, ALTER, or GRANT.
6. Never generate multiple SQL statements.
7. For UPDATE and DELETE, always use a WHERE clause.
8. Return only one SQL query.

User Request:
{user_request}
"""

    response = client.models.generate_content(
        model="gemini-3-flash-preview", 
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AgentSQLResponse,
            temperature=0.1,
        ),
    )
    return response.parsed

def summarize_data_with_gemini(data: list, user_request: str, history: str) -> str:
    prompt = f"""
Recent Chat History:
{history if history else "No previous history."}

The user asked:
"{user_request}"

The database returned:
{data}

Summarize the answer in a friendly and concise way. Report the output in a clean Markdown format, applicable in Telegram.
If there's a list order it by number not dash or indent.
Do not mention SQL or database implementation details.
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text

# ==========================================
# 6. DATABASE EXECUTION
# ==========================================

def execute_query(sql: str, fetch_results: bool = False):
    connection = None
    cursor = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql)

        if fetch_results:
            result = cursor.fetchall()
            return True, result
        else:
            connection.commit()
            return True, cursor.rowcount
    except Error as e:
        return False, str(e)
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()

# ==========================================
# 7. ONBOARDING & /START COMMAND
# ==========================================

@bot.message_handler(commands=["start"])
def start(message):
    chat_id = message.chat.id
    user = users_db.get(chat_id)

    # 1. Registered and Approved
    if user and user.get("role") in ["admin", "viewer"]:
        markup = telebot_types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        btn1 = telebot_types.KeyboardButton("List customers")
        btn2 = telebot_types.KeyboardButton("List orders")
        btn3 = telebot_types.KeyboardButton("Show today's income")
        btn4 = telebot_types.KeyboardButton("Count total orders")
        markup.add(btn1, btn2, btn3, btn4)

        bot.send_message(
            chat_id,
            f"👋 Welcome back, {user['name']}! (Role: **{user['role'].upper()}**)\n\n"
            "Tell me what you want to read or update using plain English, or use the quick menu below.",
            parse_mode="Markdown",
            reply_markup=markup
        )
    # 2. Pending Approval
    elif user and user.get("role") == "pending":
        bot.send_message(
            chat_id,
            "⏳ Your access request is currently pending review by the administrator."
        )
    # 3. New User: Start Onboarding Form
    else:
        msg = bot.send_message(
            chat_id,
            "👋 Welcome to the Database Agent!\n\n"
            "You are not authorized yet. Please fill out this short form to request access.\n\n"
            "👤 *What is your full name?*",
            parse_mode="Markdown",
            reply_markup=telebot_types.ReplyKeyboardRemove()
        )
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
    msg = bot.send_message(chat_id, "📝 *Briefly describe why you need access to this database:*", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_desc_step)

def process_desc_step(message):
    chat_id = message.chat.id
    registration_data[chat_id]["desc"] = message.text.strip()

    # Save to user database with pending status
    users_db[chat_id] = {
        "name": registration_data[chat_id]["name"],
        "phone": registration_data[chat_id]["phone"],
        "wanted_role": registration_data[chat_id]["wanted_role"],
        "desc": registration_data[chat_id]["desc"],
        "role": "pending"
    }
    save_json(USERS_FILE, users_db)

    bot.send_message(
        chat_id,
        "✅ *Your request has been submitted!*\nThe administrator has been notified and will review your application.",
        parse_mode="Markdown"
    )

    # Forward request to Admin
    if ADMIN_CHAT_ID:
        keyboard = telebot_types.InlineKeyboardMarkup()
        keyboard.row(
            telebot_types.InlineKeyboardButton("✅ Approve as ADMIN", callback_data=f"auth_admin_{chat_id}"),
            telebot_types.InlineKeyboardButton("👀 Approve as VIEWER", callback_data=f"auth_viewer_{chat_id}")
        )
        keyboard.row(
            telebot_types.InlineKeyboardButton("❌ Reject", callback_data=f"auth_reject_{chat_id}")
        )

        admin_msg = (
            f"🚨 **New Database Access Request**\n\n"
            f"👤 **Name:** {users_db[chat_id]['name']}\n"
            f"📞 **Phone:** {users_db[chat_id]['phone']}\n"
            f"🎭 **Requested Role:** {users_db[chat_id]['wanted_role']}\n"
            f"📝 **Reason:** {users_db[chat_id]['desc']}\n\n"
            f"Choose an action:"
        )
        bot.send_message(int(ADMIN_CHAT_ID), admin_msg, parse_mode="Markdown", reply_markup=keyboard)

# ==========================================
# 8. /SETTINGS COMMAND (Governance Toggle)
# ==========================================

@bot.message_handler(commands=["settings"])
def settings_menu(message):
    chat_id = message.chat.id
    user = users_db.get(chat_id)
    if not user or user.get("role") != "admin":
        bot.send_message(chat_id, "❌ Only administrators can adjust governance settings.")
        return

    is_enabled = governance_state.get(chat_id, True)
    status_text = "🟢 ENABLED" if is_enabled else "🔴 DISABLED"
    btn_text = "Disable Governance 🔴" if is_enabled else "Enable Governance 🟢"

    keyboard = telebot_types.InlineKeyboardMarkup()
    keyboard.add(telebot_types.InlineKeyboardButton(btn_text, callback_data="toggle_gov"))

    bot.send_message(
        chat_id,
        f"🛡 **Governance Settings**\n\n"
        f"Deterministic security checks block destructive statements (`DROP`, `TRUNCATE`, `ALTER`, unbounded `UPDATE`/`DELETE`).\n\n"
        f"Current Status: **{status_text}**",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# ==========================================
# 9. USER MESSAGE HANDLER (RBAC Enforced)
# ==========================================

@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_user_message(message):
    user_text = message.text
    chat_id = message.chat.id

    # 1. Check Authorization
    user = users_db.get(chat_id)
    if not user or user.get("role") not in ["admin", "viewer"]:
        bot.send_message(chat_id, "❌ You are not authorized to use this bot. Type /start to request access.")
        return

    user_role = user["role"]

    if chat_id not in user_history:
        user_history[chat_id] = []

    history_string = "\n".join(user_history[chat_id])

    status_msg = bot.send_message(chat_id, "🧠 Analyzing request...")

    try:
        agent_response = get_sql_from_gemini(user_text, history_string)
        sql = agent_response.sql.strip()
        sql_upper = sql.upper()

        # 2. RBAC Write Guard
        if agent_response.intent == "WRITE" and user_role != "admin":
            bot.edit_message_text(
                "❌ **Access Denied:** Your role (`VIEWER`) does not have permission to modify the database.",
                chat_id,
                status_msg.message_id,
                parse_mode="Markdown"
            )
            return

        # 3. Optional Deterministic Governance
        is_gov_enabled = governance_state.get(chat_id, True)
        if is_gov_enabled:
            forbidden_keywords = ["DROP", "TRUNCATE", "ALTER", "GRANT"]

            if any(keyword in sql_upper for keyword in forbidden_keywords):
                bot.edit_message_text(
                    "❌ Security Alert:\n\nDestructive schema queries are blocked.",
                    chat_id,
                    status_msg.message_id
                )
                return

            if ";" in sql.rstrip(";"):
                bot.edit_message_text(
                    "❌ Security Alert:\n\nMultiple SQL statements are not allowed.",
                    chat_id,
                    status_msg.message_id
                )
                return

            if (
                agent_response.intent == "WRITE"
                and (sql_upper.startswith("UPDATE") or sql_upper.startswith("DELETE"))
                and "WHERE" not in sql_upper
            ):
                bot.edit_message_text(
                    "❌ Security Alert:\n\nUPDATE/DELETE without a WHERE clause is blocked.",
                    chat_id,
                    status_msg.message_id
                )
                return

        # ----------------------------------
        # READ
        # ----------------------------------
        if agent_response.intent == "READ":
            bot.edit_message_text(
                "🔍 Fetching data from Azure...",
                chat_id,
                status_msg.message_id
            )

            success, result = execute_query(sql, fetch_results=True)

            if success:
                if not result:
                    bot.edit_message_text(
                        "🔍 No matching records were found.",
                        chat_id,
                        status_msg.message_id
                    )
                    return

                friendly_summary = summarize_data_with_gemini(result, user_text, history_string)

                # LRU Cache memory
                user_history[chat_id].append(f"User: {user_text}")
                user_history[chat_id].append(f"Agent: {friendly_summary}")
                user_history[chat_id] = user_history[chat_id][-MAX_HISTORY_LENGTH:]
                save_json(HISTORY_FILE, user_history)

                try:
                    bot.edit_message_text(
                        friendly_summary,
                        chat_id,
                        status_msg.message_id,
                        parse_mode="Markdown"
                    )
                except Exception:
                    bot.edit_message_text(
                        friendly_summary,
                        chat_id,
                        status_msg.message_id
                    )
            else:
                bot.edit_message_text(
                    f"⚠️ Database Error:\n\n{result}",
                    chat_id,
                    status_msg.message_id
                )

        # ----------------------------------
        # WRITE
        # ----------------------------------
        elif agent_response.intent == "WRITE":
            query_id = str(uuid.uuid4())[:8]

            pending_queries[query_id] = {
                "sql": sql,
                "chat_id": chat_id,
                "user_text": user_text
            }

            keyboard = telebot_types.InlineKeyboardMarkup()
            approve_button = telebot_types.InlineKeyboardButton(
                "✅ Approve",
                callback_data=f"approve_{query_id}"
            )
            reject_button = telebot_types.InlineKeyboardButton(
                "❌ Reject",
                callback_data=f"reject_{query_id}"
            )
            keyboard.row(approve_button, reject_button)

            bot.edit_message_text(
                f"⚠️ Action Required\n\n"
                f"{agent_response.explanation}\n\n"
                f"Do you approve this change?",
                chat_id,
                status_msg.message_id,
                reply_markup=keyboard
            )
        else:
            bot.edit_message_text(
                "❌ Invalid agent intent.",
                chat_id,
                status_msg.message_id
            )

    except Exception as e:
        bot.edit_message_text(
            f"❌ An error occurred:\n\n{str(e)}",
            chat_id,
            status_msg.message_id
        )

# ==========================================
# 10. CALLBACK BUTTON HANDLER
# ==========================================

@bot.callback_query_handler(func=lambda call: True)
def handle_button_callback(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    data = call.data

    # ----------------------------------
    # 1. GOVERNANCE TOGGLE
    # ----------------------------------
    if data == "toggle_gov":
        current_state = governance_state.get(chat_id, True)
        new_state = not current_state
        governance_state[chat_id] = new_state
        save_json(GOVERNANCE_FILE, governance_state)

        status_text = "🟢 ENABLED" if new_state else "🔴 DISABLED"
        btn_text = "Disable Governance 🔴" if new_state else "Enable Governance 🟢"

        keyboard = telebot_types.InlineKeyboardMarkup()
        keyboard.add(telebot_types.InlineKeyboardButton(btn_text, callback_data="toggle_gov"))

        bot.edit_message_text(
            f"🛡 **Governance Settings**\n\n"
            f"Deterministic security checks block destructive statements (`DROP`, `TRUNCATE`, `ALTER`, unbounded `UPDATE`/`DELETE`).\n\n"
            f"Current Status: **{status_text}**",
            chat_id,
            call.message.message_id,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    # ----------------------------------
    # 2. ADMIN USER AUTHORIZATION
    # ----------------------------------
    if data.startswith("auth_"):
        parts = data.split("_")
        action = parts[1]
        target_user_id = int(parts[2])

        if action == "admin":
            users_db[target_user_id]["role"] = "admin"
            bot.edit_message_text(
                f"✅ Approved user `{target_user_id}` as **ADMIN**.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown"
            )
            bot.send_message(
                target_user_id,
                "🎉 Your access request has been approved! You have been granted **ADMIN** access.\nType /start to load your menu.",
                parse_mode="Markdown"
            )
        elif action == "viewer":
            users_db[target_user_id]["role"] = "viewer"
            bot.edit_message_text(
                f"✅ Approved user `{target_user_id}` as **VIEWER**.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown"
            )
            bot.send_message(
                target_user_id,
                "🎉 Your access request has been approved! You have been granted **VIEWER** access.\nType /start to load your menu.",
                parse_mode="Markdown"
            )
        elif action == "reject":
            users_db.pop(target_user_id, None)
            bot.edit_message_text(
                f"❌ Rejected access for user `{target_user_id}`.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown"
            )
            bot.send_message(
                target_user_id,
                "❌ Your access request was declined by the administrator."
            )

        save_json(USERS_FILE, users_db)
        return

    # ----------------------------------
    # 3. SQL WRITE APPROVAL / REJECTION
    # ----------------------------------
    try:
        action, query_id = data.split("_", 1)
    except ValueError:
        bot.edit_message_text(
            "❌ Invalid callback.",
            chat_id,
            call.message.message_id
        )
        return

    pending = pending_queries.get(query_id)

    if not pending:
        bot.edit_message_text(
            "❌ This action has expired or has already been processed.",
            chat_id,
            call.message.message_id
        )
        return

    sql_to_execute = pending["sql"]
    original_user_text = pending["user_text"]

    if action == "approve":
        success, result = execute_query(sql_to_execute, fetch_results=False)

        if success:
            bot.edit_message_text(
                "✅ Action completed successfully.",
                chat_id,
                call.message.message_id
            )

            if chat_id not in user_history:
                user_history[chat_id] = []

            # Save to LRU cache
            user_history[chat_id].append(f"User: {original_user_text}")
            user_history[chat_id].append(f"Agent: Approved and executed change -> {sql_to_execute}")
            user_history[chat_id] = user_history[chat_id][-MAX_HISTORY_LENGTH:]
            save_json(HISTORY_FILE, user_history)

        else:
            bot.edit_message_text(
                f"⚠️ Execution Failed:\n\n{result}",
                chat_id,
                call.message.message_id
            )

    elif action == "reject":
        bot.edit_message_text(
            "❌ Action cancelled by user.",
            chat_id,
            call.message.message_id
        )

    del pending_queries[query_id]

# ==========================================
# 11. MAIN
# ==========================================

if __name__ == "__main__":
    print("🚀 Database Agent is running...")
    bot.infinity_polling(skip_pending=True)