import asyncio
import hashlib
import html
import io
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
try:
    from aiogram.exceptions import TelegramMigrateToChat
except ImportError:  # дуже стара версія aiogram
    class TelegramMigrateToChat(TelegramAPIError):
        migrate_to_chat_id = None
from aiogram.filters import Command
from aiogram.types import BotCommand, BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from dotenv import load_dotenv

try:
    from card import THEMES, current_theme, render_overview_card, render_schedule_card
    _card_err = None
except Exception as _e:  # Pillow не встановлено або card.py відсутній
    render_schedule_card = render_overview_card = current_theme = THEMES = None
    _card_err = _e

load_dotenv(override=True)

# хостинги працюють у UTC, а графік — за київським часом
os.environ.setdefault("TZ", "Europe/Kyiv")
if hasattr(time, "tzset"):
    time.tzset()
USE_IMAGES = os.getenv("USE_IMAGES", "1") == "1" and render_schedule_card is not None

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_URL = os.getenv(
    "LOE_API_URL",
    "https://api.loe.lviv.ua/api/menus?page=1&type=photo-grafic&jsonld=1",
)
DB_PATH = os.getenv("DB_PATH", "loe_bot.db")  # на хостингу з диском вкажи шлях до тому, напр. /data/loe_bot.db
PORT = int(os.getenv("PORT", "0"))  # якщо хостинг дає PORT (Render/Koyeb) — піднімаємо health-сервер
CACHE_BUST = os.getenv("CACHE_BUST", "1") == "1"
GROUPS = ["1.1", "1.2", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5.1", "5.2", "6.1", "6.2"]

CHECK_INTERVAL = max(15, int(os.getenv("CHECK_INTERVAL", "30")))  # інтервал перевірки сайту, с (не менше 15)
REMINDER_MINUTES_BEFORE = int(os.getenv("REMINDER_MINUTES_BEFORE", "15"))  # за замовчуванням для нових чатів
LEAD_OPTIONS = (5, 10, 15)  # кнопки «за скільки хвилин сповіщати»
REMIND_END = os.getenv("REMIND_END", "1") == "1"            # нагадувати і про те, що світло скоро зʼявиться

# закріплення графіка в групових чатах
PIN_SCHEDULE = os.getenv("PIN_SCHEDULE", "1") == "1"        # закріплювати свіжий графік
REPLACE_OLD = os.getenv("REPLACE_OLD", "1") == "1"          # видаляти стару версію картинки цього ж дня
DELETE_EXPIRED = os.getenv("DELETE_EXPIRED", "1") == "1"    # видаляти (а не лише відкріплювати) вчорашні картинки

# щоденні зведення
DIGESTS = os.getenv("DIGESTS", "0") == "1"                 # за замовчуванням ВИМКНЕНО: публікуємо лише коли графік змінився на сайті
ROLLOVER_REFRESH = os.getenv("ROLLOVER_REFRESH", "1") == "1"  # тиха заміна закріплених картинок після півночі
MORNING_TIME = os.getenv("MORNING_TIME", "07:00")           # графік на сьогодні
EVENING_TIME = os.getenv("EVENING_TIME", "20:00")           # графік на завтра

# моніторинг і діагностика
HEARTBEAT_MINUTES = max(1, int(os.getenv("HEARTBEAT_MINUTES", "5")))  # як часто писати в лог «перевірка ок», хв
LOG_EVERY_CHECK = os.getenv("LOG_EVERY_CHECK", "0") == "1"           # 1 — писати в лог кожну перевірку (шумно)
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)                        # твій Telegram id: алерти про збої/зміни сайту
STATUS_REFRESH_MINUTES = int(os.getenv("STATUS_REFRESH_MINUTES", "30"))  # тихо оновлювати закріплені графіки («зараз без світла…»); 0 — вимкнути
ALERT_AFTER_ERRORS = 5                                                 # скільки збійних перевірок поспіль до алерта

# резервна копія бази в Telegram (для хостингів без постійного диска, напр. безкоштовний Render)
BACKUP_NAME = "loe_bot_backup.db"
BACKUP_ENABLED = bool(OWNER_ID) and os.getenv("BACKUP", "1") == "1"  # потрібен OWNER_ID

WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
if _card_err:
    logging.warning(f"Картинки вимкнено: {_card_err} (встанови: python -m pip install pillow)")
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

STATUS = {
    "started": datetime.now(),
    "checks": 0, "errors": 0, "consecutive_errors": 0,
    "last_ok": None, "last_error": None, "last_error_text": "",
    "last_change": None, "groups": 0, "days": [],
    "last_beat": None, "beat_checks": 0,
}

BOT_COMMANDS = [
    ("start", "Обрати чергу / налаштувати чат"),
    ("settings", "Налаштування: черга, сповіщення, закріплення"),
    ("today", "Графік на сьогодні"),
    ("tomorrow", "Графік на завтра"),
    ("now", "Поточний графік"),
    ("all", "Зведена картинка по всіх чергах"),
    ("next", "Найближче вимкнення"),
    ("pin", "Оновити і закріпити графік (у групі)"),
    ("status", "Стан бота і перевірок сайту ЛОЕ"),
    ("stop", "Відписатись від сповіщень"),
    ("help", "Довідка"),
]


# ---------------------------------------------------------------- DB

def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                chat_type TEXT NOT NULL,
                group_name TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                group_name TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pinned_messages (
                chat_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, day)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sent_reminders (
                reminder_key TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                notify INTEGER NOT NULL DEFAULT 1,
                lead INTEGER NOT NULL DEFAULT 15,
                pin INTEGER NOT NULL DEFAULT 1,
                notify_end INTEGER NOT NULL DEFAULT 1
            )
        """)
        # додаткові черги для особистих чатів (кілька черг на одного користувача)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_groups (
                chat_id INTEGER NOT NULL,
                group_name TEXT NOT NULL,
                PRIMARY KEY (chat_id, group_name)
            )
        """)
        # що змінилось у закріпленій картинці — щоб фонове оновлення не стирало цей блок
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pin_notes (
                chat_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                kind TEXT NOT NULL,
                changes_json TEXT NOT NULL,
                PRIMARY KEY (chat_id, day)
            )
        """)
        # міграція зі старої бази: нова колонка «нагадувати про ввімкнення» (спершу = старе notify)
        cols = [r[1] for r in cur.execute("PRAGMA table_info(chat_settings)").fetchall()]
        if "notify_end" not in cols:
            cur.execute("ALTER TABLE chat_settings ADD COLUMN notify_end INTEGER NOT NULL DEFAULT 1")
            cur.execute("UPDATE chat_settings SET notify_end = notify")


def set_subscription(chat_id: int, chat_type: str, group_name: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO subscribers (chat_id, chat_type, group_name) VALUES (?, ?, ?)",
            (chat_id, chat_type, group_name),
        )


def count_subscribers() -> tuple[int, int]:
    """(особисті, групові)"""
    with db() as conn:
        p = conn.execute("SELECT COUNT(*) FROM subscribers WHERE chat_type = 'private'").fetchone()[0]
        g = conn.execute("SELECT COUNT(*) FROM subscribers WHERE chat_type != 'private'").fetchone()[0]
    return p, g


def migrate_chat(old_id: int, new_id: int):
    """Група стала супергрупою (id змінився): переносимо підписку та налаштування.
    Закріплені повідомлення не переносимо — у новому чаті в них інші id."""
    if not new_id or old_id == new_id:
        return
    with db() as conn:
        conn.execute("DELETE FROM pinned_messages WHERE chat_id = ?", (old_id,))
        for table in ("subscribers", "chat_settings"):
            conn.execute(f"UPDATE OR REPLACE {table} SET chat_id = ? WHERE chat_id = ?", (new_id, old_id))
    logging.info(f"Чат {old_id} перенесено в {new_id} (група → супергрупа)")


def remove_subscription(chat_id: int):
    with db() as conn:
        conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM pinned_messages WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chat_groups WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM pin_notes WHERE chat_id = ?", (chat_id,))


def get_chat_groups(chat_id: int) -> list[str]:
    """Обрані черги особистого чату (у порядку GROUPS)."""
    with db() as conn:
        rows = conn.execute("SELECT group_name FROM chat_groups WHERE chat_id = ?", (chat_id,)).fetchall()
    chosen = {r[0] for r in rows}
    if not chosen:
        sub = get_subscription(chat_id)
        if sub and sub[1] in GROUPS:
            return [sub[1]]
        return []
    return [g for g in GROUPS if g in chosen]


def toggle_chat_group(chat_id: int, chat_type: str, grp: str) -> list[str]:
    """Додає/прибирає чергу в особистому чаті. Повертає оновлений список (порожній — відписано)."""
    current = get_chat_groups(chat_id)
    if grp in current:
        current.remove(grp)
    else:
        current.append(grp)
    current = [g for g in GROUPS if g in current]
    with db() as conn:
        conn.execute("DELETE FROM chat_groups WHERE chat_id = ?", (chat_id,))
        conn.executemany("INSERT INTO chat_groups (chat_id, group_name) VALUES (?, ?)", [(chat_id, g) for g in current])
        if current:
            conn.execute(
                "INSERT OR REPLACE INTO subscribers (chat_id, chat_type, group_name) VALUES (?, ?, ?)",
                (chat_id, chat_type, current[0]),
            )
        else:
            conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
    return current


def set_pin_note(chat_id: int, day: str, kind: str, changes: list[str]):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pin_notes (chat_id, day, kind, changes_json) VALUES (?, ?, ?, ?)",
            (chat_id, day, kind, json.dumps(changes, ensure_ascii=False)),
        )


def get_pin_note(chat_id: int, day: str) -> tuple[str, list[str]] | None:
    with db() as conn:
        row = conn.execute(
            "SELECT kind, changes_json FROM pin_notes WHERE chat_id = ? AND day = ?", (chat_id, day)
        ).fetchone()
    return (row[0], json.loads(row[1])) if row else None


def delete_pin_note(chat_id: int, day: str):
    with db() as conn:
        conn.execute("DELETE FROM pin_notes WHERE chat_id = ? AND day = ?", (chat_id, day))


def get_subscription(chat_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT chat_type, group_name FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def get_subscribers_by_group(group_name: str):
    """Чати, підписані на чергу (включно з особистими, де обрано кілька черг)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT chat_id, chat_type FROM subscribers WHERE group_name = ?", (group_name,)
        ).fetchall()
        extra = conn.execute(
            "SELECT s.chat_id, s.chat_type FROM chat_groups g JOIN subscribers s ON s.chat_id = g.chat_id "
            "WHERE g.group_name = ?", (group_name,)
        ).fetchall()
    return list(dict.fromkeys(rows + extra))


