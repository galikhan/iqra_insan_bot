# iqra_insan_bot

Telegram bot that collects daily numeric reports from users by code and sends todo reminders.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill BOT_TOKEN and ADMIN_IDS
python bot.py
```

Every user (including admins) must press **/start** once to get registered and receive notifications.

## Reporting

Send `kk:10`, `kk10`, `kk-10`, `kk_12` (several at once: `kk:10 ab5`). Only codes listed in `/codes` are accepted.

## Commands

| Everyone | |
|---|---|
| `/codes` | list codes |
| `/todos` | daily todo list |
| `/today` `/week` `/month` | your sums per code |
| `/undo` | delete your last entry |

| Admin | |
|---|---|
| `/add_code kk Description` / `/del_code kk` | manage codes |
| `/add_todo text` / `/del_todo 2` / `/clear_todos` | manage daily todos |
| `/set_notification_at 10:00 18:00` | reminder times (default 10:00, 18:00) |
| `/notify_now` | send reminder now |
| `/users` | registered users |
| `/today all` (also week/month) | report for all users |

Week runs Wednesday–Tuesday; month starts on the 1st. Times use `TIMEZONE`.
