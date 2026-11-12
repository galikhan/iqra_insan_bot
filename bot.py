import html
import logging
import os
import re
from datetime import datetime, time, timedelta
from functools import wraps
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    Update,
)
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
# Codes are stored in Latin. Cyrillic letters that look like Latin ones are mapped to them,
# so "ск" (Cyrillic) == "ck" (Latin). Any Cyrillic/Kazakh letter is accepted by the parser;
# ones without a Latin twin simply won't match an existing code.
CYR_TO_LAT = str.maketrans("авеёкмнорстухіһү", "abeekmhopctyxihy")
LETTERS = "a-zA-Zа-яА-ЯёЁәіңғүұқөһӘІҢҒҮҰҚӨҺ"
ENTRY_RE = re.compile(rf"([{LETTERS}]+)\s*[:\-_=]?\s*(\d+)")
TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")
NOTIFY_JOB_PREFIX = "notify_"
GROUP_REPORT_JOB = "group_report"
DEFAULT_GROUP_REPORT_TIME = "20:00"
WEEK_START_WEEKDAY = 2  # Wednesday


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Бұл команда тек админдерге арналған.")
            return
        return await func(update, context)

    return wrapper


def register(update: Update):
    # Only private chats: chat_id is where personal reminders go, never a group.
    if update.effective_chat.type != "private":
        return
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
    return [(normalize_code(m.group(1)), int(m.group(2))) for m in matches]


def normalize_code(code: str) -> str:
    return code.lower().translate(CYR_TO_LAT)


# ---------- general commands ----------

HELP_USER = (
    "<b>Қалай есеп беру керек</b>\n"
    "Код пен санды жіберіңіз, мысалы: <code>kk:10</code>, <code>kk10</code>, "
    "<code>kk-10</code>, <code>kk_12</code>.\n"
    "Бірнешеуін бірден: <code>kk:10 ab5</code>\n"
    "Әр кодқа күніне <b>бір мән</b>: жаңа хабарлама бүгінгі мәнді <b>ауыстырады</b>. "
    "Сондықтан күннің <b>жалпы санын</b> жіберіңіз (мысалы, таңертең kk:10, кешке тағы 5 бет оқысаңыз — kk:15). "
    "Бүгінгі мәндеріңізді /today арқылы көруге болады. "
    "Қате жіберсеңіз — дұрысын қайта жіберіңіз, ал <code>kk:0</code> бүгінгі жазбаны өшіреді.\n\n"
    "<b>Командалар</b>\n"
    "/codes – қолжетімді кодтар тізімі\n"
    "/todos – күнделікті тапсырмалар тізімі\n"
    "/today – бүгінгі есебіңіз\n"
    "/week – осы аптадағы есебіңіз\n"
    "/month – осы айдағы есебіңіз\n"
    "/help – осы хабарлама"
)