def get_group_chats():
    """Усі групові чати: [(chat_id, bound_group_name)]"""
    with db() as conn:
        return conn.execute(
            "SELECT chat_id, group_name FROM subscribers WHERE chat_type != 'private'"
        ).fetchall()


def get_private_subscribers():
    """Особисті чати: [(chat_id, group_name)]"""
    with db() as conn:
        return conn.execute(
            "SELECT chat_id, group_name FROM subscribers WHERE chat_type = 'private'"
        ).fetchall()


def get_all_saved() -> dict:
    result = {}
    for g in GROUPS:
        data = get_saved_schedule(g)
        if data:
            result[g] = data
    return result


def get_pin(chat_id: int, day: str) -> int | None:
    with db() as conn:
        row = conn.execute(
            "SELECT message_id FROM pinned_messages WHERE chat_id = ? AND day = ?", (chat_id, day)
        ).fetchone()
    return row[0] if row else None


def set_pin(chat_id: int, day: str, message_id: int):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pinned_messages (chat_id, day, message_id) VALUES (?, ?, ?)",
            (chat_id, day, message_id),
        )


def delete_pin_row(chat_id: int, day: str):
    with db() as conn:
        conn.execute("DELETE FROM pinned_messages WHERE chat_id = ? AND day = ?", (chat_id, day))
        conn.execute("DELETE FROM pin_notes WHERE chat_id = ? AND day = ?", (chat_id, day))


def get_all_pins() -> list[tuple[int, str, int]]:
    with db() as conn:
        return conn.execute("SELECT chat_id, day, message_id FROM pinned_messages").fetchall()


def get_chat_pins(chat_id: int) -> list[tuple[str, int]]:
    with db() as conn:
        return conn.execute(
            "SELECT day, message_id FROM pinned_messages WHERE chat_id = ?", (chat_id,)
        ).fetchall()


def get_settings(chat_id: int) -> dict:
    """Налаштування чату: нагадування перед вимкненням, перед ввімкненням, за скільки хвилин, закріплення."""
    with db() as conn:
        row = conn.execute(
            "SELECT notify, lead, pin, notify_end FROM chat_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    if not row:
        return {"notify": True, "notify_end": True, "lead": REMINDER_MINUTES_BEFORE, "pin": True}
    return {"notify": bool(row[0]), "notify_end": bool(row[3]), "lead": int(row[1]), "pin": bool(row[2])}


def update_setting(chat_id: int, **changes):
    cfg = get_settings(chat_id)
    cfg.update(changes)
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_settings (chat_id, notify, lead, pin, notify_end) VALUES (?, ?, ?, ?, ?)",
            (chat_id, int(cfg["notify"]), int(cfg["lead"]), int(cfg["pin"]), int(cfg["notify_end"])),
        )


def get_saved_schedule(group_name: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT data_json FROM schedules WHERE group_name = ?", (group_name,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def save_schedule(group_name: str, data: dict):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO schedules (group_name, data_json, updated_at) VALUES (?, ?, ?)",
            (group_name, json.dumps(data, ensure_ascii=False), datetime.now().strftime("%d.%m.%Y %H:%M")),
        )


def was_reminder_sent(reminder_key: str) -> bool:
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM sent_reminders WHERE reminder_key = ?", (reminder_key,)
        ).fetchone() is not None


def mark_reminder_sent(reminder_key: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sent_reminders (reminder_key, sent_at) VALUES (?, ?)",
            (reminder_key, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def cleanup_old_reminders(days: int = 3):
    border = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as conn:
        conn.execute("DELETE FROM sent_reminders WHERE sent_at < ?", (border,))


# ---------------------------------------------------------------- Parsing

def parse_intervals(raw_text: str) -> list[str]:
    pairs = re.findall(r"(\d{1,2}:\d{2})\s*(?:до|-|–|—)\s*(\d{1,2}:\d{2})", raw_text)
    intervals = []
    for start, end in pairs:
        s_h, s_m = map(int, start.split(":"))
        e_h, e_m = map(int, end.split(":"))
        intervals.append(f"{s_h:02d}:{s_m:02d}–{e_h:02d}:{e_m:02d}")
    return sorted(dict.fromkeys(intervals))


def calc_duration_minutes(intervals: list[str]) -> int:
    total = 0
    for item in intervals:
        start_str, end_str = item.split("–")
        s_h, s_m = map(int, start_str.split(":"))
        e_h, e_m = map(int, end_str.split(":"))
        start_min = s_h * 60 + s_m
        end_min = e_h * 60 + e_m
        if end_min == 0:
            end_min = 24 * 60
        if end_min > start_min:
            total += end_min - start_min
    return total


def format_duration(minutes: int) -> str:
    hours = minutes // 60
    mins = minutes % 60
    if hours and mins:
        return f"{hours} год {mins} хв"
    if hours:
        return f"{hours} год"
    return f"{mins} хв"


def extract_all_groups(body_text: str) -> dict[str, dict]:
    result = {}
    date_blocks = re.split(r"(Графік погодинних відключень на\s+\d{2}\.\d{2}\.\d{4})", body_text)

    if len(date_blocks) > 1:
        sections = []
        for i in range(1, len(date_blocks), 2):
            header = date_blocks[i].strip()
            content = date_blocks[i + 1] if i + 1 < len(date_blocks) else ""
            date_match = re.search(r"\d{2}\.\d{2}\.\d{4}", header)
            day_label = date_match.group(0) if date_match else header
            sections.append((day_label, content))
    else:
        sections = [("Актуальний графік", body_text)]

    for grp in GROUPS:
        days_data = {}
        for day_label, content in sections:
            pattern = rf"(Група\s+{re.escape(grp)}[^\n]*[\s\S]*?)(?=Група\s+\d+\.\d+|Графік погодинних|$)"
            match = re.search(pattern, content)
            if match:
                block = match.group(1).strip()
                # зберігаємо лише інтервали: "raw" міг змінюватись без реальних змін графіка
                days_data[day_label] = {"intervals": parse_intervals(block)}
        if days_data:
            result[grp] = {"days": days_data}

    return result


# ---------------------------------------------------------------- Дати та інтервали

def parse_day(label: str) -> date | None:
    try:
        return datetime.strptime(label, "%d.%m.%Y").date()
    except ValueError:
        return None


def day_rel(label: str) -> str:
    d = parse_day(label)
    if not d:
        return ""
    return {0: "сьогодні", 1: "завтра"}.get((d - date.today()).days, "")


def pretty_day(label: str) -> str:
    """'сьогодні (вівторок 24.11)' / 'середа 25.11' / як є, якщо не дата."""
    d = parse_day(label)
    if not d:
        return label
    base = f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"
    rel = day_rel(label)
    return f"{rel} ({base})" if rel else base


def is_actual(label: str) -> bool:
    d = parse_day(label)
    return d is None or d >= date.today()


def sorted_days(days) -> list[str]:
    def key(label):
        try:
            return datetime.strptime(label, "%d.%m.%Y")
        except ValueError:
            return datetime.max
    return sorted(days, key=key)


def upcoming_days(all_days, limit: int = 2) -> list[str]:
    """Сьогодні та завтра (дати порівнюються як дати, а не як рядки).
    Минулих днів не повертає: якщо актуального графіка нема — порожній список (мовчимо)."""
    return [d for d in sorted_days(all_days) if is_actual(d)][:limit]


def resolve_day(snapshot: dict, offset: int) -> str | None:
    """Мітка дня для /today (0) і /tomorrow (1); None — якщо ще не опубліковано."""
    all_days = {d for v in snapshot.values() for d in v.get("days", {})}
    target = (date.today() + timedelta(days=offset)).strftime("%d.%m.%Y")
    if target in all_days:
        return target
    undated = [d for d in all_days if parse_day(d) is None]
    return undated[0] if undated and offset == 0 else None


def iv_bounds(iv: str) -> tuple[int, int] | None:
    try:
        a, b = iv.split("–")
        s_h, s_m = map(int, a.split(":"))
        e_h, e_m = map(int, b.split(":"))
    except ValueError:
        return None
    s, e = s_h * 60 + s_m, e_h * 60 + e_m
    if e == 0:
        e = 1440
    return (s, e) if e > s else None


def outage_spans(data: dict) -> list[tuple[datetime, datetime]]:
    """Усі вимкнення черги як (початок, кінець); сусідні (22:00–00:00 + 00:00–02:00) зливаються."""
    spans = []
    for day_label, info in data.get("days", {}).items():
        d0 = parse_day(day_label)
        if not d0:
            continue
        base = datetime.combine(d0, datetime.min.time())
        for iv in info.get("intervals", []):
            b = iv_bounds(iv)
            if b:
                spans.append((base + timedelta(minutes=b[0]), base + timedelta(minutes=b[1])))
    spans.sort()
    merged: list[tuple[datetime, datetime]] = []
    for st, en in spans:
        if merged and st <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], en))
        else:
            merged.append((st, en))
    return merged


def current_span(data: dict, now: datetime) -> tuple[datetime, datetime] | None:
    for st, en in outage_spans(data):
        if st <= now < en:
            return st, en
    return None


# ---------------------------------------------------------------- Messages

def build_diff_summary(old_data: dict | None, new_data: dict) -> list[str]:
    if not old_data:
        return ["Перше завантаження графіка в базу."]

    changes = []
    old_days = old_data.get("days", {})
    new_days = new_data.get("days", {})

    for day, info in new_days.items():
        new_set = set(info.get("intervals", []))
        if day not in old_days:
            changes.append(f"📅 Додано графік на <b>{day}</b>.")
            continue

        old_set = set(old_days[day].get("intervals", []))
        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)

        if added:
            changes.append(f"➕ <b>{day}</b> — додано вимкнення: <code>{', '.join(added)}</code>")
        if removed:
            changes.append(f"➖ <b>{day}</b> — скасовано вимкнення: <code>{', '.join(removed)}</code>")

    return changes


