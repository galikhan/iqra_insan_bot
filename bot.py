import html
import logging
import os
import re
from datetime import datetime, time, timedelta
from functools import wraps
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from db import DB

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

db = DB(DB_PATH)

# kk:10, kk10, kk-10, kk_12, "kk 10" — several per message allowed (separated by space/comma/newline)
ENTRY_RE = re.compile(r"([a-zA-Z]+)\s*[:\-_=]?\s*(\d+)")
TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")
NOTIFY_JOB_PREFIX = "notify_"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ This command is for admins only.")
            return
        return await func(update, context)

    return wrapper


def register(update: Update):
    u = update.effective_user
    db.upsert_user(u.id, update.effective_chat.id, u.username, u.full_name)


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def esc(s: str) -> str:
    return html.escape(s or "")


# ---------- parsing ----------

def parse_entries(text: str) -> Optional[List[Tuple[str, int]]]:
    """Return list of (code, value) if the whole message consists of entries, else None."""
    stripped = text.strip()
    matches = list(ENTRY_RE.finditer(stripped))
    if not matches:
        return None
    leftover = ENTRY_RE.sub("", stripped)
    if re.sub(r"[\s,;]+", "", leftover):
        return None  # message has other text, not a pure entry message
    return [(m.group(1).lower(), int(m.group(2))) for m in matches]


# ---------- general commands ----------

HELP_USER = (
    "<b>How to report</b>\n"
    "Send a code and a number, e.g. <code>kk:10</code>, <code>kk10</code>, "
    "<code>kk-10</code>, <code>kk_12</code>.\n"
    "Several at once: <code>kk:10 ab5</code>\n\n"
    "<b>Commands</b>\n"
    "/codes – list of available codes\n"
    "/todos – today's todo list\n"
    "/today – your report for today\n"
    "/week – your report for this week\n"
    "/month – your report for this month\n"
    "/undo – remove your last entry\n"
    "/help – this message"
)

HELP_ADMIN = (
    "\n\n<b>Admin commands</b>\n"
    "/add_code &lt;code&gt; [description] – add/update a code\n"
    "/del_code &lt;code&gt; – delete a code\n"
    "/add_todo &lt;text&gt; – add a daily todo\n"
    "/del_todo &lt;number&gt; – delete todo by its number\n"
    "/clear_todos – delete all todos\n"
    "/set_notification_at 10:00 18:00 – set notification times\n"
    "/notify_now – send the notification to everyone now\n"
    "/users – list registered users\n"
    "/today all, /week all, /month all – report for all users"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update)
    role = "admin" if is_admin(update.effective_user.id) else "user"
    await update.message.reply_text(
        f"Assalamu alaykum, {esc(update.effective_user.first_name)}! "
        f"You are registered as <b>{role}</b>.\n\n" + help_text(update.effective_user.id),
        parse_mode=ParseMode.HTML,
    )


def help_text(user_id: int) -> str:
    return HELP_USER + (HELP_ADMIN if is_admin(user_id) else "")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update)
    await update.message.reply_text(help_text(update.effective_user.id), parse_mode=ParseMode.HTML)


async def codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update)
    codes = db.list_codes()
    if not codes:
        await update.message.reply_text("No codes configured yet.")
        return
    lines = ["<b>Available codes</b>"]
    for code, desc in codes:
        lines.append(f"<code>{esc(code)}</code> — {esc(desc)}" if desc else f"<code>{esc(code)}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def todos_text() -> str:
    todos = db.list_todos()
    if not todos:
        return "No todos configured."
    return "\n".join(f"{i}. {esc(t)}" for i, (_, t) in enumerate(todos, 1))


async def todos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update)
    await update.message.reply_text(
        "<b>Daily todos</b>\n" + todos_text(), parse_mode=ParseMode.HTML
    )


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update)
    removed = db.undo_last(update.effective_user.id)
    if removed:
        await update.message.reply_text(f"↩️ Removed {removed[0]}:{removed[1]}")
    else:
        await update.message.reply_text("Nothing to undo.")


# ---------- reports ----------

def period_start(period: str) -> datetime:
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today
    if period == "week":
        return today - timedelta(days=today.weekday())  # Monday
    if period == "month":
        return today.replace(day=1)
    raise ValueError(period)


TITLES = {"today": "Today", "week": "This week", "month": "This month"}


