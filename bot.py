import asyncio
import html
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
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

try:
    from card import render_overview_card, render_schedule_card
    _card_err = None
except Exception as _e:  # Pillow не встановлено або card.py відсутній
    render_schedule_card = render_overview_card = None
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

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  # фіксований інтервал перевірки сайту, с
REMINDER_MINUTES_BEFORE = int(os.getenv("REMINDER_MINUTES_BEFORE", "15"))
REMIND_END = os.getenv("REMIND_END", "1") == "1"            # нагадувати і про те, що світло скоро зʼявиться

# закріплення графіка в групових чатах
PIN_SCHEDULE = os.getenv("PIN_SCHEDULE", "1") == "1"        # закріплювати свіжий графік
REPLACE_OLD = os.getenv("REPLACE_OLD", "1") == "1"          # видаляти стару версію картинки цього ж дня
DELETE_EXPIRED = os.getenv("DELETE_EXPIRED", "0") == "1"    # видаляти (а не лише відкріплювати) вчорашні картинки

# щоденні зведення
DIGESTS = os.getenv("DIGESTS", "0") == "1"                 # за замовчуванням ВИМКНЕНО: публікуємо лише коли графік змінився на сайті
ROLLOVER_REFRESH = os.getenv("ROLLOVER_REFRESH", "1") == "1"  # тиха заміна закріплених картинок після півночі
MORNING_TIME = os.getenv("MORNING_TIME", "07:00")           # графік на сьогодні
EVENING_TIME = os.getenv("EVENING_TIME", "20:00")           # графік на завтра

WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
if _card_err:
    logging.warning(f"Картинки вимкнено: {_card_err} (встанови: python -m pip install pillow)")
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


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


def set_subscription(chat_id: int, chat_type: str, group_name: str):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO subscribers (chat_id, chat_type, group_name) VALUES (?, ?, ?)",
            (chat_id, chat_type, group_name),
        )


def remove_subscription(chat_id: int):
    with db() as conn:
        conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM pinned_messages WHERE chat_id = ?", (chat_id,))


def get_subscription(chat_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT chat_type, group_name FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def get_subscribers_by_group(group_name: str):
    with db() as conn:
        return conn.execute(
            "SELECT chat_id, chat_type FROM subscribers WHERE group_name = ?", (group_name,)
        ).fetchall()


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


def get_all_pins() -> list[tuple[int, str, int]]:
    with db() as conn:
        return conn.execute("SELECT chat_id, day, message_id FROM pinned_messages").fetchall()


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
    """Сьогодні та завтра (дати порівнюються як дати, а не як рядки)."""
    res = [d for d in sorted_days(all_days) if is_actual(d)]
    return res[:limit] or sorted_days(all_days)[-1:]


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


def get_groups_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(GROUPS), 4):
        row = [InlineKeyboardButton(text=f"Черга {g}", callback_data=f"set_grp:{g}") for g in GROUPS[i:i + 4]]
        rows.append(row)
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
            lines.append("Тепер: " + ", ".join(f"<code>{iv}</code>" for iv in ivs))
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
        worst = max(totals, key=totals.get)
        lines.append(f"⏱ Найбільше без світла: <b>{worst}</b> — {format_duration(totals[worst])}")
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
    None: "🎄 <b>Графік</b>",
}


def build_caption(snapshot: dict, day: str, focus: str | None = None, changed=None, olds=None, kind: str | None = None) -> str:
    """Текст під картинкою: що змінилось, ваша черга, хто зараз без світла."""
    head = f"{CAPTION_TITLES.get(kind, CAPTION_TITLES[None])} · {pretty_day(day)}"
    lines = [head]
    lines += focus_lines(focus, snapshot, day)
    if kind == "update" and changed:
        ch = compact_changes(set(changed), olds or {}, snapshot, day)
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


async def pin_replace(chat_id: int, day: str, new_id: int):
    """Закріплює нове повідомлення, а старе (цього ж дня) відкріплює й за потреби видаляє."""
    old_id = get_pin(chat_id, day)
    try:
        await bot.pin_chat_message(chat_id, new_id, disable_notification=True)
    except TelegramAPIError as e:
        logging.warning(f"Не вдалось закріпити в {chat_id}: {e} (потрібні права адміна «Закріплення повідомлень»)")
        return
    set_pin(chat_id, day, new_id)
    if old_id and old_id != new_id:
        try:
            await bot.unpin_chat_message(chat_id, message_id=old_id)
        except TelegramAPIError:
            pass
        if REPLACE_OLD:
            try:
                await bot.delete_message(chat_id, old_id)
            except TelegramAPIError as e:
                logging.info(f"Не вдалось видалити стару картинку {old_id} в {chat_id}: {e}")