def format_personal_message(group_name: str, data: dict, is_update: bool = False) -> str:
    title = (
        f"⚡️ <b>Оновлення графіка для черги {group_name}</b>\n"
        if is_update
        else f"💡 <b>Графік для черги {group_name}</b>\n"
    )
    lines = [title]

    for day, info in data.get("days", {}).items():
        if not is_actual(day):
            continue
        lines.append(f"📅 <b>{pretty_day(day)}</b>")
        intervals = info.get("intervals", [])
        if intervals:
            for iv in intervals:
                lines.append(f"  • ❌ <code>{iv}</code>")
        else:
            lines.append("  • ✅ Відключень не заплановано")
        lines.append("")

    return "\n".join(lines).strip()


def format_chat_message(group_name: str, new_data: dict, old_data: dict | None = None, is_update: bool = False) -> str:
    header = (
        f"🚨 <b>ОНОВЛЕННЯ ГРАФІКА | Черга {group_name}</b>\n"
        if is_update
        else f"📊 <b>Зведення по світлу | Черга {group_name}</b>\n"
    )
    lines = [header]

    if is_update:
        diffs = build_diff_summary(old_data, new_data)
        if diffs:
            lines.append("🔄 <b>Що змінилось:</b>")
            lines.extend(diffs)
            lines.append("")

    for day, info in new_data.get("days", {}).items():
        if not is_actual(day):
            continue
        intervals = info.get("intervals", [])
        count = len(intervals)
        total_min = calc_duration_minutes(intervals)

        lines.append(f"📅 <b>{day}</b>")
        if count > 0:
            lines.append(f"🔢 Всього відключень: <b>{count}</b> (разом <b>{format_duration(total_min)}</b> без світла)")
            for iv in intervals:
                lines.append(f"  • ❌ <code>{iv}</code>")
        else:
            lines.append("✅ Відключень не заплановано (0 вимкнень)")
        lines.append("")

    return "\n".join(lines).strip()


def get_groups_keyboard(is_private: bool = True, current: str | list | None = None, back: bool = False) -> InlineKeyboardMarkup:
    """Вибір черги. current — поточна або список обраних (позначаються ✅); back — «Назад» замість «Налаштування»."""
    chosen = set(current) if isinstance(current, (list, set, tuple)) else ({current} if current else set())
    rows = []
    for i in range(0, len(GROUPS), 4):
        rows.append([
            InlineKeyboardButton(
                text=f"{'✅ ' if g in chosen else ''}Черга {g}", callback_data=f"set_grp:{g}"
            )
            for g in GROUPS[i:i + 4]
        ])
    current = "all" if "all" in chosen else None
    if not is_private:
        rows.append([InlineKeyboardButton(
            text=f"{'✅ ' if current == 'all' else ''}📊 Усі черги разом", callback_data="set_grp:all"
        )])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data="cfg:back")
        if back
        else InlineKeyboardButton(text="⚙️ Налаштування сповіщень", callback_data="cfg:open")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def next_outage_text(group_name: str, data: dict) -> str | None:
    """Найближче вимкнення після поточного часу."""
    now = datetime.now()
    for st, _ in outage_spans(data):
        if st > now:
            left = int((st - now).total_seconds() // 60)
            return (
                f"⏭ Найближче вимкнення: <b>{st.strftime('%d.%m')}</b> "
                f"<code>{st.strftime('%H:%M')}</code> (через {format_duration(left)})"
            )
    return None


def diff_for_day(old_data: dict | None, new_data: dict, day: str) -> list[str]:
    old_set = set(((old_data or {}).get("days", {}).get(day) or {}).get("intervals", []))
    new_set = set((new_data.get("days", {}).get(day) or {}).get("intervals", []))
    lines = []
    if new_set - old_set:
        lines.append("➕ Додано: " + ", ".join(f"<code>{iv}</code>" for iv in sorted(new_set - old_set)))
    if old_set - new_set:
        lines.append("➖ Скасовано: " + ", ".join(f"<code>{iv}</code>" for iv in sorted(old_set - new_set)))
    return lines


def format_day_text(grp: str, data: dict, day: str, heading: str) -> str:
    """Графік однієї черги на один день (для /today, /tomorrow і щоденних зведень)."""
    ivs = (data.get("days", {}).get(day) or {}).get("intervals", [])
    lines = [f"{heading}\n", f"📅 <b>{pretty_day(day)}</b>"]
    if ivs:
        for iv in ivs:
            lines.append(f"  • ❌ <code>{iv}</code>")
        lines.append(f"⏱ Без світла загалом: <b>{format_duration(calc_duration_minutes(ivs))}</b>")
    else:
        lines.append("  • ✅ Відключень не заплановано")
    if day_rel(day) == "сьогодні":
        now = datetime.now()
        cur = current_span(data, now)
        if cur:
            lines.append(f"🔴 Зараз світла немає — очікується о <b>{cur[1].strftime('%H:%M')}</b>")
        nxt = next_outage_text(grp, data)
        if nxt:
            lines.append(nxt)
    return "\n".join(lines)


def format_personal_update(grp: str, new_data: dict, old_data: dict | None, days: list[str], new_days: set) -> str:
    only_new = all(d in new_days for d in days)
    title = (
        f"🆕 <b>Опубліковано новий графік · черга {grp}</b>"
        if only_new
        else f"⚡️ <b>Оновлення графіка · черга {grp}</b>"
    )
    lines = [title, ""]
    for day in days:
        ivs = (new_data.get("days", {}).get(day) or {}).get("intervals", [])
        lines.append(f"📅 <b>{pretty_day(day)}</b>")
        if day not in new_days:
            lines.extend(diff_for_day(old_data, new_data, day))
        if ivs:
            label = "Відключення" if day in new_days else "Тепер"
            lines.append(f"{label}: " + ", ".join(f"<code>{iv}</code>" for iv in ivs))
            lines.append(f"⏱ Без світла: <b>{format_duration(calc_duration_minutes(ivs))}</b>")
        else:
            lines.append("✅ Відключень не заплановано")
        lines.append("")
    nxt = next_outage_text(grp, new_data)
    if nxt:
        lines.append(nxt)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- Підписи до картинок

def fit_caption(lines: list[str], limit: int = 1000) -> str:
    """Додає рядки, доки влазить у ліміт підпису (HTML-теги не рвуться посередині)."""
    out, size = [], 0
    for ln in lines:
        if size + len(ln) + 1 > limit:
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out).strip()


def compact_changes(changed: set, olds: dict, snapshot: dict, day: str, limit: int = 8) -> list[str]:
    lines = []
    for g in GROUPS:
        if g not in changed:
            continue
        parts = diff_for_day(olds.get(g), snapshot.get(g, {}), day)
        if parts:
            lines.append(f"<b>{g}</b>: " + "  ".join(p.replace("Додано: ", "").replace("Скасовано: ", "") for p in parts))
    if len(lines) > limit:
        extra = len(lines) - limit
        lines = lines[:limit] + [f"…та ще {extra} черг(и)"]
    return lines


def status_lines(snapshot: dict, day: str) -> list[str]:
    lines = []
    free = [
        g for g in GROUPS
        if g in snapshot and day in snapshot[g].get("days", {})
        and not snapshot[g]["days"][day].get("intervals")
    ]
    if free:
        lines.append("✅ Без відключень: " + ", ".join(free))
    if day_rel(day) == "сьогодні":
        now = datetime.now()
        off = []
        for g in GROUPS:
            if g in snapshot:
                cur = current_span(snapshot[g], now)
                if cur:
                    off.append(f"{g} (до {cur[1].strftime('%H:%M')})")
        lines.append("🔴 Зараз без світла: " + ", ".join(off) if off else "🟢 Зараз світло є в усіх чергах")
    totals = {
        g: calc_duration_minutes(snapshot[g]["days"][day].get("intervals", []))
        for g in GROUPS if g in snapshot and day in snapshot[g].get("days", {})
    }
    if totals and max(totals.values()) > 0:
        top = max(totals.values())
        worst = ", ".join(g for g in GROUPS if totals.get(g) == top)  # усі черги з однаковим максимумом
        lines.append(f"⏱ Найбільше без світла: <b>{worst}</b> — {format_duration(top)}")
    return lines


def focus_lines(grp: str | None, snapshot: dict, day: str) -> list[str]:
    if not grp or grp not in snapshot:
        return []
    ivs = (snapshot[grp].get("days", {}).get(day) or {}).get("intervals")
    if ivs is None:
        return []
    if ivs:
        text = ", ".join(f"<code>{iv}</code>" for iv in ivs)
        lines = [f"👉 <b>Черга {grp}:</b> ❌ {text} · {format_duration(calc_duration_minutes(ivs))} без світла"]
    else:
        lines = [f"👉 <b>Черга {grp}:</b> ✅ без відключень"]
    if day_rel(day) == "сьогодні":
        nxt = next_outage_text(grp, snapshot[grp])
        if nxt:
            lines.append(nxt)
    return lines


CAPTION_TITLES = {
    "update": "🔔 <b>Оновлення графіка</b>",
    "new": "🆕 <b>Опубліковано графік</b>",
    "morning": "☀️ <b>Доброго ранку! Графік на сьогодні</b>",
    "evening": "🌙 <b>Графік на завтра</b>",
}
SEASON_EMOJI = {"winter": "🎄", "spring": "🌷", "summer": "☀️", "autumn": "🍂"}


def season_emoji() -> str:
    """Значок у підписі за сезоном (той самий, що й тема картинки)."""
    try:
        return SEASON_EMOJI.get(current_theme(), "💡") if current_theme else "💡"
    except Exception:
        return "💡"


def build_caption(snapshot: dict, day: str, focus: str | None = None, changed=None, olds=None,
                  kind: str | None = None, saved_changes: list[str] | None = None) -> str:
    """Текст під картинкою: що змінилось, ваша черга, хто зараз без світла.
    saved_changes — збережений блок «що змінилось» (щоб фонове оновлення його не стирало)."""
    title = CAPTION_TITLES.get(kind) or f"{season_emoji()} <b>Графік</b>"
    head = f"{title} · {pretty_day(day)}"
    lines = [head]
    lines += focus_lines(focus, snapshot, day)
    if kind == "update" and (changed or saved_changes):
        ch = saved_changes if saved_changes else compact_changes(set(changed), olds or {}, snapshot, day)
        if ch:
            lines += ["", "🔄 <b>Що змінилось:</b>"] + ch
    st = status_lines(snapshot, day)
    if st:
        lines += [""] + st
    return fit_caption(lines)