def make_report_handler(period: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        register(update)
        since = period_start(period)
        header = f"📊 <b>{TITLES[period]}</b> (since {since:%d.%m.%Y})"
        uid = update.effective_user.id

        if context.args and context.args[0].lower() == "all":
            if not is_admin(uid):
                await update.message.reply_text("⛔ Only admins can see everyone's report.")
                return
            rows = db.sums(since)
            if not rows:
                await update.message.reply_text(header + "\n\nNo entries.", parse_mode=ParseMode.HTML)
                return
            lines, totals, current = [header], {}, None
            for name, code, total in rows:
                if name != current:
                    lines.append(f"\n👤 <b>{esc(name)}</b>")
                    current = name
                lines.append(f"  {esc(code)}: {total}")
                totals[code] = totals.get(code, 0) + total
            lines.append("\n<b>Total</b>")
            lines += [f"  {esc(c)}: {v}" for c, v in sorted(totals.items())]
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
            return

        rows = db.sums(since, user_id=uid)
        if not rows:
            await update.message.reply_text(header + "\n\nNo entries.", parse_mode=ParseMode.HTML)
            return
        lines = [header, ""] + [f"{esc(code)}: <b>{total}</b>" for code, total in rows]
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    return handler


# ---------- entry messages ----------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update)
    entries = parse_entries(update.message.text or "")
    if entries is None:
        await update.message.reply_text(
            "I didn't understand. Send something like <code>kk:10</code>. See /codes and /help.",
            parse_mode=ParseMode.HTML,
        )
        return

    unknown = sorted({c for c, _ in entries if not db.code_exists(c)})
    if unknown:
        await update.message.reply_text(
            f"❌ Unknown code(s): {', '.join(unknown)}. Nothing saved. See /codes."
        )
        return

    db.add_entries(update.effective_user.id, entries, now())
    saved = ", ".join(f"{c}:{v}" for c, v in entries)
    await update.message.reply_text(f"✅ Saved: {saved}")


# ---------- admin: codes & todos ----------

@admin_only
async def add_code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not re.fullmatch(r"[a-zA-Z]+", context.args[0]):
        await update.message.reply_text("Usage: /add_code <letters> [description]")
        return
    code = context.args[0].lower()
    desc = " ".join(context.args[1:])
    db.add_code(code, desc)
    await update.message.reply_text(f"✅ Code '{code}' saved.")


@admin_only
async def del_code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /del_code <code>")
        return
    code = context.args[0].lower()
    ok = db.delete_code(code)
    await update.message.reply_text(f"🗑 Code '{code}' deleted." if ok else "No such code.")


@admin_only
async def add_todo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /add_todo <text>")
        return
    db.add_todo(text)
    await update.message.reply_text("✅ Todo added.\n\n" + todos_text(), parse_mode=ParseMode.HTML)


@admin_only
async def del_todo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /del_todo <number>  (see /todos)")
        return
    removed = db.delete_todo_by_position(int(context.args[0]))
    if removed is None:
        await update.message.reply_text("No todo with that number.")
        return
    await update.message.reply_text(
        f"🗑 Removed: {esc(removed)}\n\n" + todos_text(), parse_mode=ParseMode.HTML
    )


@admin_only
async def clear_todos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.clear_todos()
    await update.message.reply_text("🗑 All todos removed.")


@admin_only
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = db.all_users()
    lines = [f"<b>Users ({len(users)})</b>"]
    for uid, _, name in users:
        tag = " (admin)" if is_admin(uid) else ""
        lines.append(f"• {esc(name)} <code>{uid}</code>{tag}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------- notifications ----------

async def send_notification(context: ContextTypes.DEFAULT_TYPE):
    text = "🔔 <b>Daily todos</b>\n" + todos_text() + "\n\nReport with e.g. <code>kk:10</code> · /codes"
    for _, chat_id, name in db.all_users():
        try:
            await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        except Exception as e:  # user blocked the bot, etc.
            log.warning("Failed to notify %s (%s): %s", name, chat_id, e)


def schedule_notifications(app: Application):
    jq = app.job_queue
    for job in jq.jobs():
        if job.name and job.name.startswith(NOTIFY_JOB_PREFIX):
            job.schedule_removal()
    for t in db.get_notify_times():
        h, m = map(int, t.split(":"))
        jq.run_daily(send_notification, time=time(h, m, tzinfo=TZ), name=NOTIFY_JOB_PREFIX + t)
    log.info("Notifications scheduled at %s (%s)", db.get_notify_times(), TZ)


@admin_only
async def set_notification_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = ", ".join(db.get_notify_times())
    if not context.args:
        await update.message.reply_text(
            f"Current times: {current}\nUsage: /set_notification_at 10:00 18:00"
        )
        return
    times = []
    for arg in " ".join(context.args).replace(",", " ").split():
        m = TIME_RE.match(arg)
        if not m:
            await update.message.reply_text(f"Invalid time '{arg}'. Use HH:MM, e.g. 09:30")
            return
        times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    times = sorted(set(times))
    db.set_notify_times(times)
    schedule_notifications(context.application)
    await update.message.reply_text(f"✅ Notifications will be sent at {', '.join(times)} ({TZ.key}).")


@admin_only
async def notify_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_notification(context)
    await update.message.reply_text("✅ Notification sent.")


# ---------- app ----------

async def post_init(app: Application):
    schedule_notifications(app)
    await app.bot.set_my_commands([
        BotCommand("codes", "List available codes"),
        BotCommand("todos", "Daily todo list"),
        BotCommand("today", "Report for today"),
        BotCommand("week", "Report for this week"),
        BotCommand("month", "Report for this month"),
        BotCommand("undo", "Remove last entry"),
        BotCommand("help", "Help"),
    ])


def main():
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty — nobody will have admin rights.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("codes", codes_cmd))
    app.add_handler(CommandHandler("todos", todos_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    for period in ("today", "week", "month"):
        app.add_handler(CommandHandler(period, make_report_handler(period)))

    app.add_handler(CommandHandler("add_code", add_code_cmd))
    app.add_handler(CommandHandler("del_code", del_code_cmd))
    app.add_handler(CommandHandler("add_todo", add_todo_cmd))
    app.add_handler(CommandHandler("del_todo", del_todo_cmd))
    app.add_handler(CommandHandler("clear_todos", clear_todos_cmd))
    app.add_handler(CommandHandler("set_notification_at", set_notification_cmd))
    app.add_handler(CommandHandler("notify_now", notify_now_cmd))
    app.add_handler(CommandHandler("users", users_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
