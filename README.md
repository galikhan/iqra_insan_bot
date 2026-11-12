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

One value per code per day: a new message **replaces** today's value for that code, so users send the day's total (`kk:10` in the morning, `kk:15` after reading 5 more pages). `kk:0` deletes today's value.

## Commands

| Everyone | |
|---|---|
| `/codes` | list codes |
| `/todos` | daily todo list |
| `/today` `/week` `/month` | your sums per code |

| Admin | |
|---|---|
| `/add_code kk Description` / `/del_code kk` | manage codes |
| `/add_codes` + one `code — description` per line | add many codes at once |
| `/add_todo text` / `/del_todo 2` / `/clear_todos` | manage daily todos |
| `/set_notification_at 10:00 18:00` | reminder times (default 10:00, 18:00) |
| `/notify_now` | send reminder now |
| `/users` | registered users |
| `/set_report_group` (send inside the group) | post daily report of all users to that group |
| `/set_report_at 20:00` / `/unset_report_group` | group report time (default 20:00) / stop it |
| `/report_now` | post today's report to the group now |
| `/today all` (also week/month) | report for all users |

Week runs Wednesday–Tuesday; month starts on the 1st. Times use `TIMEZONE`.