async def send_msg(chat_id: int, png: bytes | None, text: str, silent: bool = False):
    """Надсилає фото з підписом (або текст). Повертає Message або None."""
    for _ in range(2):
        try:
            if png:
                return await bot.send_photo(
                    chat_id, BufferedInputFile(png, filename="schedule.png"), caption=text, parse_mode="HTML",
                    disable_notification=silent,
                )
            return await bot.send_message(chat_id, text, parse_mode="HTML", disable_notification=silent)
        except TelegramForbiddenError:
            logging.info(f"Чат {chat_id} заблокував бота / бота видалено, видаляю підписку.")
            remove_subscription(chat_id)
            return None
        except TelegramMigrateToChat as e:
            new_id = getattr(e, "migrate_to_chat_id", None)
            if not new_id:
                return None
            migrate_chat(chat_id, new_id)
            chat_id = new_id  # повторюємо вже в новий чат
        except TelegramRetryAfter as e:
            logging.warning(f"Flood control при надсиланні в {chat_id}, чекаю {e.retry_after}с")
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            logging.error(f"Помилка надсилання в {chat_id}: {e}")
            return None
    return None


async def safe_send(chat_id: int, text: str) -> bool:
    return await send_msg(chat_id, None, text) is not None


async def safe_send_photo(chat_id: int, png: bytes, caption: str) -> bool:
    return await send_msg(chat_id, png, caption) is not None


async def track_and_pin(chat_id: int, day: str, new_id: int, pin: bool):
    """Запам'ятовує свіжу картинку дня, за потреби закріплює її, а стару (цього ж дня) відкріплює й видаляє."""
    old_id = get_pin(chat_id, day)
    delete_pin_note(chat_id, day)  # нове повідомлення — старий блок «що змінилось» неактуальний
    if pin and PIN_SCHEDULE and not get_chat_pins(chat_id):
        await cleanup_orphan_pins(chat_id)  # база могла загубитись (деплой без диска) — прибираємо старі закріплення бота
    if pin:
        try:
            await bot.pin_chat_message(chat_id, new_id, disable_notification=True)
        except TelegramAPIError as e:
            logging.warning(f"Не вдалось закріпити в {chat_id}: {e} (потрібні права адміна «Закріплення повідомлень»)")
    set_pin(chat_id, day, new_id)
    if old_id and old_id != new_id:
        if PIN_SCHEDULE:
            try:
                await bot.unpin_chat_message(chat_id, message_id=old_id)
            except TelegramAPIError:
                pass
        if REPLACE_OLD:
            try:
                await bot.delete_message(chat_id, old_id)
            except TelegramAPIError as e:
                logging.info(f"Не вдалось видалити стару картинку {old_id} в {chat_id}: {e}")


async def publish(chat_id: int, day: str, png: bytes | None, text: str,
                  managed: bool = False, silent: bool = False, force_pin: bool = False) -> bool:
    """Надсилає картинку/текст на день.
    managed=True — повідомлення «кероване»: запам'ятовуємо його, стару версію цього дня видаляємо,
    закріплюємо, якщо в чаті ввімкнено закріплення (force_pin — закріпити незалежно від налаштування)."""
    msg = await send_msg(chat_id, png, text, silent=silent)
    if msg is None:
        return False
    chat_id = msg.chat.id  # міг змінитись після міграції групи в супергрупу
    if managed or force_pin:
        pin = PIN_SCHEDULE and (force_pin or get_settings(chat_id)["pin"])
        await track_and_pin(chat_id, day, msg.message_id, pin)
    return True


async def cleanup_orphan_pins(chat_id: int, limit: int = 6):
    """Якщо бот не пам'ятає своїх закріплень (нова/стерта база), але в чаті ще висять його старі графіки —
    відкріплює (і видаляє) їх. Чужі закріплення (не від бота) не чіпає: зупиняється на першому такому."""
    try:
        me = await bot.me()
    except TelegramAPIError:
        return
    keep = {mid for _, mid in get_chat_pins(chat_id)}
    for _ in range(limit):
        try:
            chat = await bot.get_chat(chat_id)
        except TelegramAPIError:
            return
        pm = getattr(chat, "pinned_message", None)
        if not pm or not pm.from_user or pm.from_user.id != me.id or pm.message_id in keep:
            return
        try:
            await bot.unpin_chat_message(chat_id, message_id=pm.message_id)
        except TelegramAPIError:
            return
        if REPLACE_OLD:
            try:
                await bot.delete_message(chat_id, pm.message_id)
            except TelegramAPIError:
                pass


async def release_chat_pins(chat_id: int):
    """Відписка від чату: відкріплює (і за DELETE_EXPIRED видаляє) графіки бота, щоб вони не висіли вічно."""
    for day, msg_id in get_chat_pins(chat_id):
        try:
            await bot.unpin_chat_message(chat_id, message_id=msg_id)
        except TelegramAPIError:
            pass
        if DELETE_EXPIRED:
            try:
                await bot.delete_message(chat_id, msg_id)
            except TelegramAPIError:
                pass
        delete_pin_row(chat_id, day)


async def apply_pin_setting(chat_id: int, enable: bool):
    """Кнопка «Закріплювати»: одразу закріплює/відкріплює актуальні картинки цього чату."""
    if not PIN_SCHEDULE:
        return
    for day, msg_id in get_chat_pins(chat_id):
        d = parse_day(day)
        if d is not None and d < date.today():
            continue
        try:
            if enable:
                await bot.pin_chat_message(chat_id, msg_id, disable_notification=True)
            else:
                await bot.unpin_chat_message(chat_id, message_id=msg_id)
        except TelegramAPIError as e:
            logging.info(f"Не вдалось змінити закріплення {msg_id} в {chat_id}: {e}")


async def expire_old_pins():
    """Відкріплює картинки за минулі дні."""
    for chat_id, day, msg_id in get_all_pins():
        d = parse_day(day)
        if d is None or d >= date.today():
            continue
        try:
            await bot.unpin_chat_message(chat_id, message_id=msg_id)
        except TelegramAPIError:
            pass
        if DELETE_EXPIRED:
            try:
                await bot.delete_message(chat_id, msg_id)
            except TelegramAPIError:
                pass
        delete_pin_row(chat_id, day)


async def pin_hint(chat_id: int) -> str:
    """Підказка в групі, якщо боту бракує прав закріплювати / видаляти старі версії."""
    if not PIN_SCHEDULE:
        return ""
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id, me.id)
    except TelegramAPIError:
        return ""
    is_admin = member.status in ("administrator", "creator")
    missing = []
    if not is_admin or not (member.status == "creator" or getattr(member, "can_pin_messages", False)):
        missing.append("«Закріплення повідомлень»")
    if REPLACE_OLD and (not is_admin or not (member.status == "creator" or getattr(member, "can_delete_messages", False))):
        missing.append("«Видалення повідомлень» (щоб прибирати старі версії)")
    if not missing:
        return ""
    who = "зроби мене адміном із правами" if not is_admin else "додай мені права"
    return "\n\n📌 Щоб я закріплював свіжий графік, " + who + " " + " і ".join(missing) + "."


async def make_card(group_name: str, data: dict, is_update: bool) -> bytes | None:
    """Малює картинку в окремому потоці, щоб не блокувати бота."""
    if not USE_IMAGES:
        return None
    try:
        return await asyncio.to_thread(render_schedule_card, group_name, data, is_update)
    except Exception as e:
        logging.error(f"Не вдалось намалювати картинку для черги {group_name}: {e}")
        return None


async def make_overview(all_data: dict, day: str, changed: set, is_update: bool) -> bytes | None:
    if not USE_IMAGES:
        return None
    try:
        return await asyncio.to_thread(render_overview_card, all_data, day, changed, is_update)
    except Exception as e:
        logging.error(f"Не вдалось намалювати зведену картинку на {day}: {e}")
        return None


async def broadcast_to_group_chats(changed_by_day: dict, olds: dict, new_days: set):
    """Зведена картинка в усі групові чати: нова версія закріплюється замість старої."""
    chats = get_group_chats()
    if not chats:
        return
    snapshot = get_all_saved()
    days = [d for d in sorted_days(changed_by_day.keys()) if is_actual(d)][:2]
    pngs = {
        day: await make_overview(snapshot, day, changed_by_day[day], "НОВИЙ ГРАФІК" if day in new_days else "ОНОВЛЕННЯ ГРАФІКА")
        for day in days
    }

    for chat_id, grp in chats:
        focus = grp if grp in GROUPS else None
        for day in days:
            kind = "new" if day in new_days else "update"
            caption = build_caption(snapshot, day, focus=focus, changed=changed_by_day[day], olds=olds, kind=kind)
            ok = await publish(chat_id, day, pngs.get(day), caption, managed=True)
            if ok and kind == "update":
                ch = compact_changes(set(changed_by_day[day]), olds, snapshot, day)
                if ch:
                    set_pin_note(chat_id, day, kind, ch)
            await asyncio.sleep(0.1)  # не впиратись у ліміти Telegram


# ---------------------------------------------------------------- Бекап бази в Telegram

