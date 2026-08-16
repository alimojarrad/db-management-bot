# Deployment Guide

This guide explains how to deploy the Telegram bot as a `systemd` service on a Linux server.

## 1. Clone the Repository

Clone the project onto the server:

```bash
git clone <repository-url>
cd <project-directory>
```

## 2. Create a Python Virtual Environment

Create a virtual environment:

```bash
python3 -m venv .bot-env
```

Activate it:

```bash
source .bot-env/bin/activate
```

Install the required dependencies:

```bash
pip install -r requirements.txt
```

## 3. Configure the Bot

Configure the required environment variables or bot credentials before starting the application.

Do not commit private credentials such as the Telegram bot token to Git.

## 4. Test the Bot

Before configuring `systemd`, verify that the bot works correctly:

```bash
./.bot-env/bin/python bot.py
```

If the bot starts successfully, stop it with:

```text
Ctrl+C
```

## 5. Configure systemd

The repository contains the following service file:

```text
tele-bot.service
```

Copy it to the systemd unit directory:

```bash
sudo cp "tele-bot.service" /etc/systemd/system/tele-bot.service
```

Reload systemd:

```bash
sudo systemctl daemon-reload
```

## 6. Enable the Service

Enable the service so that the bot starts automatically when the server boots:

```bash
sudo systemctl enable tele-bot
```

Start the bot:

```bash
sudo systemctl start tele-bot
```

## 7. Check the Service

Check whether the bot is running:

```bash
sudo systemctl status tele-bot
```

A successful deployment should show:

```text
Active: active (running)
```

## 8. Managing the Bot

Start:

```bash
sudo systemctl start tele-bot
```

Stop:

```bash
sudo systemctl stop tele-bot
```

Restart:

```bash
sudo systemctl restart tele-bot
```

Check status:

```bash
sudo systemctl status tele-bot
```

## 9. View Logs

View the bot's systemd logs:

```bash
sudo journalctl -u tele-bot
```

Follow logs in real time:

```bash
sudo journalctl -u tele-bot -f
```

View recent logs:

```bash
sudo journalctl -u tele-bot -n 100
```

## Troubleshooting

If the service fails to start, first check its status:

```bash
sudo systemctl status tele-bot
```

Then inspect the logs:

```bash
sudo journalctl -u tele-bot -n 100 --no-pager
```

Common issues include:

* Incorrect `ExecStart` path
* Incorrect `WorkingDirectory`
* Missing Python dependencies
* Incorrect file permissions
* Missing environment variables
* Incorrect `User` configuration
* Security policies such as SELinux preventing execution

After modifying the service file, reload systemd:

```bash
sudo systemctl daemon-reload
```

Then restart the service:

```bash
sudo systemctl restart tele-bot
```