HELP_ADMIN = (
    "\n\n<b>Админ командалары</b>\n"
    "/add_code &lt;код&gt; [сипаттама] – код қосу/өзгерту\n"
    "/add_codes – бірнеше кодты бірден қосу (әр жолға: код — сипаттама)\n"
    "/del_code &lt;код&gt; – кодты өшіру\n"
    "/add_todo &lt;мәтін&gt; – күнделікті тапсырма қосу\n"
    "/del_todo &lt;нөмір&gt; – тапсырманы нөмірі бойынша өшіру\n"
    "/clear_todos – барлық тапсырмаларды өшіру\n"
    "/set_notification_at 10:00 18:00 – еске салу уақытын орнату\n"
    "/notify_now – еске салуды барлығына қазір жіберу\n"
    "/set_report_group – (топта жазу) күнделікті есепті осы топқа жіберу\n"
    "/unset_report_group – топқа есеп жіберуді тоқтату\n"
    "/set_report_at 20:00 – топқа есеп жіберу уақыты\n"
    "/report_now – бүгінгі есепті топқа қазір жіберу\n"
    "/users – тіркелген пайдаланушылар тізімі\n"
    "/today all, /week all, /month all – барлық пайдаланушылардың есебі"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update)
    if is_admin(update.effective_user.id):
        await set_admin_menu(context.bot, update.effective_user.id)
    role = "админ" if is_admin(update.effective_user.id) else "пайдаланушы"
    await update.message.reply_text(
        f"Ассалаумағалейкум, {esc(update.effective_user.first_name)}! "
        f"Сіз <b>{role}</b> ретінде тіркелдіңіз.\n\n" + help_text(update.effective_user.id),
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
        await update.message.reply_text("Әзірге кодтар қосылмаған.")
        return
    lines = ["<b>Қолжетімді кодтар</b>"]
    for code, desc in codes:
        lines.append(f"<code>{esc(code)}</code> — {esc(desc)}" if desc else f"<code>{esc(code)}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def todos_text() -> str:
    todos = db.list_todos()
    if not todos:
        return "Тапсырмалар қосылмаған."
    return "\n".join(f"{i}. {esc(t)}" for i, (_, t) in enumerate(todos, 1))


async def todos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update)
    await update.message.reply_text(
        "<b>Күнделікті тапсырмалар</b>\n" + todos_text(), parse_mode=ParseMode.HTML
    )


# ---------- reports ----------

def period_start(period: str) -> datetime:
    today = now().replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today
    if period == "week":
        # Week runs Wednesday..Tuesday (weekday(): Mon=0, Wed=2)
        return today - timedelta(days=(today.weekday() - WEEK_START_WEEKDAY) % 7)
    if period == "month":
        return today.replace(day=1)
    raise ValueError(period)


TITLES = {"today": "Бүгін", "week": "Осы апта", "month": "Осы ай"}


def fmt_sums(items: List[Tuple[str, int]]) -> str:
    return " · ".join(f"{esc(code)} {total}" for code, total in items)


def build_all_report(period: str) -> str:
    """Compact report: one line per user, who reported and who didn't."""
    since = period_start(period)
    users = db.all_users()  # (id, chat_id, full_name)
    per_user = {}
    totals = {}
    for uid, code, total in db.sums_by_user(since):
        per_user.setdefault(uid, []).append((code, total))
        totals[code] = totals.get(code, 0) + total

    names = {uid: name for uid, _, name in users}
    done = sorted((uid for uid in per_user if uid in names), key=lambda u: names[u].lower())
    missing = sorted((uid for uid in names if uid not in per_user), key=lambda u: names[u].lower())

    lines = [f"📊 <b>{TITLES[period]}</b> · {since:%d.%m.%Y}" + (f"–{now():%d.%m.%Y}" if period != "today" else "")]
    lines.append(f"\n✅ <b>Есеп берді ({len(done)}/{len(names)})</b>")
    lines += [f"{esc(names[u])}: {fmt_sums(per_user[u])}" for u in done] or ["—"]
    if missing:
        lines.append(f"\n❌ <b>Есеп бермеді ({len(missing)})</b>")
        lines.append(", ".join(esc(names[u]) for u in missing))
    if totals:
        lines.append(f"\nΣ {fmt_sums(sorted(totals.items()))}")
    return "\n".join(lines)


def make_report_handler(period: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        register(update)
        since = period_start(period)
        header = f"📊 <b>{TITLES[period]}</b> ({since:%d.%m.%Y} бастап)"
        uid = update.effective_user.id

        if context.args and context.args[0].lower() == "all":
            if not is_admin(uid):
                await update.message.reply_text("⛔ Барлығының есебін тек админдер көре алады.")
                return
            await update.message.reply_text(build_all_report(period), parse_mode=ParseMode.HTML)
            return

        rows = db.sums(since, user_id=uid)
        if not rows:
            await update.message.reply_text(header + "\n\nЖазбалар жоқ.", parse_mode=ParseMode.HTML)
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
            "Түсінбедім. Мысалы, <code>kk:10</code> түрінде жіберіңіз. /codes және /help қараңыз.",
            parse_mode=ParseMode.HTML,
        )
        return

    unknown = sorted({c for c, _ in entries if not db.code_exists(c)})
    if unknown:
        await update.message.reply_text(
            f"❌ Белгісіз код(тар): {', '.join(unknown)}. Ештеңе сақталмады. /codes қараңыз."
        )
        return

    lines = []
    for code, value, prev in db.set_daily_values(update.effective_user.id, entries, now()):
        was = f" (бұрын: {prev})" if prev is not None and prev != value else ""
        if value == 0:
            lines.append(f"🗑 {code}: бүгінгі жазба өшірілді{was}")
        else:
            lines.append(f"✅ {code}: {value}{was}")
    await update.message.reply_text("\n".join(lines))


# ---------- admin: codes & todos ----------

@admin_only
async def add_code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = normalize_code(context.args[0]) if context.args else ""
    if not re.fullmatch(r"[a-z]+", code):
        await update.message.reply_text(
            "Қолданылуы: /add_code <латын әріптері> [сипаттама]\n"
            "Код латын әріптерінен тұруы керек (немесе оларға ұқсас кирилл әріптерінен: а, с, к, о, р ...)."
        )
        return
    desc = " ".join(context.args[1:])
    db.add_code(code, desc)
    await update.message.reply_text(f"✅ '{code}' коды сақталды.")


@admin_only
async def del_code_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Қолданылуы: /del_code <код>")
        return
    code = normalize_code(context.args[0])
    ok = db.delete_code(code)
    await update.message.reply_text(f"🗑 '{code}' коды өшірілді." if ok else "Мұндай код жоқ.")


# one "code — description" per line; separator may be —, –, -, :, = or just a space
BULK_LINE_RE = re.compile(rf"^\s*([{LETTERS}]+)\s*(?:[—–\-:=]\s*)?(.*?)\s*$")


@admin_only
async def add_codes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = (update.message.text or "").split(None, 1)  # drop "/add_codes"
    lines = [l for l in (parts[1] if len(parts) > 1 else "").splitlines() if l.strip()]
    if not lines:
        await update.message.reply_text(
            "Қолданылуы (әр жолға бір код):\n"
            "/add_codes\n"
            "ck — cevshen\n"
            "kk — Kuran\n"
            "rs — Risale"
        )
        return

    saved, errors = [], []
    for line in lines:
        m = BULK_LINE_RE.match(line)
        code = normalize_code(m.group(1)) if m else ""
        if not m or not re.fullmatch(r"[a-z]+", code):
            errors.append(line.strip())
            continue
        db.add_code(code, m.group(2))
        saved.append(f"<code>{esc(code)}</code> — {esc(m.group(2))}" if m.group(2) else f"<code>{esc(code)}</code>")

    msg = []
    if saved:
        msg.append(f"✅ Сақталды ({len(saved)}):\n" + "\n".join(saved))
    if errors:
        msg.append(f"❌ Қате жолдар ({len(errors)}):\n" + "\n".join(esc(e) for e in errors))
    await update.message.reply_text("\n\n".join(msg), parse_mode=ParseMode.HTML)


@admin_only
async def add_todo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Қолданылуы: /add_todo <мәтін>")
        return
    db.add_todo(text)
    await update.message.reply_text("✅ Тапсырма қосылды.\n\n" + todos_text(), parse_mode=ParseMode.HTML)


@admin_only
async def del_todo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Қолданылуы: /del_todo <нөмір>  (/todos қараңыз)")
        return
    removed = db.delete_todo_by_position(int(context.args[0]))
    if removed is None:
        await update.message.reply_text("Бұл нөмірмен тапсырма жоқ.")
        return
    await update.message.reply_text(
        f"🗑 Өшірілді: {esc(removed)}\n\n" + todos_text(), parse_mode=ParseMode.HTML
    )


@admin_only
async def clear_todos_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.clear_todos()
    await update.message.reply_text("🗑 Барлық тапсырмалар өшірілді.")


@admin_only
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = db.all_users()
    lines = [f"<b>Пайдаланушылар ({len(users)})</b>"]
    for uid, _, name in users:
        tag = " (админ)" if is_admin(uid) else ""
        lines.append(f"• {esc(name)} <code>{uid}</code>{tag}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------- notifications ----------

async def send_notification(context: ContextTypes.DEFAULT_TYPE):
    text = "🔔 <b>Күнделікті тапсырмалар</b>\n" + todos_text() + "\n\nЕсеп беру мысалы: <code>kk:10</code> · /codes"
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
            f"Қазіргі уақыттар: {current}\nҚолданылуы: /set_notification_at 10:00 18:00"
        )
        return
    times = []
    for arg in " ".join(context.args).replace(",", " ").split():
        m = TIME_RE.match(arg)
        if not m:
            await update.message.reply_text(f"Қате уақыт: '{arg}'. СС:ММ форматын қолданыңыз, мысалы 09:30")
            return
        times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    times = sorted(set(times))
    db.set_notify_times(times)
    schedule_notifications(context.application)
    await update.message.reply_text(f"✅ Еске салулар {', '.join(times)} уақытында жіберіледі ({TZ.key}).")


# ---------- daily report to group ----------

async def send_group_report(context: ContextTypes.DEFAULT_TYPE):
    chat_id = db.get_setting("report_group_id")
    if not chat_id:
        return
    try:
        await context.bot.send_message(int(chat_id), build_all_report("today"), parse_mode=ParseMode.HTML)
    except Exception as e:  # bot removed from group, group upgraded to supergroup, etc.
        log.warning("Failed to send group report to %s: %s", chat_id, e)


def schedule_group_report(app: Application):
    for job in app.job_queue.get_jobs_by_name(GROUP_REPORT_JOB):
        job.schedule_removal()
    t = db.get_setting("report_time", DEFAULT_GROUP_REPORT_TIME)
    h, m = map(int, t.split(":"))
    app.job_queue.run_daily(send_group_report, time=time(h, m, tzinfo=TZ), name=GROUP_REPORT_JOB)
    log.info("Group report scheduled at %s (group %s)", t, db.get_setting("report_group_id"))


@admin_only
async def set_report_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text(
            "Бұл команданы есеп жіберілетін топтың ішінде жазыңыз (алдымен ботты топқа қосыңыз)."
        )
        return
    db.set_setting("report_group_id", str(chat.id))
    t = db.get_setting("report_time", DEFAULT_GROUP_REPORT_TIME)
    await update.message.reply_text(
        f"✅ Күнделікті есеп осы топқа сағат {t}-де ({TZ.key}) жіберіледі."
    )


@admin_only
async def unset_report_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_setting("report_group_id", None)
    await update.message.reply_text("🗑 Топқа есеп жіберу тоқтатылды.")


@admin_only
async def set_report_at_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = TIME_RE.match(context.args[0]) if context.args else None
    if not m:
        current = db.get_setting("report_time", DEFAULT_GROUP_REPORT_TIME)
        await update.message.reply_text(
            f"Қазіргі уақыт: {current}\nҚолданылуы: /set_report_at 20:00"
        )
        return
    t = f"{int(m.group(1)):02d}:{m.group(2)}"
    db.set_setting("report_time", t)
    schedule_group_report(context.application)
    group = "орнатылған" if db.get_setting("report_group_id") else "әлі орнатылмаған (топта /set_report_group жазыңыз)"
    await update.message.reply_text(f"✅ Топқа есеп уақыты: {t} ({TZ.key}). Топ {group}.")


@admin_only
async def report_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.get_setting("report_group_id"):
        await update.message.reply_text("Топ орнатылмаған. Топтың ішінде /set_report_group жазыңыз.")
        return
    await send_group_report(context)
    await update.message.reply_text("✅ Есеп топқа жіберілді.")


@admin_only
async def notify_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_notification(context)
    await update.message.reply_text("✅ Еске салу жіберілді.")


# ---------- app ----------

USER_COMMANDS = [
    BotCommand("codes", "Қолжетімді кодтар тізімі"),
    BotCommand("todos", "Күнделікті тапсырмалар тізімі"),
    BotCommand("today", "Бүгінгі есеп (админ: /today all)"),
    BotCommand("week", "Осы аптаның есебі (админ: /week all)"),
    BotCommand("month", "Осы айдың есебі (админ: /month all)"),
    BotCommand("help", "Анықтама"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand("add_code", "Код қосу/өзгерту: /add_code kk Сипаттама"),
    BotCommand("add_codes", "Бірнеше код қосу: әр жолға «код — сипаттама»"),
    BotCommand("del_code", "Кодты өшіру: /del_code kk"),
    BotCommand("add_todo", "Күнделікті тапсырма қосу: /add_todo мәтін"),
    BotCommand("del_todo", "Тапсырманы нөмірі бойынша өшіру: /del_todo 2"),
    BotCommand("clear_todos", "Барлық тапсырмаларды өшіру"),
    BotCommand("set_notification_at", "Еске салу уақытын орнату: /set_notification_at 10:00 18:00"),
    BotCommand("notify_now", "Еске салуды барлығына қазір жіберу"),
    BotCommand("set_report_group", "Топта жазыңыз: күнделікті есеп осы топқа"),
    BotCommand("unset_report_group", "Топқа есеп жіберуді тоқтату"),
    BotCommand("set_report_at", "Топқа есеп уақыты: /set_report_at 20:00"),
    BotCommand("report_now", "Бүгінгі есепті топқа қазір жіберу"),
    BotCommand("users", "Тіркелген пайдаланушылар тізімі"),
]


async def set_admin_menu(bot, admin_id: int) -> bool:
    # In a private chat, chat_id == user_id. Fails if the admin hasn't started the bot yet.
    try:
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        return True
    except Exception as e:
        log.warning("Could not set admin menu for %s (send /start first): %s", admin_id, e)
        return False


async def post_init(app: Application):
    schedule_notifications(app)
    schedule_group_report(app)
    # Remove menus left by other backends: these scopes take priority over the default one.
    for scope in (BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats(),
                  BotCommandScopeAllChatAdministrators()):
        await app.bot.delete_my_commands(scope=scope)
    await app.bot.set_my_commands(USER_COMMANDS)  # default menu for everyone
    for admin_id in ADMIN_IDS:
        await set_admin_menu(app.bot, admin_id)


def main():
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty — nobody will have admin rights.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("codes", codes_cmd))
    app.add_handler(CommandHandler("todos", todos_cmd))
    for period in ("today", "week", "month"):
        app.add_handler(CommandHandler(period, make_report_handler(period)))

    app.add_handler(CommandHandler("add_code", add_code_cmd))
    app.add_handler(CommandHandler("add_codes", add_codes_cmd))
    app.add_handler(CommandHandler("del_code", del_code_cmd))
    app.add_handler(CommandHandler("add_todo", add_todo_cmd))
    app.add_handler(CommandHandler("del_todo", del_todo_cmd))
    app.add_handler(CommandHandler("clear_todos", clear_todos_cmd))
    app.add_handler(CommandHandler("set_notification_at", set_notification_cmd))
    app.add_handler(CommandHandler("notify_now", notify_now_cmd))
    app.add_handler(CommandHandler("set_report_group", set_report_group_cmd))
    app.add_handler(CommandHandler("unset_report_group", unset_report_group_cmd))
    app.add_handler(CommandHandler("set_report_at", set_report_at_cmd))
    app.add_handler(CommandHandler("report_now", report_now_cmd))
    app.add_handler(CommandHandler("users", users_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))

    log.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