def db_fingerprint() -> tuple[str, int] | None:
    """(хеш вмісту бази, кількість підписаних чатів). None — база недоступна або порожня."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    try:
        subs = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
        digest = hashlib.sha256("\n".join(conn.iterdump()).encode("utf-8")).hexdigest()
        return digest, subs
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def db_is_empty() -> bool:
    fp = db_fingerprint()
    return fp is None or fp[1] == 0


def make_backup_bytes() -> bytes:
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


async def restore_from_backup() -> bool:
    """Якщо база порожня (новий деплой без диска) — бере закріплений бекап із чату з власником і відновлює."""
    if not (BACKUP_ENABLED and bot):
        return False
    if not await asyncio.to_thread(db_is_empty):
        return False
    try:
        chat = await bot.get_chat(OWNER_ID)
        pm = getattr(chat, "pinned_message", None)
        doc = getattr(pm, "document", None) if pm else None
        if not doc or doc.file_name != BACKUP_NAME:
            logging.info("Бекап для відновлення не знайдено (закріпленого файлу в чаті з власником нема).")
            return False
        buf = io.BytesIO()
        await bot.download(doc, destination=buf)
        data = buf.getvalue()
    except Exception as e:
        logging.warning(f"Не вдалось завантажити бекап: {e}")
        return False
    if not data.startswith(b"SQLite format 3\x00"):
        logging.warning("Закріплений файл не схожий на базу SQLite, відновлення пропущено.")
        return False
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = DB_PATH + ".restore"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, DB_PATH)
    STATUS["backup_msg"] = pm.message_id
    logging.info("Базу відновлено з бекапу в Telegram.")
    return True


async def send_backup(reason: str = "авто") -> bool:
    """Надсилає базу власнику, закріплює нову копію, стару відкріплює й видаляє."""
    if not BACKUP_ENABLED:
        return False
    try:
        data = await asyncio.to_thread(make_backup_bytes)
    except Exception as e:
        logging.warning(f"Не вдалось зробити бекап: {e}")
        return False
    old = STATUS.get("backup_msg")
    try:
        msg = await bot.send_document(
            OWNER_ID, BufferedInputFile(data, filename=BACKUP_NAME),
            caption=f"💾 Бекап бази · {datetime.now():%d.%m %H:%M} · {reason}\nНе видаляй: з цього файлу бот відновиться після перезапуску.",
            disable_notification=True,
        )
    except TelegramAPIError as e:
        logging.warning(f"Не вдалось надіслати бекап власнику: {e} (напиши боту /start з акаунта OWNER_ID)")
        return False
    try:
        await bot.pin_chat_message(OWNER_ID, msg.message_id, disable_notification=True)
    except TelegramAPIError as e:
        logging.warning(f"Не вдалось закріпити бекап: {e}")
    if old and old != msg.message_id:
        for call in (bot.unpin_chat_message(OWNER_ID, message_id=old), bot.delete_message(OWNER_ID, old)):
            try:
                await call
            except TelegramAPIError:
                pass
    STATUS["backup_msg"] = msg.message_id
    return True


async def backup_loop():
    """Раз на 20 с перевіряє, чи змінилась база; якщо так (і є підписники) — шле свіжий бекап (не частіше разу на хв)."""
    last_hash = None
    last_sent = 0.0
    while True:
        try:
            if BACKUP_ENABLED:
                fp = await asyncio.to_thread(db_fingerprint)
                # порожню базу не бекапимо, щоб не затерти нею добрий бекап
                if fp and fp[1] > 0 and fp[0] != last_hash and time.monotonic() - last_sent >= 60:
                    if await send_backup():
                        last_hash = fp[0]
                    last_sent = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Помилка в backup_loop: {e}")
        await asyncio.sleep(20)


# ---------------------------------------------------------------- Background loops

async def reminders_loop():
    last_cleanup = 0.0
    while True:
        try:
            now = datetime.now()

            if time.time() - last_cleanup > 6 * 3600:
                cleanup_old_reminders()
                last_cleanup = time.time()

            for grp in GROUPS:
                saved = get_saved_schedule(grp)
                if not saved:
                    continue

                subscribers = get_subscribers_by_group(grp)
                if not subscribers:
                    continue

                spans = outage_spans(saved)
                for chat_id, _ in subscribers:
                    cfg = get_settings(chat_id)
                    want_start = cfg["notify"]
                    want_end = REMIND_END and cfg["notify_end"]
                    if not (want_start or want_end):
                        continue
                    lead = timedelta(minutes=cfg["lead"])

                    for start, end in spans:
                        # перед вимкненням
                        if want_start and start - lead <= now < start:
                            key = f"{chat_id}:{grp}:start:{start:%Y%m%d%H%M}"
                            if not was_reminder_sent(key):
                                left = max(1, round((start - now).total_seconds() / 60))
                                text = (
                                    f"⏳ <b>Увага! Через {left} хв вимкнення світла</b>\n"
                                    f"Черга: <b>{grp}</b>\n"
                                    f"Інтервал: <code>{start.strftime('%H:%M')}–{end.strftime('%H:%M')}</code>"
                                )
                                if await safe_send(chat_id, text):
                                    mark_reminder_sent(key)

                        # перед поверненням світла
                        if want_end and start < now and end - lead <= now < end:
                            key = f"{chat_id}:{grp}:end:{end:%Y%m%d%H%M}"
                            if not was_reminder_sent(key):
                                left = max(1, round((end - now).total_seconds() / 60))
                                text = (
                                    f"💡 <b>Світло має зʼявитись приблизно через {left} хв</b>\n"
                                    f"Черга: <b>{grp}</b> · очікується о <code>{end.strftime('%H:%M')}</code>"
                                )
                                if await safe_send(chat_id, text):
                                    mark_reminder_sent(key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Помилка в reminders_loop: {e}")

        await asyncio.sleep(20)


# ---------------------------------------------------------------- Щоденні зведення

def _minutes_of(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


async def send_digest(day: str, kind: str):
    """kind: 'morning' (графік на сьогодні) або 'evening' (графік на завтра)."""
    snapshot = get_all_saved()
    if not snapshot:
        return
    png = await make_overview(snapshot, day, set(), False)

    for chat_id, grp in get_group_chats():
        caption = build_caption(snapshot, day, focus=grp if grp in GROUPS else None, kind=kind)
        await publish(chat_id, day, png, caption, managed=True)
        await asyncio.sleep(0.1)

    heading = "☀️ <b>Доброго ранку!</b>" if kind == "morning" else "🌙 <b>Графік на завтра</b>"
    for chat_id, grp in get_private_subscribers():
        if grp == "all":
            caption = build_caption(snapshot, day, kind=kind)
            await publish(chat_id, day, png, caption)
        elif grp in snapshot:
            for g in get_chat_groups(chat_id):
                if g in snapshot:
                    await safe_send(chat_id, format_day_text(g, snapshot[g], day, f"{heading} Черга <b>{g}</b>"))
        await asyncio.sleep(0.1)


async def edit_card(chat_id: int, day: str, msg_id: int, png: bytes | None, caption: str) -> bool:
    """Тихо редагує вже опубліковане повідомлення (картинка + підпис). Нове повідомлення НЕ створюється."""
    for _ in range(2):
        try:
            if png:
                await bot.edit_message_media(
                    media=InputMediaPhoto(
                        media=BufferedInputFile(png, filename="schedule.png"), caption=caption, parse_mode="HTML"
                    ),
                    chat_id=chat_id, message_id=msg_id,
                )
            else:
                try:
                    await bot.edit_message_text(caption, chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
                except TelegramBadRequest as e:
                    if "no text in the message" not in str(e):
                        raise
                    await bot.edit_message_caption(chat_id=chat_id, message_id=msg_id, caption=caption, parse_mode="HTML")
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            return False
        except TelegramBadRequest as e:
            text = str(e).lower()
            if "not modified" in text:
                return True
            if "not found" in text or "can't be edited" in text:  # повідомлення видалили вручну
                delete_pin_row(chat_id, day)
            logging.info(f"Не вдалось відредагувати {msg_id} в {chat_id}: {e}")
            return False
        except Exception as e:
            logging.warning(f"Помилка редагування {msg_id} в {chat_id}: {e}")
            return False
    return False


async def refresh_cards_after_midnight():
    """Після півночі НІЧОГО не надсилаємо: уже опубліковані картинки (що були «завтра») тихо оновлюються
    на місці — міняється підпис («сьогодні»), позначка «зараз» і час оновлення на картинці."""
    snapshot = get_all_saved()
    if not snapshot:
        return
    focus = dict(get_group_chats())  # chat_id -> черга/all
    pngs: dict[str, bytes | None] = {}
    for chat_id, day, msg_id in get_all_pins():
        d = parse_day(day)
        if d is None or d < date.today() or chat_id not in focus:
            continue  # вчорашні прибере expire_old_pins
        if not any(day in v.get("days", {}) for v in snapshot.values()):
            continue
        if day not in pngs:
            pngs[day] = await make_overview(snapshot, day, set(), False)
        grp = focus[chat_id]
        note = get_pin_note(chat_id, day)
        caption = build_caption(
            snapshot, day, focus=grp if grp in GROUPS else None,
            kind=note[0] if note else None, saved_changes=note[1] if note else None,
        )
        await edit_card(chat_id, day, msg_id, pngs[day], caption)
        await asyncio.sleep(0.1)


async def digest_loop():
    while True:
        try:
            if ROLLOVER_REFRESH:
                rollover_key = f"rollover:{date.today():%Y%m%d}"
                if not was_reminder_sent(rollover_key):
                    mark_reminder_sent(rollover_key)
                    if get_all_pins():
                        await refresh_cards_after_midnight()
                    STATUS["last_refresh"] = time.monotonic()

            # періодичне тихе оновлення закріплених картинок/підписів (статус «зараз» не застаріває)
            if STATUS_REFRESH_MINUTES > 0 and ROLLOVER_REFRESH:
                if STATUS.get("last_refresh") is None:
                    STATUS["last_refresh"] = time.monotonic()
                elif time.monotonic() - STATUS["last_refresh"] >= STATUS_REFRESH_MINUTES * 60:
                    STATUS["last_refresh"] = time.monotonic()
                    if get_all_pins():
                        await refresh_cards_after_midnight()

            await expire_old_pins()  # вчорашні картинки: відкріпити й видалити

            if DIGESTS:
                now = datetime.now()
                nm = now.hour * 60 + now.minute
                today = now.strftime("%d.%m.%Y")
                tomorrow = (now + timedelta(days=1)).strftime("%d.%m.%Y")
                snapshot_days = {d for v in get_all_saved().values() for d in v.get("days", {})}

                morning_key = f"digest:morning:{today}"
                if _minutes_of(MORNING_TIME) <= nm < _minutes_of(MORNING_TIME) + 180 \
                        and today in snapshot_days and not was_reminder_sent(morning_key):
                    mark_reminder_sent(morning_key)
                    await send_digest(today, "morning")

                # якщо графік на завтра вже було розіслано як «новий», вечірнє зведення не дублюємо
                evening_key = f"digest:day:{tomorrow}"
                if nm >= _minutes_of(EVENING_TIME) \
                        and tomorrow in snapshot_days and not was_reminder_sent(evening_key):
                    mark_reminder_sent(evening_key)
                    await send_digest(tomorrow, "evening")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Помилка в digest_loop: {e}")

        await asyncio.sleep(30)


def html_to_plain_text(raw_html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>|</div\s*>|</li\s*>|</tr\s*>|</h\d\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


async def fetch_schedule_text(session: aiohttp.ClientSession) -> str:
    """Бере готовий HTML графіка з API ЛОЕ (без браузера) і повертає чистий текст."""
    url = API_URL
    if CACHE_BUST:
        url += ("&" if "?" in url else "?") + f"_={int(time.time())}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/ld+json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        resp.raise_for_status()
        payload = await resp.json(content_type=None)

    parts = []
    # JSON-LD дає {"hydra:member": [...]}, звичайний JSON — одразу список
    if isinstance(payload, dict):
        members = payload.get("hydra:member") or payload.get("member") or []
    else:
        members = payload
    for member in members:
        if not isinstance(member, dict):
            continue
        for item in member.get("menuItems", []):
            raw_html = item.get("rawHtml") or item.get("rawMobileHtml") or ""
            if raw_html:
                parts.append(html_to_plain_text(raw_html))
    return "\n\n".join(parts)


async def health_server():
    """Мінімальний HTTP-сервер для хостингів, яким потрібен відкритий порт / пінг (UptimeRobot)."""
    from aiohttp import web

    async def ok(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info(f"Health-сервер слухає порт {PORT}")
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


async def notify_owner(text: str):
    """Особисте повідомлення власнику (OWNER_ID) — про збої/зміни/перезапуск. Без OWNER_ID нічого не робить."""
    if not OWNER_ID or not bot:
        return
    try:
        await bot.send_message(OWNER_ID, text, parse_mode="HTML")
    except TelegramAPIError as e:
        logging.warning(f"Не вдалось написати власнику ({OWNER_ID}): {e}")


async def process_update(all_groups: dict) -> dict:
    """Порівнює з базою, розсилає зміни. Повертає підсумок для логів: {saved, first_load, days}."""
    changed_by_day: dict[str, set] = {}
    grp_changed_days: dict[str, list[str]] = {}
    olds: dict[str, dict | None] = {}
    prev_days = {d for g in GROUPS for d in ((get_saved_schedule(g) or {}).get("days", {}))}
    saved_groups = 0
    first_load = 0

    for grp, new_data in all_groups.items():
        old_data = get_saved_schedule(grp)
        if old_data == new_data:
            continue

        save_schedule(grp, new_data)
        saved_groups += 1

        # перше завантаження в порожню базу — не розсилаємо "оновлення"
        if old_data is None:
            first_load += 1
            continue

        olds[grp] = old_data
        for day, info in new_data.get("days", {}).items():
            if not is_actual(day):
                continue
            old_iv = old_data.get("days", {}).get(day, {}).get("intervals")
            if old_iv != info.get("intervals"):
                changed_by_day.setdefault(day, set()).add(grp)
                grp_changed_days.setdefault(grp, []).append(day)

    if saved_groups:
        STATUS["last_change"] = datetime.now()

    # сповіщаємо лише про реальні зміни (зникнення вчорашнього дня — не оновлення)
    new_days = {d for d in changed_by_day if d not in prev_days}

    # особисті чати — текст по своїй черзі
    for grp, days in grp_changed_days.items():
        days = sorted_days(days)
        text = format_personal_update(grp, all_groups[grp], olds[grp], days, new_days)
        for chat_id, chat_type in get_subscribers_by_group(grp):
            if chat_type == "private":
                await safe_send(chat_id, text)

    # групові чати — одна зведена картинка на кожен день, закріплюється замість старої
    if changed_by_day:
        await broadcast_to_group_chats(changed_by_day, olds, new_days)
        for day in new_days:  # вечірнє зведення вже не потрібне
            mark_reminder_sent(f"digest:day:{day}")
        logging.info("Зафіксовано зміни графіка, розіслано сповіщення.")

    return {"saved": saved_groups, "first_load": first_load, "days": sorted_days(changed_by_day.keys())}


def record_check_error(reason: str):
    STATUS["checks"] += 1
    STATUS["errors"] += 1
    STATUS["consecutive_errors"] += 1
    STATUS["last_error"] = datetime.now()
    STATUS["last_error_text"] = reason[:200]


async def check_alerts():
    if STATUS["consecutive_errors"] == ALERT_AFTER_ERRORS:  # рівно один раз за серію збоїв
        await notify_owner(
            f"⚠️ <b>Сайт ЛОЕ не віддає графік</b> уже {ALERT_AFTER_ERRORS} перевірок поспіль.\n"
            f"Причина: <code>{html.escape(STATUS['last_error_text'])}</code>"
        )


def log_check(summary: dict):
    """Видимий слід перевірок у логах: перша, кожна зі змінами, далі раз на HEARTBEAT_MINUTES."""
    s = STATUS
    now_m = time.monotonic()
    changed = bool(summary.get("saved"))
    due = s["last_beat"] is None or now_m - s["last_beat"] >= HEARTBEAT_MINUTES * 60
    if not (LOG_EVERY_CHECK or changed or due):
        return
    if summary.get("first_load"):
        note = f"перше завантаження, записано черг: {summary['first_load']}"
    elif summary.get("days"):
        note = "ЗМІНИ на " + ", ".join(summary["days"])
    elif changed:
        note = "дані на сайті оновились, актуальні дні без змін"
    else:
        note = "без змін"
    since = s["checks"] - s["beat_checks"]
    logging.info(
        f"🔎 Перевірка ЛОЕ ок: черг {s['groups']}, дати {', '.join(s['days']) or '—'} — {note} "
        f"(перевірок з минулого звіту: {since}, помилок усього: {s['errors']})"
    )
    s["last_beat"] = now_m
    s["beat_checks"] = s["checks"]


async def monitor_loop():
    while True:  # зовнішній цикл: якщо щось впало — стартуємо заново
        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    try:
                        body_text = await fetch_schedule_text(session)
                        all_groups = extract_all_groups(body_text)
                        days_seen = sorted({d for v in all_groups.values() for d in v.get("days", {})})
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        record_check_error(f"запит до API: {e}")
                        logging.error(f"Помилка запиту до API ЛОЕ: {e}")
                        await check_alerts()
                        await asyncio.sleep(CHECK_INTERVAL)
                        continue

                    if not all_groups:
                        record_check_error("не розпізнано жодної черги (змінився формат?)")
                        logging.warning("Не вдалось розпізнати жодної черги у відповіді API (змінився формат?).")
                        await check_alerts()
                    else:
                        was_failing = STATUS["consecutive_errors"] >= ALERT_AFTER_ERRORS
                        STATUS["checks"] += 1
                        STATUS["consecutive_errors"] = 0
                        STATUS["last_ok"] = datetime.now()
                        STATUS["groups"] = len(all_groups)
                        STATUS["days"] = days_seen
                        if was_failing:
                            await notify_owner("✅ Звʼязок із сайтом ЛОЕ відновлено.")

                        summary: dict = {}
                        try:
                            summary = await process_update(all_groups)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logging.error(f"Помилка обробки оновлення: {e}")
                        log_check(summary)
                        if summary.get("days"):
                            await notify_owner(
                                "🔔 <b>На сайті ЛОЕ змінився графік</b>: "
                                + ", ".join(pretty_day(d) for d in summary["days"])
                            )

                    await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Монітор перезапускається через помилку: {e}")
            await asyncio.sleep(15)


# ---------------------------------------------------------------- Handlers

HELP_TEXT = (
    "<b>Команди:</b>\n"
    "/start — обрати чергу (в особистих)\n"
    "/bind — вибір черги кнопками (для груп — адмінам)\n"
    "/bind all — групі надсилати всі черги разом\n"
    "/bind 5.1 — прив'язати чат до однієї черги\n"
    "/today — графік на сьогодні\n"
    "/tomorrow — графік на завтра\n"
    "/now — поточний графік\n"
    "/all — зведена картинка по всіх чергах (сьогодні + завтра)\n"
    "/pin — (у групі) оновити і закріпити свіжий графік\n"
    "/next — найближче вимкнення\n"
    "/settings — кнопки: черга, сповіщення за 5/10/15 хв, закріплення, відписка\n"
    "/status — стан бота і перевірок сайту ЛОЕ\n"
    "/stop — відписатись від сповіщень\n"
    "/help — ця довідка"
)


PICK_TEXT_PRIVATE = (
    "Обери черги, про які хочеш отримувати сповіщення (можна кілька, наприклад <b>5.1</b> і <b>3.2</b>).\n"
    "Натисни на чергу ще раз, щоб прибрати її."
)
PICK_TEXT_GROUP = (
    "Обери, який графік показувати в цьому чаті:\n"
    "• <b>Черга X.Y</b> — зведення по всіх чергах + деталі по обраній\n"
    "• <b>Усі черги разом</b> — лише зведення\n\n"
    "<i>Змінювати налаштування можуть лише адміни чату.</i>"
)


async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        return False
    return member.status in ("creator", "administrator")


async def message_from_admin(message: types.Message) -> bool:
    """В особистих — завжди так; у групах — лише адмін (включно з анонімним адміном)."""
    if message.chat.type == "private":
        return True
    if message.sender_chat and message.sender_chat.id == message.chat.id:  # адмін, що пише анонімно
        return True
    return bool(message.from_user) and await is_chat_admin(message.chat.id, message.from_user.id)


def current_group(chat_id: int) -> str | None:
    sub = get_subscription(chat_id)
    return sub[1] if sub else None


def current_groups(chat_id: int, is_private: bool):
    """Для особистих — список обраних черг, для груп — одна черга/all."""
    return get_chat_groups(chat_id) if is_private else current_group(chat_id)


@dp.message(Command("start", "group"))
async def cmd_start(message: types.Message):
    logging.info(f"Отримано /start у чаті {message.chat.id} ({message.chat.type})")
    is_private = message.chat.type == "private"
    await message.answer(
        PICK_TEXT_PRIVATE if is_private else PICK_TEXT_GROUP,
        reply_markup=get_groups_keyboard(is_private, current=current_groups(message.chat.id, is_private)),
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


async def send_bind_extras(chat_id: int, chat_type: str, grp: str):
    """Після прив'язки: підказка про права адміна (група) і свіжа картинка графіка."""
    is_group = chat_type != "private"
    if is_group:
        hint = await pin_hint(chat_id)
        if hint:
            await bot.send_message(chat_id, hint.strip(), parse_mode="HTML")
    if not USE_IMAGES:
        return
    if grp == "all":
        if get_all_saved():
            await send_overview(chat_id, managed=is_group)
    elif is_group and get_saved_schedule(grp):
        await send_overview(chat_id, focus=grp, managed=True)


