
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ChatMemberHandler,
)

# =======================
# Setup & configuration
# =======================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")  # e.g. @your_channel or numeric id
TZ = os.getenv("TZ", "Europe/Berlin")
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "9"))  # 24h, local TZ
DB_PATH = os.getenv("DB_PATH", "stats.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("presence-bot")


# =======================
# Storage (SQLite)
# =======================
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS daily_stats ("
            "  date TEXT PRIMARY KEY,"
            "  member_count INTEGER NOT NULL"
            ")"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS counters ("
            "  key TEXT PRIMARY KEY,"
            "  value INTEGER NOT NULL"
            ")"
        )
        for k in ("joins", "leaves"):
            con.execute("INSERT OR IGNORE INTO counters(key, value) VALUES (?, 0)", (k,))


def set_daily_count(date_str: str, count: int):
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("INSERT OR REPLACE INTO daily_stats(date, member_count) VALUES(?,?)", (date_str, count))


def get_last_two_days():
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute("SELECT date, member_count FROM daily_stats ORDER BY date DESC LIMIT 2")
        return cur.fetchall()


def bump_counter(key: str, delta: int):
    with closing(sqlite3.connect(DB_PATH)) as con, con:
        con.execute("UPDATE counters SET value = value + ? WHERE key = ?", (delta, key))


def read_counters():
    with closing(sqlite3.connect(DB_PATH)) as con:
        cur = con.execute("SELECT key, value FROM counters")
        return {k: v for k, v in cur.fetchall()}


# =======================
# Helpers
# =======================
async def resolve_chat_id(app: Application):
    # Accept both @username and numeric id from env.
    return CHANNEL_ID


async def fetch_member_count(context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = await resolve_chat_id(context.application)
    chat = await context.bot.get_chat(chat_id)
    count = await context.bot.get_chat_member_count(chat.id)
    return count


# =======================
# Commands
# =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != ADMIN_ID:
        return
    chat_id = await resolve_chat_id(context.application)
    try:
        chat = await context.bot.get_chat(chat_id)
        kind = "канал" if chat.type == ChatType.CHANNEL else ("группа" if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) else str(chat.type))
        await update.effective_message.reply_text(
            f"✅ Бот запущен.\nМониторинг: <b>{chat.title}</b> ({kind})\n"
            f"Отчёт ежедневно в {REPORT_HOUR:02d}:00 ({TZ}).",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Не удалось получить чат: {e}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != ADMIN_ID:
        return
    try:
        count = await fetch_member_count(context)
        rows = get_last_two_days()
        today = datetime.now(ZoneInfo(TZ)).date().isoformat()
        delta = None
        if rows and rows[0][0] == today:
            last_today = rows[0][1]
            prev = rows[1][1] if len(rows) > 1 else None
            delta = None if prev is None else last_today - prev
        msg = f"📊 Текущие участники: <b>{count}</b>"
        if delta is not None:
            sign = "➕" if delta > 0 else ("➖" if delta < 0 else "➖")
            msg += f"  ({sign}{abs(delta)} за день)"
        c = read_counters()
        if c:
            msg += f"\nJoins (события): {c.get('joins',0)}, Leaves: {c.get('leaves',0)}"
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.effective_message.reply_text(f"Ошибка: {e}")


# =======================
# Event handlers (groups only)
# =======================
async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cm = update.chat_member
        if not cm:
            return
        old_status = cm.old_chat_member.status
        new_status = cm.new_chat_member.status
        if str(old_status) in ("left", "kicked") and str(new_status) in ("member", "administrator"):
            bump_counter("joins", 1)
        if str(old_status) in ("member", "administrator") and str(new_status) in ("left", "kicked"):
            bump_counter("leaves", 1)
    except Exception as e:
        log.warning("chat_member handler error: %s", e)


# =======================
# Jobs
# =======================
async def daily_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        now = datetime.now(ZoneInfo(TZ))
        count = await fetch_member_count(context)
        set_daily_count(now.date().isoformat(), count)
        rows = get_last_two_days()
        delta = None
        if len(rows) >= 2 and rows[0][0] == now.date().isoformat():
            delta = rows[0][1] - rows[1][1]
        sign = "➕" if (delta or 0) > 0 else ("➖" if (delta or 0) < 0 else "➖")
        msg = (
            f"🗓️ {now:%Y-%m-%d} {REPORT_HOUR:02d}:00 {TZ}\n"
            f"👥 Участники: <b>{count}</b>"
        )
        if delta is not None:
            msg += f"  ({sign}{abs(delta)} за день)"
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.error("Daily job failed: %s", e)


def main():
    if not TOKEN or not CHANNEL_ID or not ADMIN_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN, CHANNEL_ID, ADMIN_ID in .env")

    init_db()

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Plan daily job using JobQueue
    application.job_queue.run_daily(
        daily_job,
        time=dtime(hour=REPORT_HOUR, tzinfo=ZoneInfo(TZ)),
        name="daily-stats",
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