async def publish(chat_id: int, day: str, png: bytes | None, text: str, pin: bool = True, silent: bool = False) -> bool:
    """Надсилає картинку/текст на день і (для груп) закріплює її замість попередньої."""
    msg = await send_msg(chat_id, png, text, silent=silent)
    if msg is None:
        return False
    if pin and PIN_SCHEDULE:
        await pin_replace(chat_id, day, msg.message_id)
    return True


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
    """Підказка в групі, якщо в бота немає прав закріплювати."""
    if not PIN_SCHEDULE:
        return ""
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id, me.id)
    except TelegramAPIError:
        return ""
    if member.status == "administrator" and getattr(member, "can_pin_messages", False):
        return ""
    return (
        "\n\n📌 Щоб я закріплював свіжий графік, зроби мене адміном із правами "
        "«Закріплення повідомлень» і «Видалення повідомлень» (щоб прибирати старі версії)."
    )


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
            await publish(chat_id, day, pngs.get(day), caption, pin=True)
            await asyncio.sleep(0.1)  # не впиратись у ліміти Telegram


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

                lead = timedelta(minutes=REMINDER_MINUTES_BEFORE)
                for start, end in outage_spans(saved):
                    # перед вимкненням
                    if start - lead <= now < start:
                        left = max(1, round((start - now).total_seconds() / 60))
                        text = (
                            f"⏳ <b>Увага! Через {left} хв вимкнення світла</b>\n"
                            f"Черга: <b>{grp}</b>\n"
                            f"Інтервал: <code>{start.strftime('%H:%M')}–{end.strftime('%H:%M')}</code>"
                        )
                        for chat_id, _ in subscribers:
                            key = f"{chat_id}:{grp}:start:{start:%Y%m%d%H%M}"
                            if not was_reminder_sent(key) and await safe_send(chat_id, text):
                                mark_reminder_sent(key)

                    # перед поверненням світла
                    if REMIND_END and end - lead <= now < end and start < now:
                        left = max(1, round((end - now).total_seconds() / 60))
                        text = (
                            f"💡 <b>Світло має зʼявитись приблизно через {left} хв</b>\n"
                            f"Черга: <b>{grp}</b> · очікується о <code>{end.strftime('%H:%M')}</code>"
                        )
                        for chat_id, _ in subscribers:
                            key = f"{chat_id}:{grp}:end:{end:%Y%m%d%H%M}"
                            if not was_reminder_sent(key) and await safe_send(chat_id, text):
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
        await publish(chat_id, day, png, caption, pin=True)
        await asyncio.sleep(0.1)

    heading = "☀️ <b>Доброго ранку!</b>" if kind == "morning" else "🌙 <b>Графік на завтра</b>"
    for chat_id, grp in get_private_subscribers():
        if grp == "all":
            caption = build_caption(snapshot, day, kind=kind)
            await publish(chat_id, day, png, caption, pin=False)
        elif grp in snapshot:
            await safe_send(chat_id, format_day_text(grp, snapshot[grp], day, f"{heading} Черга <b>{grp}</b>"))
        await asyncio.sleep(0.1)


async def refresh_cards_after_midnight():
    """Тихо замінює закріплені картинки, щоб після півночі «ЗАВТРА» стало «СЬОГОДНІ». Без сповіщень."""
    snapshot = get_all_saved()
    if not snapshot:
        return
    all_days = {d for v in snapshot.values() for d in v.get("days", {})}
    days = upcoming_days(all_days)
    pngs = {day: await make_overview(snapshot, day, set(), False) for day in days}
    for chat_id, grp in get_group_chats():
        for day in days:
            caption = build_caption(snapshot, day, focus=grp if grp in GROUPS else None)
            await publish(chat_id, day, pngs.get(day), caption, pin=True, silent=True)
            await asyncio.sleep(0.1)