@dp.callback_query(F.data.startswith("set_grp:"))
async def cb_set_group(callback: types.CallbackQuery):
    msg = callback.message
    if msg is None:
        await callback.answer()
        return
    chat = msg.chat
    is_private = chat.type == "private"
    grp = callback.data.split(":", 1)[1]
    if grp not in GROUPS and not (grp == "all" and not is_private):
        await callback.answer("Невідома черга")
        return
    if not is_private and not await is_chat_admin(chat.id, callback.from_user.id):
        await callback.answer("Змінювати чергу можуть лише адміни чату", show_alert=True)
        return

    if is_private:
        chosen = toggle_chat_group(chat.id, chat.type, grp)
        if chosen:
            text = "✅ Обрані черги: <b>" + ", ".join(chosen) + "</b>\n\n"
            blocks = []
            for g in chosen:
                saved = get_saved_schedule(g)
                if saved:
                    blocks.append(format_personal_message(g, saved, is_update=False))
            text += "\n\n".join(blocks) if blocks else (
                "Дані з сайту ЛОЕ ще завантажуються, надішлю графік одразу після першої перевірки."
            )
        else:
            text = "🔕 Жодної черги не обрано — сповіщення вимкнено. Обери чергу нижче."
        try:
            await msg.edit_text(text[:4000], reply_markup=get_groups_keyboard(True, current=chosen), parse_mode="HTML")
        except TelegramBadRequest:
            pass
        await callback.answer(("Додано/прибрано: " + grp) if chosen else "Відписано")
        return

    set_subscription(chat.id, chat.type, grp)

    # група: перетворюємо повідомлення на меню налаштувань і надсилаємо свіжий графік
    text, kb = settings_view(chat.id, False)
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await callback.answer("Чат прив'язано: " + ("усі черги" if grp == "all" else f"черга {grp}"))
    await send_bind_extras(chat.id, chat.type, grp)


