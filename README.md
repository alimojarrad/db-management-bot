# Telegram Database Agent (Text-to-SQL)

A Telegram bot that acts as a natural language database management agent. This project allows users to read and update a MySQL database using plain English, powered by the Google Gemini API. 

It includes a Human-in-the-Loop (HITL) governance system that prevents accidental data destruction by requiring explicit user approval for any query that modifies data.

## Features

* **Natural Language to SQL:** Uses Gemini 2.5 Flash with Structured Outputs to reliably translate English intents into valid MySQL queries.
* **Smart Intent Routing:** Automatically detects whether a prompt is a `READ` (SELECT) or a `WRITE` (UPDATE, INSERT, DELETE) operation.
* **Human-in-the-Loop Validation:** Before executing any `WRITE` query, the bot generates a plain-English explanation of the change and requires the user to click an "Approve" or "Reject" inline button.
* **Security Governance:** Hardcoded rules automatically block destructive queries (e.g., `DROP`, `ALTER`, `TRUNCATE`) and unbounded updates (e.g., `UPDATE` without a `WHERE` clause).
* **Conversational Output:** Translates raw database rows back into friendly, conversational text.

## Prerequisites

Before running the bot, ensure you have the following:

* Python 3.9 or higher
* A Telegram Bot Token (get this from [@BotFather](https://t.me/botfather))
* A Google Gemini API Key
* A MySQL Database (e.g., Azure Database for MySQL)

## Installation

1. **Clone the repository** (if applicable) or create a new directory for the project.

2. **Install the required dependencies:**
   ```bash
   pip install "python-telegram-bot[job-queue]" google-genai pydantic mysql-connector-python