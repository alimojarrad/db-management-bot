import os
from dotenv import load_dotenv
import uuid
import mysql.connector
from mysql.connector import Error
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import telebot
from telebot import types as telebot_types

load_dotenv()
# ==========================================
# 1. CONFIGURATION & CREDENTIALS
# ==========================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Azure VM MySQL configuration
DB_CONFIG = {
    "host": os.getenv("host"),
    "port": "3306",
    "user": "project",
    "password": os.getenv("password"),
    "database": "my_database"
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
# 3. INITIALIZE CLIENTS
# ==========================================

bot = telebot.TeleBot(TELEGRAM_TOKEN)

client = genai.Client(api_key=GEMINI_API_KEY)

# In-memory store for pending writes
pending_queries = {}


# ==========================================
# 4. GEMINI SCHEMAS & API CALLS
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


def get_sql_from_gemini(user_request: str) -> AgentSQLResponse:

    prompt = f"""
You are a MySQL database agent.

Translate the user's request into a valid MySQL query.

Database Schema:
{DATABASE_SCHEMA}

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


def summarize_data_with_gemini(
    data: list,
    user_request: str
) -> str:

    prompt = f"""
The user asked:

"{user_request}"

The database returned:

{data}

Summarize the answer in a friendly and concise way.

Do not mention SQL or database implementation details.
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    return response.text


# ==========================================
# 5. DATABASE EXECUTION
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
# 6. /START COMMAND
# ==========================================

@bot.message_handler(commands=["start"])
def start(message):

    bot.send_message(
        message.chat.id,
        "👋 Hello! I am your database agent.\n\n"
        "Tell me what you want to read or update "
        "using plain English."
    )


# ==========================================
# 7. USER MESSAGE HANDLER
# ==========================================

@bot.message_handler(
    func=lambda message: True,
    content_types=["text"]
)
def handle_user_message(message):

    user_text = message.text

    status_msg = bot.send_message(
        message.chat.id,
        "🧠 Analyzing request..."
    )

    try:

        # ----------------------------------
        # Generate SQL
        # ----------------------------------

        agent_response = get_sql_from_gemini(user_text)

        sql = agent_response.sql.strip()
        sql_upper = sql.upper()

        # ----------------------------------
        # SECURITY / GOVERNANCE
        # ----------------------------------

        forbidden_keywords = [
            "DROP",
            "TRUNCATE",
            "ALTER",
            "GRANT"
        ]

        if any(
            keyword in sql_upper
            for keyword in forbidden_keywords
        ):

            bot.edit_message_text(
                "❌ Security Alert:\n\n"
                "Destructive schema queries are blocked.",
                message.chat.id,
                status_msg.message_id
            )

            return

        # ----------------------------------
        # Prevent multiple statements
        # ----------------------------------

        if ";" in sql.rstrip(";"):

            bot.edit_message_text(
                "❌ Security Alert:\n\n"
                "Multiple SQL statements are not allowed.",
                message.chat.id,
                status_msg.message_id
            )

            return

        # ----------------------------------
        # Prevent unbounded UPDATE/DELETE
        # ----------------------------------

        if (
            agent_response.intent == "WRITE"
            and (
                sql_upper.startswith("UPDATE")
                or sql_upper.startswith("DELETE")
            )
            and "WHERE" not in sql_upper
        ):

            bot.edit_message_text(
                "❌ Security Alert:\n\n"
                "UPDATE/DELETE without a WHERE clause "
                "is blocked.",
                message.chat.id,
                status_msg.message_id
            )

            return

        # ----------------------------------
        # READ
        # ----------------------------------

        if agent_response.intent == "READ":

            bot.edit_message_text(
                "🔍 Fetching data from Azure...",
                message.chat.id,
                status_msg.message_id
            )

            success, result = execute_query(
                sql,
                fetch_results=True
            )

            if success:

                if not result:

                    bot.edit_message_text(
                        "🔍 No matching records were found.",
                        message.chat.id,
                        status_msg.message_id
                    )

                    return

                friendly_summary = (
                    summarize_data_with_gemini(
                        result,
                        user_text
                    )
                )

                bot.edit_message_text(
                    friendly_summary,
                    message.chat.id,
                    status_msg.message_id
                )

            else:

                bot.edit_message_text(
                    f"⚠️ Database Error:\n\n{result}",
                    message.chat.id,
                    status_msg.message_id
                )

        # ----------------------------------
        # WRITE
        # ----------------------------------

        elif agent_response.intent == "WRITE":

            query_id = str(uuid.uuid4())[:8]

            pending_queries[query_id] = {
                "sql": sql,
                "chat_id": message.chat.id
            }

            keyboard = telebot_types.InlineKeyboardMarkup()

            approve_button = (
                telebot_types.InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"approve_{query_id}"
                )
            )

            reject_button = (
                telebot_types.InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=f"reject_{query_id}"
                )
            )

            keyboard.row(
                approve_button,
                reject_button
            )

            bot.edit_message_text(
                f"⚠️ Action Required\n\n"
                f"{agent_response.explanation}\n\n"
                f"Do you approve this change?",
                message.chat.id,
                status_msg.message_id,
                reply_markup=keyboard
            )

        else:

            bot.edit_message_text(
                "❌ Invalid agent intent.",
                message.chat.id,
                status_msg.message_id
            )

    except Exception as e:

        bot.edit_message_text(
            f"❌ An error occurred:\n\n{str(e)}",
            message.chat.id,
            status_msg.message_id
        )


# ==========================================
# 8. CALLBACK BUTTON HANDLER
# ==========================================

@bot.callback_query_handler(
    func=lambda call: True
)
def handle_button_callback(call):

    bot.answer_callback_query(call.id)

    try:

        action, query_id = call.data.split("_", 1)

    except ValueError:

        bot.edit_message_text(
            "❌ Invalid callback.",
            call.message.chat.id,
            call.message.message_id
        )

        return

    pending = pending_queries.get(query_id)

    if not pending:

        bot.edit_message_text(
            "❌ This action has expired or "
            "has already been processed.",
            call.message.chat.id,
            call.message.message_id
        )

        return

    sql_to_execute = pending["sql"]

    # ==================================
    # APPROVE
    # ==================================

    if action == "approve":

        success, result = execute_query(
            sql_to_execute,
            fetch_results=False
        )

        if success:

            bot.edit_message_text(
                "✅ Action completed successfully.",
                call.message.chat.id,
                call.message.message_id
            )

        else:

            bot.edit_message_text(
                f"⚠️ Execution Failed:\n\n{result}",
                call.message.chat.id,
                call.message.message_id
            )

    # ==================================
    # REJECT
    # ==================================

    elif action == "reject":

        bot.edit_message_text(
            "❌ Action cancelled by user.",
            call.message.chat.id,
            call.message.message_id
        )

    # ==================================
    # CLEANUP
    # ==================================

    del pending_queries[query_id]


# ==========================================
# 9. MAIN
# ==========================================

if __name__ == "__main__":

    print("🚀 Database Agent is running...")

    bot.infinity_polling(
        skip_pending=True
    )