def settings_view(chat_id: int, is_private: bool) -> tuple[str, InlineKeyboardMarkup]:
    cfg = get_settings(chat_id)
    sub = get_subscription(chat_id)
    lines = ["⚙️ <b>Налаштування чату</b>", ""]
    if not sub:
        lines += ["⚠️ Чат ще не підписаний — натисни «Обрати чергу».", ""]
    elif sub[1] == "all":
        lines += [
            "📍 Показую: <b>усі черги разом</b>",
            "ℹ️ Нагадування про вимкнення працюють для конкретної черги — обери її кнопкою нижче.",
            "",
        ]
    elif is_private:
        lines += ["📍 Черги: <b>" + ", ".join(get_chat_groups(chat_id) or [sub[1]]) + "</b>", ""]
    else:
        lines += [f"📍 Черга: <b>{sub[1]}</b>", ""]

    lines.append("⏳ Перед вимкненням: " + (f"<b>за {cfg['lead']} хв</b>" if cfg["notify"] else "не нагадую"))
    if REMIND_END:
        lines.append("💡 Перед ввімкненням: " + (f"<b>за {cfg['lead']} хв</b>" if cfg["notify_end"] else "не нагадую"))
    lines.append("<i>Оновлення графіка приходять завжди.</i>")

    kb = [[InlineKeyboardButton(
        text="🔁 Змінити черги" if sub else "➕ Обрати черги", callback_data="cfg:queue"
    )]]
    kb.append([InlineKeyboardButton(
        text="⏳ Вимкнення: так ✅" if cfg["notify"] else "⏳ Вимкнення: ні ❌", callback_data="cfg:notify"
    )])
    if REMIND_END:
        kb.append([InlineKeyboardButton(
            text="💡 Ввімкнення: так ✅" if cfg["notify_end"] else "💡 Ввімкнення: ні ❌", callback_data="cfg:notify_end"
        )])
    if cfg["notify"] or (REMIND_END and cfg["notify_end"]):
        kb.append([
            InlineKeyboardButton(
                text=f"✅ за {m} хв" if m == cfg["lead"] else f"за {m} хв", callback_data=f"cfg:lead:{m}"
            )
            for m in LEAD_OPTIONS
        ])
    if not is_private and PIN_SCHEDULE:
        lines.append("📌 Закріплення графіка: " + ("так" if cfg["pin"] else "ні"))
        kb.append([InlineKeyboardButton(
            text="📌 Закріплювати: так ✅" if cfg["pin"] else "📌 Закріплювати: ні ❌", callback_data="cfg:pin"
        )])
    bottom = [InlineKeyboardButton(text="📊 Показати графік", callback_data="cfg:show")]
    if sub:
        bottom.append(InlineKeyboardButton(text="🚫 Відписатись", callback_data="cfg:stop"))
    kb.append(bottom)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    text, kb = settings_view(message.chat.id, message.chat.type == "private")
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


async def show_now(chat_id: int, is_private: bool):
    """Кнопка «Показати графік»."""
    sub = get_subscription(chat_id)
    if not sub:
        await bot.send_message(chat_id, "Спочатку обери чергу: /settings → «Обрати чергу».")
        return
    grp = sub[1]
    if is_private and grp in GROUPS:
        blocks = [
            format_personal_message(g, get_saved_schedule(g), is_update=False)
            for g in get_chat_groups(chat_id) if get_saved_schedule(g)
        ]
        text = "\n\n".join(blocks) if blocks else "Дані ще завантажуються, спробуй за хвилину."
        await bot.send_message(chat_id, text[:4000], parse_mode="HTML")
    elif USE_IMAGES and get_all_saved():
        if not await send_overview(chat_id, focus=grp if grp in GROUPS else None):
            await bot.send_message(chat_id, "Актуального графіка на сьогодні чи завтра ще немає на сайті ЛОЕ.")
    else:
        await bot.send_message(chat_id, "Дані ще завантажуються або картинки вимкнено.")