async def digest_loop():
    while True:
        try:
            await expire_old_pins()

            if ROLLOVER_REFRESH:
                rollover_key = f"rollover:{date.today():%Y%m%d}"
                if not was_reminder_sent(rollover_key):
                    mark_reminder_sent(rollover_key)
                    if get_all_pins():
                        await refresh_cards_after_midnight()

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


async def process_update(all_groups: dict):
    changed_by_day: dict[str, set] = {}
    grp_changed_days: dict[str, list[str]] = {}
    olds: dict[str, dict | None] = {}
    prev_days = {d for g in GROUPS for d in ((get_saved_schedule(g) or {}).get("days", {}))}

    for grp, new_data in all_groups.items():
        old_data = get_saved_schedule(grp)
        if old_data == new_data:
            continue

        save_schedule(grp, new_data)

        # перше завантаження в порожню базу — не розсилаємо "оновлення"
        if old_data is None:
            continue

        olds[grp] = old_data
        for day, info in new_data.get("days", {}).items():
            if not is_actual(day):
                continue
            old_iv = old_data.get("days", {}).get(day, {}).get("intervals")
            if old_iv != info.get("intervals"):
                changed_by_day.setdefault(day, set()).add(grp)
                grp_changed_days.setdefault(grp, []).append(day)

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


async def monitor_loop():
    while True:  # зовнішній цикл: якщо щось впало — стартуємо заново
        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    logging.info(f"Перевірка ЛОЕ (інтервал {CHECK_INTERVAL}с)...")
                    try:
                        body_text = await fetch_schedule_text(session)
                        all_groups = extract_all_groups(body_text)
                        days_seen = sorted({d for v in all_groups.values() for d in v.get("days", {})})
                        logging.info(f"Розпізнано черг: {len(all_groups)}, дати: {', '.join(days_seen) or '—'}")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logging.error(f"Помилка запиту до API ЛОЕ: {e}")
                        await asyncio.sleep(CHECK_INTERVAL)
                        continue

                    if not all_groups:
                        logging.warning("Не вдалось розпізнати жодної черги у відповіді API (змінився формат?).")
                    else:
                        try:
                            await process_update(all_groups)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logging.error(f"Помилка обробки оновлення: {e}")

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
    "/bind all — групі надсилати всі черги разом\n"
    "/bind 5.1 — прив'язати чат до однієї черги\n"
    "/today — графік на сьогодні\n"
    "/tomorrow — графік на завтра\n"
    "/now — поточний графік\n"
    "/all — зведена картинка по всіх чергах (сьогодні + завтра)\n"
    "/pin — (у групі) оновити і закріпити свіжий графік\n"
    "/next — найближче вимкнення\n"
    "/stop — відписатись від сповіщень\n"
    "/help — ця довідка"
)


@dp.message(Command("start", "group"))
async def cmd_start(message: types.Message):
    logging.info(f"Отримано /start у чаті {message.chat.id} ({message.chat.type})")
    if message.chat.type == "private":
        await message.answer(
            "Обери свою фактичну чергу відключень (наприклад, <b>5.1</b>):",
            reply_markup=get_groups_keyboard(),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "Щоб отримувати графік у цьому чаті:\n<code>/bind all</code> — усі черги одним зведенням\n<code>/bind 5.1</code> — зведення + деталі по черзі 5.1",
            parse_mode="HTML",
        )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.callback_query(F.data.startswith("set_grp:"))
async def cb_set_group(callback: types.CallbackQuery):
    grp = callback.data.split(":")[1]
    if grp not in GROUPS:
        await callback.answer("Невідома черга")
        return

    set_subscription(callback.message.chat.id, "private", grp)
    saved = get_saved_schedule(grp)

    text = f"✅ Встановлено <b>чергу {grp}</b>.\n\n"
    if saved:
        text += format_personal_message(grp, saved, is_update=False)
    else:
        text += "Дані з сайту ЛОЕ ще завантажуються, надішлю графік одразу після першої перевірки."

    try:
        await callback.message.edit_text(text, reply_markup=get_groups_keyboard(), parse_mode="HTML")
    except TelegramBadRequest:
        pass  # "message is not modified" — натиснули ту саму чергу ще раз
    await callback.answer(f"Обрано чергу {grp}")