@dp.callback_query(F.data.startswith("cfg:"))
async def cb_settings(callback: types.CallbackQuery):
    msg = callback.message
    if msg is None:
        await callback.answer()
        return
    chat = msg.chat
    is_private = chat.type == "private"
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "open":  # з клавіатури вибору черги — нове повідомлення, щоб не ламати вибір черги
        text, kb = settings_view(chat.id, is_private)
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    if action == "show":  # перегляд доступний усім
        await callback.answer()
        await show_now(chat.id, is_private)
        return

    if not is_private:  # у групах міняти налаштування можуть лише адміни
        if not await is_chat_admin(chat.id, callback.from_user.id):
            await callback.answer("Змінювати налаштування можуть лише адміни чату", show_alert=True)
            return

    cfg = get_settings(chat.id)
    toast: str | None = "Збережено"
    if action == "notify":
        update_setting(chat.id, notify=not cfg["notify"])
    elif action == "notify_end" and REMIND_END:
        update_setting(chat.id, notify_end=not cfg["notify_end"])
    elif action == "lead" and len(parts) == 3 and parts[2].isdigit() and int(parts[2]) in LEAD_OPTIONS:
        update_setting(chat.id, lead=int(parts[2]))
    elif action == "pin" and not is_private:
        update_setting(chat.id, pin=not cfg["pin"])
        await apply_pin_setting(chat.id, not cfg["pin"])
    elif action == "queue":  # вибір черги прямо в цьому повідомленні
        try:
            await msg.edit_text(
                PICK_TEXT_PRIVATE if is_private else PICK_TEXT_GROUP,
                reply_markup=get_groups_keyboard(is_private, current=current_groups(chat.id, is_private), back=True),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
        await callback.answer()
        return
    elif action == "back":
        toast = None
    elif action == "stop":
        await release_chat_pins(chat.id)
        remove_subscription(chat.id)
        toast = "Відписано"
    else:
        await callback.answer()
        return

    text, kb = settings_view(chat.id, is_private)
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest:
        pass
    await callback.answer(toast)


@dp.message(Command("bind"))
async def cmd_bind_chat(message: types.Message):
    if not await message_from_admin(message):
        await message.answer("Прив'язувати чат можуть лише адміни.")
        return

    parts = (message.text or "").split()
    if len(parts) < 2:  # без аргументу — інтерактивний вибір кнопками
        await cmd_start(message)
        return
    grp = parts[1].lower()

    if grp != "all" and grp not in GROUPS:
        await message.answer(
            f"Вкажи чергу або <code>all</code>. Доступні: {', '.join(GROUPS)}\n"
            "Приклади: <code>/bind all</code> (усі черги) або <code>/bind 5.1</code>. "
            "Або просто /bind — тоді оберу кнопками.",
            parse_mode="HTML",
        )
        return

    set_subscription(message.chat.id, message.chat.type, grp)

    if grp == "all":
        await message.answer(
            "✅ Цей чат отримуватиме оновлення графіка по <b>всіх чергах</b> одним зведенням з картинкою.\n"
            "Подивитись зараз: /all · Налаштування: /settings",
            parse_mode="HTML",
        )
    else:
        saved = get_saved_schedule(grp)
        text = f"✅ Цей чат прив'язано до <b>черги {grp}</b> (картинка по всіх чергах + деталі по {grp}).\n\n"
        text += format_chat_message(grp, saved, is_update=False) if saved else "Очікую перше оновлення з сайту ЛОЕ..."
        await message.answer(text, parse_mode="HTML")
    await send_bind_extras(message.chat.id, message.chat.type, grp)


def fmt_dt(dt: datetime | None) -> str:
    return dt.strftime("%d.%m %H:%M:%S") if dt else "—"


def status_text() -> str:
    s = STATUS
    now = datetime.now()
    lines = ["🩺 <b>Стан бота</b>", ""]
    if s["last_ok"]:
        ago = int((now - s["last_ok"]).total_seconds())
        lines.append(f"🌐 Остання успішна перевірка ЛОЕ: <b>{fmt_dt(s['last_ok'])}</b> ({ago} с тому)")
    else:
        lines.append("🌐 Успішних перевірок ЛОЕ ще не було")
    lines.append(f"🔁 Інтервал: {CHECK_INTERVAL} с · перевірок: {s['checks']} · помилок: {s['errors']}")
    lines.append(f"📋 Розпізнано черг: {s['groups']} · дати: {', '.join(s['days']) or '—'}")
    lines.append(f"🕒 Дані в базі востаннє змінились: {fmt_dt(s['last_change'])}")
    if s["consecutive_errors"]:
        lines.append(
            f"⚠️ Помилок поспіль: <b>{s['consecutive_errors']}</b> — <code>{html.escape(s['last_error_text'])}</code>"
        )
    if current_theme:
        lines.append(f"🎨 Тема картинок: {THEMES[current_theme()]['ua']}")
    priv, grp = count_subscribers()
    lines.append(f"👥 Підписано: {priv} особистих, {grp} групових чатів")
    lines.append(f"⏱ Працюю: {format_duration(int((now - s['started']).total_seconds() // 60))}")
    return "\n".join(lines)


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    await message.answer(status_text(), parse_mode="HTML")


@dp.message(Command("now"))
async def cmd_now(message: types.Message):
    sub = get_subscription(message.chat.id)
    if not sub:
        await message.answer("Спочатку обери чергу через /start (в особистих) або /bind all чи /bind 5.1 (у чаті).")
        return

    chat_type, grp = sub
    if grp == "all":
        if not get_all_saved():
            await message.answer("Дані ще завантажуються, спробуй за хвилину.")
        elif USE_IMAGES:
            await send_overview(message.chat.id)
        else:
            await message.answer("Картинки вимкнено (встанови pillow: python -m pip install pillow).")
        return

    saved = get_saved_schedule(grp)
    if not saved:
        await message.answer("Дані ще завантажуються, спробуй за хвилину.")
        return

    if chat_type == "private":
        blocks = [
            format_personal_message(g, get_saved_schedule(g), is_update=False)
            for g in get_chat_groups(message.chat.id) if get_saved_schedule(g)
        ]
        await message.answer("\n\n".join(blocks)[:4000], parse_mode="HTML")
    else:
        await send_overview(message.chat.id, focus=grp)
        await message.answer(format_chat_message(grp, saved, is_update=False), parse_mode="HTML")


async def send_overview(chat_id: int, focus: str | None = None, managed: bool = False,
                        force_pin: bool = False, kind: str | None = None) -> bool:
    """Картинки-зведення на сьогодні та завтра. False — якщо актуального графіка ще нема."""
    snapshot = get_all_saved()
    if not snapshot:
        return False
    all_days = {d for v in snapshot.values() for d in v.get("days", {})}
    ok = False
    for day in upcoming_days(all_days):
        png = await make_overview(snapshot, day, set(), False)
        caption = build_caption(snapshot, day, focus=focus, kind=kind)
        ok |= await publish(chat_id, day, png, caption, managed=managed, force_pin=force_pin)
    return ok


async def show_day(message: types.Message, offset: int):
    """/today (offset=0) і /tomorrow (offset=1)."""
    snapshot = get_all_saved()
    if not snapshot:
        await message.answer("Дані ще завантажуються, спробуй за хвилину.")
        return
    day = resolve_day(snapshot, offset)
    if not day:
        word = "завтра" if offset else "сьогодні"
        await message.answer(
            f"Графіка на {word} ще немає на сайті ЛОЕ. Я надішлю його, щойно він зʼявиться"
            + ("." if get_subscription(message.chat.id) else " (підпишись: /start або /bind)."),
        )
        return

    sub = get_subscription(message.chat.id)
    grp = sub[1] if sub else None
    if sub and sub[0] == "private" and grp in snapshot:
        heading = "☀️ <b>Графік на сьогодні</b>" if offset == 0 else "🌙 <b>Графік на завтра</b>"
        for g in get_chat_groups(message.chat.id):
            if g in snapshot:
                await message.answer(
                    format_day_text(g, snapshot[g], day, f"{heading} · черга <b>{g}</b>"), parse_mode="HTML"
                )
        return

    if not USE_IMAGES:
        await message.answer("Картинки вимкнено (встанови pillow: python -m pip install pillow).")
        return
    png = await make_overview(snapshot, day, set(), False)
    caption = build_caption(snapshot, day, focus=grp if grp in GROUPS else None)
    await publish(message.chat.id, day, png, caption)


@dp.message(Command("today"))
async def cmd_today(message: types.Message):
    await show_day(message, 0)


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(message: types.Message):
    await show_day(message, 1)


@dp.message(Command("pin"))
async def cmd_pin(message: types.Message):
    if message.chat.type == "private":
        await message.answer("/pin працює в групових чатах. Тут просто користуйся /today і /tomorrow.")
        return
    sub = get_subscription(message.chat.id)
    if not sub:
        await message.answer("Спочатку прив'яжи чат: /bind all або /bind 5.1.")
        return
    if not get_all_saved() or not USE_IMAGES:
        await message.answer("Дані ще завантажуються або картинки вимкнено.")
        return
    ok = await send_overview(
        message.chat.id, focus=sub[1] if sub[1] in GROUPS else None, managed=True, force_pin=True
    )
    if not ok:
        await message.answer("Актуального графіка на сьогодні чи завтра ще немає на сайті ЛОЕ.")
        return
    hint = await pin_hint(message.chat.id)
    if hint:
        await message.answer(hint.strip(), parse_mode="HTML")


@dp.message(F.pinned_message)
async def on_service_pin(message: types.Message):
    """Прибирає службове «Бот закріпив повідомлення», щоб не засмічувати чат."""
    if not PIN_SCHEDULE:
        return
    try:
        me = await bot.me()
        if message.from_user and message.from_user.id == me.id:
            await message.delete()
    except TelegramAPIError:
        pass


@dp.message(F.migrate_to_chat_id)
async def on_group_migrated(message: types.Message):
    """Група стала супергрупою — переносимо підписку на новий id."""
    migrate_chat(message.chat.id, message.migrate_to_chat_id)


@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    if not get_all_saved():
        await message.answer("Дані ще завантажуються, спробуй за хвилину.")
        return
    if not USE_IMAGES:
        await message.answer("Картинки вимкнено (встанови pillow: python -m pip install pillow).")
        return
    if not await send_overview(message.chat.id):
        await message.answer("Актуального графіка на сьогодні чи завтра ще немає на сайті ЛОЕ.")


@dp.message(Command("next"))
async def cmd_next(message: types.Message):
    sub = get_subscription(message.chat.id)
    if not sub:
        await message.answer("Спочатку обери чергу через /start (в особистих) або /bind all чи /bind 5.1 (у чаті).")
        return

    _, grp = sub
    if grp == "all":
        await message.answer("Для /next потрібна конкретна черга: <code>/bind 5.1</code>", parse_mode="HTML")
        return

    saved = get_saved_schedule(grp)
    if not saved:
        await message.answer("Дані ще завантажуються, спробуй за хвилину.")
        return

    groups = get_chat_groups(message.chat.id) if message.chat.type == "private" else [grp]
    out = []
    for g in groups:
        s = get_saved_schedule(g)
        t = next_outage_text(g, s) if s else None
        out.append((f"<b>Черга {g}</b>\n" if len(groups) > 1 else "") + (t or "✅ Найближчим часом вимкнень не заплановано."))
    await message.answer("\n\n".join(out), parse_mode="HTML")


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    if not await message_from_admin(message):
        await message.answer("Відписати чат можуть лише адміни.")
        return
    if get_subscription(message.chat.id):
        await release_chat_pins(message.chat.id)
        remove_subscription(message.chat.id)
        await message.answer("🔕 Сповіщення вимкнено. Щоб підписатись знову — /start.")
    else:
        await message.answer("Цей чат ще не підписаний на жодну чергу.")


# ---------------------------------------------------------------- Команди власника

def is_owner(message: types.Message) -> bool:
    return bool(OWNER_ID) and message.chat.type == "private" and bool(message.from_user) and message.from_user.id == OWNER_ID


@dp.message(Command("backup"))
async def cmd_backup(message: types.Message):
    if not is_owner(message):
        return
    if not BACKUP_ENABLED:
        await message.answer("Бекап вимкнено (потрібен OWNER_ID і BACKUP=1).")
        return
    ok = await send_backup("вручну")
    if not ok:
        await message.answer("Не вдалось зробити бекап, дивись лог.")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    """/broadcast текст — розсилка всім підписаним чатам (лише власнику)."""
    if not is_owner(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Використання: <code>/broadcast текст повідомлення</code>", parse_mode="HTML")
        return
    text = "📢 " + html.escape(parts[1].strip())
    with db() as conn:
        chat_ids = [r[0] for r in conn.execute("SELECT chat_id FROM subscribers").fetchall()]
    sent = 0
    for chat_id in chat_ids:
        if await safe_send(chat_id, text):
            sent += 1
        await asyncio.sleep(0.1)
    await message.answer(f"Розіслано: {sent} з {len(chat_ids)} чатів.")


# ---------------------------------------------------------------- Main

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задано BOT_TOKEN! Створи файл .env поруч із bot.py: BOT_TOKEN=твій_токен")

    restored = await restore_from_backup()
    if not BACKUP_ENABLED:
        logging.warning("Бекап бази вимкнено: задай OWNER_ID, щоб підписки переживали перезапуски на хостингу без диска.")
    init_db()
    logging.info(f"Бот запущено. Перевірка ЛОЕ кожні {CHECK_INTERVAL}с, звіт у лог раз на {HEARTBEAT_MINUTES} хв.")
    try:  # меню «/» у Telegram
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
    except TelegramAPIError as e:
        logging.warning(f"Не вдалось встановити меню команд: {e}")
    priv, grp = count_subscribers()
    logging.info(f"У базі підписано чатів: {priv} особистих, {grp} групових (DB_PATH={DB_PATH})")
    await notify_owner(
        ("♻️ Базу відновлено з бекапу. " if restored else "")
        + f"🚀 Бот запущено. Підписано: {priv} особистих, {grp} групових чатів."
    )
    monitor_task = asyncio.create_task(monitor_loop())
    reminders_task = asyncio.create_task(reminders_loop())
    digest_task = asyncio.create_task(digest_loop())
    backup_task = asyncio.create_task(backup_loop())
    health_task = asyncio.create_task(health_server()) if PORT else None

    try:
        while True:
            try:
                await dp.start_polling(bot, close_bot_session=False)
                break
            except TelegramRetryAfter as e:
                wait_sec = e.retry_after + 2
                logging.warning(f"Ліміт Telegram (Flood control). Чекаю {wait_sec}с перед повторним підключенням...")
                await asyncio.sleep(wait_sec)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error(f"Помилка polling: {e}. Повтор через 5с...")
                await asyncio.sleep(5)
    finally:
        monitor_task.cancel()
        reminders_task.cancel()
        digest_task.cancel()
        backup_task.cancel()
        tasks = [monitor_task, reminders_task, digest_task, backup_task]
        if health_task:
            health_task.cancel()
            tasks.append(health_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот зупинено.")