@dp.message(Command("bind"))
async def cmd_bind_chat(message: types.Message):
    parts = (message.text or "").split()
    grp = parts[1].lower() if len(parts) > 1 else "all"

    if grp != "all" and grp not in GROUPS:
        await message.answer(
            f"Вкажи чергу або <code>all</code>. Доступні: {', '.join(GROUPS)}\n"
            "Приклади: <code>/bind all</code> (усі черги) або <code>/bind 5.1</code>",
            parse_mode="HTML",
        )
        return

    set_subscription(message.chat.id, message.chat.type, grp)

    if grp == "all":
        await message.answer(
            "✅ Цей чат отримуватиме оновлення графіка по <b>всіх чергах</b> одним зведенням з картинкою.\n"
            "Подивитись зараз: /all",
            parse_mode="HTML",
        )
        hint = await pin_hint(message.chat.id) if message.chat.type != "private" else ""
        if hint:
            await message.answer(hint.strip(), parse_mode="HTML")
        if get_all_saved() and USE_IMAGES:
            await send_overview(message.chat.id, pin=message.chat.type != "private")
        return

    saved = get_saved_schedule(grp)
    text = f"✅ Цей чат прив'язано до <b>черги {grp}</b> (картинка по всіх чергах + деталі по {grp}).\n\n"
    if saved:
        text += format_chat_message(grp, saved, is_update=False)
    else:
        text += "Очікую перше оновлення з сайту ЛОЕ..."
    if message.chat.type != "private":
        text += await pin_hint(message.chat.id)

    await message.answer(text, parse_mode="HTML")
    if saved and USE_IMAGES and message.chat.type != "private":
        await send_overview(message.chat.id, focus=grp, pin=True)


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
        await message.answer(format_personal_message(grp, saved, is_update=False), parse_mode="HTML")
    else:
        await send_overview(message.chat.id, focus=grp)
        await message.answer(format_chat_message(grp, saved, is_update=False), parse_mode="HTML")


async def send_overview(chat_id: int, focus: str | None = None, pin: bool = False, kind: str | None = None) -> bool:
    """Картинки-зведення на сьогодні та завтра (pin=True — закріпити замість старих)."""
    snapshot = get_all_saved()
    if not snapshot:
        return False
    all_days = {d for v in snapshot.values() for d in v.get("days", {})}
    ok = False
    for day in upcoming_days(all_days):
        png = await make_overview(snapshot, day, set(), False)
        caption = build_caption(snapshot, day, focus=focus, kind=kind)
        ok |= await publish(chat_id, day, png, caption, pin=pin)
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
        await message.answer(
            format_day_text(grp, snapshot[grp], day, f"{heading} · черга <b>{grp}</b>"), parse_mode="HTML"
        )
        return

    if not USE_IMAGES:
        await message.answer("Картинки вимкнено (встанови pillow: python -m pip install pillow).")
        return
    png = await make_overview(snapshot, day, set(), False)
    caption = build_caption(snapshot, day, focus=grp if grp in GROUPS else None)
    await publish(message.chat.id, day, png, caption, pin=False)


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
    await send_overview(message.chat.id, focus=sub[1] if sub[1] in GROUPS else None, pin=True)
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


@dp.message(Command("all"))
async def cmd_all(message: types.Message):
    if not get_all_saved():
        await message.answer("Дані ще завантажуються, спробуй за хвилину.")
        return
    if not USE_IMAGES:
        await message.answer("Картинки вимкнено (встанови pillow: python -m pip install pillow).")
        return
    await send_overview(message.chat.id)


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

    text = next_outage_text(grp, saved)
    await message.answer(text or "✅ Найближчим часом вимкнень не заплановано.", parse_mode="HTML")


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    if get_subscription(message.chat.id):
        remove_subscription(message.chat.id)
        await message.answer("🔕 Сповіщення вимкнено. Щоб підписатись знову — /start або /bind 5.1.")
    else:
        await message.answer("Ти ще не підписаний на жодну чергу.")


# ---------------------------------------------------------------- Main

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задано BOT_TOKEN! Створи файл .env поруч із bot.py: BOT_TOKEN=твій_токен")

    init_db()
    monitor_task = asyncio.create_task(monitor_loop())
    reminders_task = asyncio.create_task(reminders_loop())
    digest_task = asyncio.create_task(digest_loop())
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
        tasks = [monitor_task, reminders_task, digest_task]
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
