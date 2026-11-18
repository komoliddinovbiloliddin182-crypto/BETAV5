"""Telegram bot dispatchers: to'rtta shablonni to'liq ishlaydigan qiladi.

Shablonlar:
  - support : Yordam markazi (ticket tizimi, javob kuzatuvi)
  - shop    : Onlayn do'kon (katalog, savat, buyurtma)
  - quiz    : Quiz / Test (5 savol, ball, natija)
  - sub     : Obuna boti (obuna yo'riqnomasi, tekshiruv)

Har bir bot o'z ma'lumotlarini SQLite'da saqlaydi (do'kon mahsulotlari/buyurtmalari,
yordam ticketlari, quiz natijalari) — qayta ishga tushganda ham ma'lumot saqlanadi.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from db import db, db_lock

log = logging.getLogger("botbuilder.engine")

# Bot holatlari xotirada saqlanadi (qisqa muddatli: savat to'ldirish, savol yozish).
# Format: user_states[token_digest][tg_user_id] = {"step": "...", ...}
user_states: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(dict))

# Quiz savollari (umumiy bilim, O'zbekiston mavzusida).
QUIZ_QUESTIONS = [
    {
        "q": "O'zbekiston poytaxti qaysi shahar?",
        "options": ["Samarqand", "Toshkent", "Buxoro", "Xiva"],
        "correct": 1,
    },
    {
        "q": "O'zbekiston mustaqilligi qachon e'lon qilingan?",
        "options": ["1990-yil 1-sentabr", "1991-yil 1-sentabr", "1992-yil 1-sentabr", "1993-yil 1-sentabr"],
        "correct": 1,
    },
    {
        "q": "O'zbekiston milliy valyutasi nima?",
        "options": ["Rubl", "So'm", "Tenge", "Manat"],
        "correct": 1,
    },
    {
        "q": "O'zbekiston rasmiy tili qaysi?",
        "options": ["Rus tili", "O'zbek tili", "Qoraqalpoq tili", "Tojik tili"],
        "correct": 1,
    },
    {
        "q": "O'zbekiston bayrog'idagi yarim oy nechta yulduz bilan o'ralgan?",
        "options": ["10 ta", "11 ta", "12 ta", "13 ta"],
        "correct": 2,
    },
]

DEFAULT_PRODUCTS = [
    ("Telefon Samsung Galaxy A55", 4_500_000, "5G, 8GB RAM, 256GB memory"),
    ("Noutbook Lenovo IdeaPad", 7_200_000, "Intel i5, 16GB RAM, SSD 512GB"),
    ("AirPods Pro 2-s avlod", 1_350_000, "Faol shovqinni bastaklash, simsiz"),
    ("Power Bank 20000mAh", 320_000, "Tez quvvatlash, 2 ta port"),
    ("Smart soat Mi Band 8", 480_000, "Puls, qadamlar, uyqu kuzatuvi"),
]


def fmt_price(price: int) -> str:
    """Narxni o'qishga qulay formatga keltiradi: 4500000 -> 4 500 000 so'm."""
    return f"{price:,}".replace(",", " ") + " so'm"


async def check_trial(message: types.Message, token_digest: str) -> bool:
    """Botning sinov muddati hali amalda ekanligini tekshiradi."""
    from main import is_valid_trial

    async with db_lock:
        row = db.execute(
            "SELECT is_active, trial_ends_at FROM managed_bots WHERE token_hash=?",
            (token_digest,),
        ).fetchone()
        valid = bool(row) and is_valid_trial(row)
        if row and not valid and row["is_active"]:
            db.execute("UPDATE managed_bots SET is_active=0 WHERE token_hash=?", (token_digest,))
            db.commit()
    if not valid:
        await message.answer("⏳ Ushbu botning sinov muddati tugagan yoki bot vaqtincha to'xtatilgan.")
    return valid


def reply_menu(template: str):
    """Reply-klaviatura (asosiy menyu) — har bir shablon uchun."""
    kb = ReplyKeyboardBuilder()
    if template == "support":
        kb.button(text="💬 Savol yuborish")
        kb.button(text="📋 Mening so'rovlarim")
        kb.button(text="ℹ️ Yordam")
    elif template == "shop" or template in CATALOG_TEMPLATES:
        kb.button(text="🛍️ Katalog")
        kb.button(text="🛒 Savat")
        kb.button(text="📦 Buyurtmalarim")
        kb.button(text="☎️ Aloqa")
    elif template == "quiz":
        kb.button(text="🧠 Testni boshlash")
        kb.button(text="📊 Natijalarim")
        kb.button(text="ℹ️ Qoidalar")
    elif template == "sub":
        kb.button(text="📢 Obuna haqida")
        kb.button(text="✅ Obunani tekshirish")
        kb.button(text="☎️ Admin bilan aloqa")
    elif template == "navbat":
        kb.button(text="🎫 Navbat olish")
        kb.button(text="📅 Mening vaqtim")
        kb.button(text="ℹ️ Ma'lumot")
    elif template == "feedback":
        kb.button(text="⭐ Baholash")
        kb.button(text="📋 Mening baholarim")
        kb.button(text="ℹ️ Ma'lumot")
    elif template == "news":
        kb.button(text="📢 Obuna bo'lish")
        kb.button(text="🔕 Obunani bekor qilish")
        kb.button(text="ℹ️ Ma'lumot")
    elif template == "poll":
        kb.button(text="📊 Ovoz berish")
        kb.button(text="📈 Natijalar")
        kb.button(text="ℹ️ Ma'lumot")
    elif template == "info":
        kb.button(text="ℹ️ Biznes haqida")
        kb.button(text="📞 Aloqa")
        kb.button(text="📍 Manzil")
    elif template in REQUEST_TEMPLATES:
        kb.button(text="📝 So'rov yuborish")
        kb.button(text="📋 Mening so'rovlarim")
        kb.button(text="ℹ️ Ma'lumot")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


def admin_menu(template: str) -> "types.ReplyKeyboardMarkup":
    """Admin shablonlari (navbat/feedback/news/poll/info) uchun admin reply-klaviatura."""
    kb = ReplyKeyboardBuilder()
    if template == "navbat":
        kb.button(text="⚙️ Sozlash")
        kb.button(text="📋 Bugungi navbat")
        kb.button(text="📊 Statistika")
        kb.button(text="ℹ️ Ma'lumot")
    elif template == "feedback":
        kb.button(text="⭐ Barcha fikrlar")
        kb.button(text="📊 O'rtacha ball")
        kb.button(text="ℹ️ Ma'lumot")
    elif template == "news":
        kb.button(text="📨 Yangilik yuborish")
        kb.button(text="👥 Obunachilar")
        kb.button(text="ℹ️ Ma'lumot")
    elif template == "poll":
        kb.button(text="📝 So'rov yaratish")
        kb.button(text="📊 Faol so'rov")
        kb.button(text="ℹ️ Ma'lumot")
    elif template == "info":
        kb.button(text="⚙️ Ma'lumot sozlash")
        kb.button(text="👁 Ko'rish")
        kb.button(text="ℹ️ Ma'lumot")
    elif template in REQUEST_TEMPLATES:
        kb.button(text="📋 So'rovlar")
        kb.button(text="ℹ️ Ma'lumot")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


def admin_reply_menu() -> "types.ReplyKeyboardMarkup":
    """Eskicha alias — navbat admin menyu (avvalgi chaqiruvlar uchun)."""
    return admin_menu("navbat")


# ───────────────────────── SUPPORT BOT ─────────────────────────

async def support_create_ticket(bot_id: int, tg_user_id: int, username: str, message: str) -> int:
    """Yangi ticket yaratadi va uning raqamini qaytaradi."""
    async with db_lock:
        row = db.execute("SELECT COALESCE(MAX(ticket_no), 0) + 1 AS next_no FROM support_tickets WHERE bot_id=?", (bot_id,)).fetchone()
        ticket_no = row["next_no"]
        db.execute(
            "INSERT INTO support_tickets (bot_id, tg_user_id, tg_username, ticket_no, message, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (bot_id, tg_user_id, username, ticket_no, message, datetime.utcnow().isoformat()),
        )
        db.commit()
    return ticket_no


async def support_list_user_tickets(bot_id: int, tg_user_id: int) -> list:
    async with db_lock:
        rows = db.execute(
            "SELECT ticket_no, substr(message, 1, 60) AS preview, status, created_at "
            "FROM support_tickets WHERE bot_id=? AND tg_user_id=? ORDER BY id DESC LIMIT 10",
            (bot_id, tg_user_id),
        ).fetchall()
    return [dict(r) for r in rows]


# ───────────────────────── SHOP BOT ─────────────────────────

async def shop_list_products(bot_id: int) -> list:
    async with db_lock:
        rows = db.execute(
            "SELECT id, name, price, description FROM shop_products WHERE bot_id=? ORDER BY id",
            (bot_id,),
        ).fetchall()
    return [dict(r) for r in rows]


async def shop_cart_add(bot_id: int, tg_user_id: int, product_id: int) -> None:
    async with db_lock:
        db.execute(
            "INSERT INTO shop_cart (bot_id, tg_user_id, product_id, qty) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(bot_id, tg_user_id, product_id) DO UPDATE SET qty = qty + 1",
            (bot_id, tg_user_id, product_id),
        )
        db.commit()


async def shop_cart_list(bot_id: int, tg_user_id: int) -> list:
    async with db_lock:
        rows = db.execute(
            "SELECT c.product_id, c.qty, p.name, p.price FROM shop_cart c "
            "JOIN shop_products p ON c.product_id = p.id "
            "WHERE c.bot_id=? AND c.tg_user_id=? ORDER BY p.id",
            (bot_id, tg_user_id),
        ).fetchall()
    return [dict(r) for r in rows]


async def shop_cart_clear(bot_id: int, tg_user_id: int) -> None:
    async with db_lock:
        db.execute("DELETE FROM shop_cart WHERE bot_id=? AND tg_user_id=?", (bot_id, tg_user_id))
        db.commit()


async def shop_create_order(bot_id: int, tg_user_id: int, username: str, name: str, phone: str) -> int:
    items = await shop_cart_list(bot_id, tg_user_id)
    if not items:
        return 0
    total = sum(it["price"] * it["qty"] for it in items)
    items_json = json.dumps(
        [{"name": it["name"], "price": it["price"], "qty": it["qty"]} for it in items],
        ensure_ascii=False,
    )
    async with db_lock:
        cur = db.execute(
            "INSERT INTO shop_orders (bot_id, tg_user_id, tg_username, customer_name, customer_phone, "
            "items_json, total, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)",
            (bot_id, tg_user_id, username, name, phone, items_json, total, datetime.utcnow().isoformat()),
        )
        db.execute("DELETE FROM shop_cart WHERE bot_id=? AND tg_user_id=?", (bot_id, tg_user_id))
        db.commit()
        return cur.lastrowid


# ───────────────────────── QUIZ BOT ─────────────────────────

async def quiz_save_result(bot_id: int, tg_user_id: int, username: str, score: int, total: int) -> None:
    async with db_lock:
        db.execute(
            "INSERT INTO quiz_results (bot_id, tg_user_id, tg_username, score, total, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bot_id, tg_user_id, username, score, total, datetime.utcnow().isoformat()),
        )
        db.commit()


async def quiz_best_score(bot_id: int, tg_user_id: int) -> dict | None:
    async with db_lock:
        row = db.execute(
            "SELECT score, total, MAX(score) AS best FROM quiz_results WHERE bot_id=? AND tg_user_id=?",
            (bot_id, tg_user_id),
        ).fetchone()
    return dict(row) if row else None


async def quiz_last_results(bot_id: int, tg_user_id: int) -> list:
    async with db_lock:
        rows = db.execute(
            "SELECT score, total, created_at FROM quiz_results WHERE bot_id=? AND tg_user_id=? "
            "ORDER BY id DESC LIMIT 5",
            (bot_id, tg_user_id),
        ).fetchall()
    return [dict(r) for r in rows]


# ───────────────────────── NAVBAT BOT ─────────────────────────
# Navbat (queue) boti: birinchi /start yozgan foydalanuvchi admin bo'ladi.
# Admin biznes nomi, ish vaqti va har mijozga ajratiladigan daqiqani sozlaydi.
# Mijoz bo'sh vaqtlardan birini tanlaydi, ism va telefonini kiritadi — navbat yaratiladi.

# O'zbekiston vaqt zonasi (UTC+5). Render serveri UTC'da ishlaydi, lekin navbat
# vaqtlari mahalliy soatda ko'rsatiladi va shu soatda solishtiriladi.
TZ_OFFSET = timedelta(hours=5)


def now_local() -> datetime:
    """Mahalliy (Toshkent) vaqtni qaytaradi."""
    return datetime.utcnow() + TZ_OFFSET


def parse_hhmm(value: str) -> tuple[int, int]:
    """'09:00' -> (9, 0)."""
    h, m = value.strip().split(":")
    return int(h), int(m)


def valid_hhmm(value: str) -> bool:
    """'HH:MM' formati to'g'riligini tekshiradi."""
    value = value.strip()
    if ":" not in value:
        return False
    try:
        h, m = parse_hhmm(value)
        return 0 <= h <= 23 and 0 <= m <= 59
    except (ValueError, IndexError):
        return False


def fmt_slot_iso(iso: str) -> str:
    """'2026-11-18T13:00:00' -> '18.11 13:00'."""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d.%m %H:%M")
    except (ValueError, TypeError):
        return iso


def fmt_slot(dt: datetime) -> str:
    """datetime -> '18.11 13:00'."""
    return dt.strftime("%d.%m %H:%M")


async def navbat_get_admin(bot_id: int) -> dict | None:
    """Berilgan botning adminini qaytaradi (yoki None)."""
    async with db_lock:
        row = db.execute("SELECT admin_id, admin_username FROM bot_admins WHERE bot_id=?", (bot_id,)).fetchone()
    return dict(row) if row else None


async def navbat_set_admin(bot_id: int, admin_id: int, admin_username: str) -> int:
    """Bot adminini yozadi. Agar allaqachon admin bo'lsa, eski admin_id qaytariladi."""
    async with db_lock:
        existing = db.execute("SELECT admin_id FROM bot_admins WHERE bot_id=?", (bot_id,)).fetchone()
        if existing:
            return existing["admin_id"]
        db.execute(
            "INSERT INTO bot_admins (bot_id, admin_id, admin_username, created_at) VALUES (?, ?, ?, ?)",
            (bot_id, admin_id, admin_username, datetime.utcnow().isoformat()),
        )
        db.commit()
    return admin_id


async def navbat_get_settings(bot_id: int) -> dict:
    """Bot sozlamasini qaytaradi; agar yo'q bo'lsa, standart qiymat bilan yaratadi."""
    async with db_lock:
        row = db.execute("SELECT * FROM queue_settings WHERE bot_id=?", (bot_id,)).fetchone()
        if not row:
            db.execute(
                "INSERT INTO queue_settings (bot_id, business_name, work_start, work_end, minutes_per_client, updated_at) "
                "VALUES (?, 'Navbat boti', '09:00', '18:00', 60, ?)",
                (bot_id, datetime.utcnow().isoformat()),
            )
            db.commit()
            row = db.execute("SELECT * FROM queue_settings WHERE bot_id=?", (bot_id,)).fetchone()
    return dict(row)


async def navbat_update_settings(bot_id: int, business_name: str, work_start: str, work_end: str, minutes_per_client: int) -> None:
    async with db_lock:
        db.execute(
            "UPDATE queue_settings SET business_name=?, work_start=?, work_end=?, minutes_per_client=?, updated_at=? "
            "WHERE bot_id=?",
            (business_name, work_start, work_end, minutes_per_client, datetime.utcnow().isoformat(), bot_id),
        )
        db.commit()


async def navbat_available_slots(bot_id: int, count: int = 8) -> tuple[list[datetime], dict]:
    """Hozirdan boshlab bo'sh vaqt slotlarini topadi (ish soatlari ichida, 30 kun oldingacha)."""
    settings = await navbat_get_settings(bot_id)
    now = now_local()
    sh, sm = parse_hhmm(settings["work_start"])
    eh, em = parse_hhmm(settings["work_end"])
    minutes = settings["minutes_per_client"]
    async with db_lock:
        rows = db.execute(
            "SELECT slot_time FROM queue_entries WHERE bot_id=? AND status='waiting'",
            (bot_id,),
        ).fetchall()
    booked = sorted(datetime.fromisoformat(r["slot_time"]) for r in rows)
    slots: list[datetime] = []
    for day_offset in range(0, 30):
        if len(slots) >= count:
            break
        day = now.date() + timedelta(days=day_offset)
        day_start = datetime.combine(day, dtime(sh, sm))
        day_end = datetime.combine(day, dtime(eh, em))
        candidate = day_start
        while candidate + timedelta(minutes=minutes) <= day_end and len(slots) < count:
            slot_end = candidate + timedelta(minutes=minutes)
            # Bugungi o'tib ketgan slotlarni (boshlanish vaqti allaqachon o'tgan) o'tkazib yuboramiz.
            if day_offset == 0 and candidate < now:
                candidate += timedelta(minutes=minutes)
                continue
            conflict = any(
                b.date() == day and b < slot_end and (b + timedelta(minutes=minutes)) > candidate
                for b in booked
            )
            if not conflict:
                slots.append(candidate)
            candidate += timedelta(minutes=minutes)
    return slots, settings


async def navbat_create_booking(bot_id: int, tg_user_id: int, tg_username: str, name: str, phone: str, slot_time: str) -> int | None:
    """Yangi navbat yozadi. Agar vaqt band bo'lsa, None qaytaradi."""
    async with db_lock:
        existing = db.execute(
            "SELECT id FROM queue_entries WHERE bot_id=? AND slot_time=? AND status='waiting'",
            (bot_id, slot_time),
        ).fetchone()
        if existing:
            return None
        cur = db.execute(
            "INSERT INTO queue_entries (bot_id, tg_user_id, tg_username, customer_name, customer_phone, slot_time, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'waiting', ?)",
            (bot_id, tg_user_id, tg_username, name, phone, slot_time, datetime.utcnow().isoformat()),
        )
        db.commit()
        return cur.lastrowid


async def navbat_list_today(bot_id: int) -> tuple[list[dict], dict]:
    """Bugungi (mahalliy sana) navbatlar ro'yxati."""
    settings = await navbat_get_settings(bot_id)
    today = now_local().date().isoformat()
    async with db_lock:
        rows = db.execute(
            "SELECT id, customer_name, customer_phone, slot_time, status FROM queue_entries "
            "WHERE bot_id=? AND date(slot_time)=? ORDER BY slot_time",
            (bot_id, today),
        ).fetchall()
    return [dict(r) for r in rows], settings


async def navbat_my_slot(bot_id: int, tg_user_id: int) -> list[dict]:
    async with db_lock:
        rows = db.execute(
            "SELECT slot_time, status FROM queue_entries WHERE bot_id=? AND tg_user_id=? ORDER BY id DESC LIMIT 5",
            (bot_id, tg_user_id),
        ).fetchall()
    return [dict(r) for r in rows]


async def navbat_stats(bot_id: int) -> dict:
    today = now_local().date().isoformat()
    async with db_lock:
        total_today = db.execute(
            "SELECT COUNT(*) AS c FROM queue_entries WHERE bot_id=? AND date(slot_time)=?",
            (bot_id, today),
        ).fetchone()["c"]
        waiting = db.execute(
            "SELECT COUNT(*) AS c FROM queue_entries WHERE bot_id=? AND status='waiting' AND date(slot_time)=?",
            (bot_id, today),
        ).fetchone()["c"]
        done = db.execute(
            "SELECT COUNT(*) AS c FROM queue_entries WHERE bot_id=? AND status='done' AND date(slot_time)=?",
            (bot_id, today),
        ).fetchone()["c"]
        total_all = db.execute(
            "SELECT COUNT(*) AS c FROM queue_entries WHERE bot_id=?",
            (bot_id,),
        ).fetchone()["c"]
    return {"total_today": total_today, "waiting": waiting, "done": done, "total_all": total_all}


async def navbat_start_handler(message: types.Message, bot_id: int, states: dict) -> None:
    """Navbat botining /start handleri: birinchi foydalanuvchi admin bo'ladi."""
    uid = message.from_user.id if message.from_user else 0
    uname = (
        ("@" + message.from_user.username) if (message.from_user and message.from_user.username)
        else (message.from_user.full_name if message.from_user else "Admin")
    )
    admin = await navbat_get_admin(bot_id)
    settings = await navbat_get_settings(bot_id)
    states.pop(uid, None)
    if admin is None:
        await navbat_set_admin(bot_id, uid, uname)
        await message.answer(
            "🎉 Tabriklaymiz! Siz bu botning ADMINIsiz.\n\n"
            "Botni birinchi bo'lib ishga tushirgan foydalanuvchi admin bo'ldi.\n\n"
            "Endi navbat tizimini sozlashingiz kerak. Standart sozlama:\n"
            f"• Biznes nomi: {settings['business_name']}\n"
            f"• Ish vaqti: {settings['work_start']} - {settings['work_end']}\n"
            f"• Har mijozga: {settings['minutes_per_client']} daqiqa\n\n"
            "O'zgartirish uchun \"⚙️ Sozlash\" tugmasini bosing.",
            reply_markup=admin_reply_menu(),
        )
        return
    if admin["admin_id"] == uid:
        await message.answer(
            "👋 Admin paneliga xush kelibsiz!\n\n"
            f"📋 Biznes: {settings['business_name']}\n"
            f"🕐 Ish vaqti: {settings['work_start']} - {settings['work_end']}\n"
            f"⏱ Har mijozga: {settings['minutes_per_client']} daqiqa\n\n"
            "Mijozlar navbat olishi uchun ularga bot havolasini yuboring.",
            reply_markup=admin_reply_menu(),
        )
        return
    await message.answer(
        f"Assalomu alaykum! 🎫\n"
        f"{settings['business_name']} navbat botiga xush kelibsiz.\n\n"
        f"🕐 Ish vaqti: {settings['work_start']} - {settings['work_end']}\n"
        f"⏱ Har mijozga: {settings['minutes_per_client']} daqiqa ajratiladi.\n\n"
        f"\"🎫 Navbat olish\" tugmasini bosing va bo'sh vaqtdan birini tanlang.",
        reply_markup=reply_menu("navbat"),
    )


# ───────────────────────── GENERIC ADMIN TEMPLATES ─────────────────────────
# feedback / news / poll / info shablonlari ham navbat kabi "birinchi /start = admin"
# printsipiga ishlaydi. bot_admins jadvali umumiy (har bir bot uchun bitta admin).

ADMIN_TEMPLATES = {"navbat", "feedback", "news", "poll", "info"}


async def get_bot_admin(bot_id: int) -> dict | None:
    """Bot adminini qaytaradi (umumiy — barcha admin shablonlari uchun)."""
    async with db_lock:
        row = db.execute("SELECT admin_id, admin_username FROM bot_admins WHERE bot_id=?", (bot_id,)).fetchone()
    return dict(row) if row else None


async def set_bot_admin(bot_id: int, admin_id: int, admin_username: str) -> int:
    """Birinchi adminni yozadi; agar allaqachon bo'lsa, eski admin_id qaytaradi."""
    async with db_lock:
        existing = db.execute("SELECT admin_id FROM bot_admins WHERE bot_id=?", (bot_id,)).fetchone()
        if existing:
            return existing["admin_id"]
        db.execute(
            "INSERT INTO bot_admins (bot_id, admin_id, admin_username, created_at) VALUES (?, ?, ?, ?)",
            (bot_id, admin_id, admin_username, datetime.utcnow().isoformat()),
        )
        db.commit()
    return admin_id


# Birinchi /start — admin salomi (har bir admin shabloni uchun).
ADMIN_FIRST_WELCOME = {
    "feedback": (
        "🎉 Tabriklaymiz! Siz bu botning ADMINIsiz.\n\n"
        "Birinchi bo'lib ishga tushirgan foydalanuvchi admin bo'ldi.\n\n"
        "Mijozlar sizning biznesingizni baholaydi (1-5 ⭐) va izoh qoldiradi.\n"
        "Siz \"⭐ Barcha fikrlar\" va \"📊 O'rtacha ball\" orqali fikrlarni ko'rasiz."
    ),
    "news": (
        "🎉 Tabriklaymiz! Siz bu botning ADMINIsiz.\n\n"
        "Birinchi bo'lib ishga tushirgan foydalanuvchi admin bo'ldi.\n\n"
        "Mijozlar \"📢 Obuna bo'lish\" tugmasini bosib obunachi bo'ladi.\n"
        "Siz \"📨 Yangilik yuborish\" orqali matn yuborasiz — u barcha obunachilarga tarqatiladi."
    ),
    "poll": (
        "🎉 Tabriklaymiz! Siz bu botning ADMINIsiz.\n\n"
        "Birinchi bo'lib ishga tushirgan foydalanuvchi admin bo'ldi.\n\n"
        "Siz \"📝 So'rov yaratish\" orqali savol va variantlarni kiritasiz.\n"
        "Mijozlar ovoz beradi va natijani ko'radi."
    ),
    "info": (
        "🎉 Tabriklaymiz! Siz bu botning ADMINIsiz.\n\n"
        "Birinchi bo'lib ishga tushirgan foydalanuvchi admin bo'ldi.\n\n"
        "Siz \"⚙️ Ma'lumot sozlash\" orqali biznes nomi, haqida, xizmatlar, telefon, "
        "manzil va ish vaqtini kiritasiz. Mijozlar buni ko'radi."
    ),
}

ADMIN_RETURN_WELCOME = {
    "feedback": "👋 Admin paneliga xush kelibsiz!\n\n⭐ Barcha fikrlar · 📊 O'rtacha ball",
    "news": "👋 Admin paneliga xush kelibsiz!\n\n📨 Yangilik yuborish · 👥 Obunachilar",
    "poll": "👋 Admin paneliga xush kelibsiz!\n\n📝 So'rov yaratish · 📊 Faol so'rov",
    "info": "👋 Admin paneliga xush kelibsiz!\n\n⚙️ Ma'lumot sozlash · 👁 Ko'rish",
}

CUSTOMER_GREETING = {
    "feedback": (
        "Assalomu alaykum! ⭐\nBiznesni baholash botiga xush kelibsiz.\n\n"
        "\"⭐ Baholash\" tugmasini bosing va 1-5 yulduz baho qoldiring."
    ),
    "news": (
        "Assalomu alaykum! 📨\nYangiliklar botiga xush kelibsiz.\n\n"
        "\"📢 Obuna bo'lish\" tugmasini bosing va yangiliklardan xabardor bo'ling."
    ),
    "poll": (
        "Assalomu alaykum! 📊\nSo'rovnomalar botiga xush kelibsiz.\n\n"
        "\"📊 Ovoz berish\" tugmasini bosing va fikringizni bildiring."
    ),
    "info": (
        "Assalomu alaykum! ℹ️\nBiznes ma'lumot botiga xush kelibsiz.\n\n"
        "\"ℹ️ Biznes haqida\" tugmasini bosing."
    ),
}


async def first_admin_start(message: types.Message, bot_id: int, states: dict, template: str) -> None:
    """feedback/news/poll/info uchun /start: birinchi foydalanuvchi admin bo'ladi."""
    uid = message.from_user.id if message.from_user else 0
    uname = (
        ("@" + message.from_user.username) if (message.from_user and message.from_user.username)
        else (message.from_user.full_name if message.from_user else "Admin")
    )
    admin = await get_bot_admin(bot_id)
    states.pop(uid, None)
    if admin is None:
        await set_bot_admin(bot_id, uid, uname)
        await message.answer(ADMIN_FIRST_WELCOME[template], reply_markup=admin_menu(template))
        return
    if admin["admin_id"] == uid:
        await message.answer(ADMIN_RETURN_WELCOME[template], reply_markup=admin_menu(template))
        return
    await message.answer(CUSTOMER_GREETING[template], reply_markup=reply_menu(template))


# ── FEEDBACK helpers ──
async def feedback_save(bot_id: int, tg_user_id: int, tg_username: str, rating: int, comment: str) -> int:
    async with db_lock:
        cur = db.execute(
            "INSERT INTO feedback_entries (bot_id, tg_user_id, tg_username, rating, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bot_id, tg_user_id, tg_username, rating, comment, datetime.utcnow().isoformat()),
        )
        db.commit()
        return cur.lastrowid


async def feedback_list_user(bot_id: int, tg_user_id: int) -> list:
    async with db_lock:
        rows = db.execute(
            "SELECT rating, comment, created_at FROM feedback_entries WHERE bot_id=? AND tg_user_id=? ORDER BY id DESC LIMIT 10",
            (bot_id, tg_user_id),
        ).fetchall()
    return [dict(r) for r in rows]


async def feedback_list_all(bot_id: int, limit: int = 15) -> list:
    async with db_lock:
        rows = db.execute(
            "SELECT rating, comment, tg_username, created_at FROM feedback_entries WHERE bot_id=? ORDER BY id DESC LIMIT ?",
            (bot_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


async def feedback_average(bot_id: int) -> dict:
    async with db_lock:
        row = db.execute(
            "SELECT COUNT(*) AS c, AVG(rating) AS avg FROM feedback_entries WHERE bot_id=?",
            (bot_id,),
        ).fetchone()
    return {"count": row["c"] if row else 0, "avg": round(row["avg"], 2) if row and row["avg"] else 0}


# ── NEWS helpers ──
async def news_subscribe(bot_id: int, tg_user_id: int, tg_username: str) -> bool:
    """Obuna qo'shadi. Agar allaqachon bo'lsa, False qaytaradi."""
    async with db_lock:
        existing = db.execute("SELECT id FROM news_subscribers WHERE bot_id=? AND tg_user_id=?", (bot_id, tg_user_id)).fetchone()
        if existing:
            return False
        db.execute(
            "INSERT INTO news_subscribers (bot_id, tg_user_id, tg_username, subscribed_at) VALUES (?, ?, ?, ?)",
            (bot_id, tg_user_id, tg_username, datetime.utcnow().isoformat()),
        )
        db.commit()
    return True


async def news_unsubscribe(bot_id: int, tg_user_id: int) -> bool:
    """Obunani o'chiradi. Agar obuna yo'q bo'lsa, False qaytaradi."""
    async with db_lock:
        cur = db.execute("DELETE FROM news_subscribers WHERE bot_id=? AND tg_user_id=?", (bot_id, tg_user_id))
        db.commit()
    return cur.rowcount > 0


async def news_subscriber_count(bot_id: int) -> int:
    async with db_lock:
        row = db.execute("SELECT COUNT(*) AS c FROM news_subscribers WHERE bot_id=?", (bot_id,)).fetchone()
    return row["c"] if row else 0


async def news_subscriber_ids(bot_id: int) -> list:
    async with db_lock:
        rows = db.execute("SELECT tg_user_id FROM news_subscribers WHERE bot_id=?", (bot_id,)).fetchall()
    return [r["tg_user_id"] for r in rows]


async def news_create_post(bot_id: int, body: str) -> int:
    async with db_lock:
        cur = db.execute(
            "INSERT INTO news_posts (bot_id, body, sent_count, created_at) VALUES (?, ?, 0, ?)",
            (bot_id, body, datetime.utcnow().isoformat()),
        )
        db.commit()
        return cur.lastrowid


async def news_mark_sent(post_id: int, sent_count: int) -> None:
    async with db_lock:
        db.execute("UPDATE news_posts SET sent_count=? WHERE id=?", (sent_count, post_id))
        db.commit()


# ── POLL helpers ──
async def poll_create(bot_id: int, question: str, options: list) -> int:
    """Yangi so'rov yaratadi, eski faol so'rovni o'chiradi (faqat bitta faol bo'lsin)."""
    async with db_lock:
        db.execute("UPDATE polls SET is_active=0 WHERE bot_id=? AND is_active=1", (bot_id,))
        cur = db.execute(
            "INSERT INTO polls (bot_id, question, options_json, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
            (bot_id, question, json.dumps(options, ensure_ascii=False), datetime.utcnow().isoformat()),
        )
        db.commit()
        return cur.lastrowid


async def poll_get_active(bot_id: int) -> dict | None:
    async with db_lock:
        row = db.execute("SELECT * FROM polls WHERE bot_id=? AND is_active=1 ORDER BY id DESC LIMIT 1", (bot_id,)).fetchone()
    return dict(row) if row else None


async def poll_vote(poll_id: int, tg_user_id: int, choice: int) -> bool:
    """Ovoz qo'shadi. Agar allaqachon ovoz bergan bo'lsa, False qaytaradi."""
    async with db_lock:
        existing = db.execute("SELECT id FROM poll_votes WHERE poll_id=? AND tg_user_id=?", (poll_id, tg_user_id)).fetchone()
        if existing:
            return False
        db.execute(
            "INSERT INTO poll_votes (poll_id, tg_user_id, choice, created_at) VALUES (?, ?, ?, ?)",
            (poll_id, tg_user_id, choice, datetime.utcnow().isoformat()),
        )
        db.commit()
    return True


async def poll_results(poll_id: int) -> list:
    async with db_lock:
        rows = db.execute(
            "SELECT choice, COUNT(*) AS c FROM poll_votes WHERE poll_id=? GROUP BY choice ORDER BY choice",
            (poll_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── INFO helpers ──
async def info_get_settings(bot_id: int) -> dict:
    async with db_lock:
        row = db.execute("SELECT * FROM info_settings WHERE bot_id=?", (bot_id,)).fetchone()
        if not row:
            db.execute(
                "INSERT INTO info_settings (bot_id, business_name, about, services, phone, address, work_hours, updated_at) "
                "VALUES (?, 'Biznes', '', '', '', '', '', ?)",
                (bot_id, datetime.utcnow().isoformat()),
            )
            db.commit()
            row = db.execute("SELECT * FROM info_settings WHERE bot_id=?", (bot_id,)).fetchone()
    return dict(row)


async def info_update_settings(bot_id: int, fields: dict) -> None:
    async with db_lock:
        db.execute(
            "UPDATE info_settings SET business_name=?, about=?, services=?, phone=?, address=?, work_hours=?, updated_at=? "
            "WHERE bot_id=?",
            (
                fields.get("business_name", "Biznes"),
                fields.get("about", ""),
                fields.get("services", ""),
                fields.get("phone", ""),
                fields.get("address", ""),
                fields.get("work_hours", ""),
                datetime.utcnow().isoformat(),
                bot_id,
            ),
        )
        db.commit()


# ───────────────────────── REQUEST ENGINE (15 biznes shabloni) ─────────────────────────
# Har xil bizneslar uchun "so'rov yig'ish" shablonlari — bitta umumiy engine bilan ishlaydi.
# Mijoz formani to'ldiradi (maydonlar ketma-ket) → contact_requests jadvaliga yoziladi →
# adminga xabar boradi. Admin "📋 So'rovlar" orqali barchasini ko'radi va ✅ belgilaydi.

REQUEST_TEMPLATES: dict[str, dict] = {
    "callback": {
        "label": "Qayta aloqa",
        "ico": "📞",
        "greeting": "Assalomu alaykum! 📞\nQayta aloqa botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing — biz sizga qo'ng'iroq qilamiz.",
        "fields": [("name", "✍️ Ismingizni kiriting:"), ("phone", "📞 Telefon raqamingizni kiriting:")],
    },
    "consultation": {
        "label": "Konsultatsiya",
        "ico": "🩺",
        "greeting": "Assalomu alaykum! 🩺\nBepul konsultatsiya botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing — mutaxassis siz bilan bog'lanadi.",
        "fields": [("name", "✍️ Ismingiz:"), ("phone", "📞 Telefon:"), ("question", "❓ Savolingizni yozing:")],
    },
    "complaint": {
        "label": "Shikoyat/Taklif",
        "ico": "📝",
        "greeting": "Assalomu alaykum! 📝\nShikoyat va takliflar botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("message", "✍️ Shikoyat yoki taklifingizni yozing:"), ("phone", "📞 Telefon (ixtiyoriy, yoki \"-\"):")],
    },
    "quote": {
        "label": "Narx so'rash",
        "ico": "💰",
        "greeting": "Assalomu alaykum! 💰\nNarx/baho so'rash botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("what", "✍️ Nima kerakligini yozing:"), ("phone", "📞 Telefon:")],
    },
    "delivery": {
        "label": "Yetkazib berish",
        "ico": "🚚",
        "greeting": "Assalomu alaykum! 🚚\nYetkazib berish buyurtmasi botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("address", "📍 Manzilingizni yozing:"), ("phone", "📞 Telefon:"), ("note", "📝 Izoh (yoki \"-\"):")],
    },
    "measure": {
        "label": "O'lchovga kelish",
        "ico": "📐",
        "greeting": "Assalomu alaykum! 📐\nO'lchovga kelish botiga xush kelibsiz (mebel, deraza, h.k.).\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("name", "✍️ Ismingiz:"), ("phone", "📞 Telefon:"), ("when", "🕒 Qachon kelasiz:")],
    },
    "master": {
        "label": "Usta chaqirish",
        "ico": "🔧",
        "greeting": "Assalomu alaykum! 🔧\nUsta/mutaxassis chaqirish botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("service", "✍️ Qanday usta kerak (santexnik, elektr, h.k.):"), ("phone", "📞 Telefon:"), ("problem", "🔧 Muammoni yozing:")],
    },
    "reservation": {
        "label": "Bron qilish",
        "ico": "📅",
        "greeting": "Assalomu alaykum! 📅\nBron qilish botiga xush kelibsiz (restoran, zal, h.k.).\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("name", "✍️ Ismingiz:"), ("count", "👥 Odamlar soni:"), ("when", "🕒 Vaqt:"), ("phone", "📞 Telefon:")],
    },
    "visit": {
        "label": "Tashrif",
        "ico": "🚪",
        "greeting": "Assalomu alaykum! 🚪\nTashrif buyurtish botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("name", "✍️ Ismingiz:"), ("phone", "📞 Telefon:"), ("when", "🕒 Tashrif vaqti:")],
    },
    "application": {
        "label": "Ariza",
        "ico": "📄",
        "greeting": "Assalomu alaykum! 📄\nOnlayn ariza botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("name", "✍️ Ismingiz:"), ("phone", "📞 Telefon:"), ("message", "📝 Arizangizni yozing:")],
    },
    "repair": {
        "label": "Ta'mirga qabul",
        "ico": "🛠️",
        "greeting": "Assalomu alaykum! 🛠️\nTexnika ta'mirga qabul qilish botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("device", "✍️ Qanday texnika (telefon, TV, h.k.):"), ("problem", "🔧 Muammo:"), ("phone", "📞 Telefon:")],
    },
    "cleaning": {
        "label": "Tozalash xizmati",
        "ico": "🧹",
        "greeting": "Assalomu alaykum! 🧹\nTozalash (kliniy) xizmati botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("address", "📍 Manzil:"), ("phone", "📞 Telefon:"), ("note", "📝 Izoh (yoki \"-\"):")],
    },
    "tour": {
        "label": "Tur/Ekskursiya",
        "ico": "✈️",
        "greeting": "Assalomu alaykum! ✈️\nTur va ekskursiyalarga yozilish botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("name", "✍️ Ismingiz:"), ("phone", "📞 Telefon:"), ("count", "👥 Odamlar soni:")],
    },
    "estimate": {
        "label": "Smeta",
        "ico": "🧮",
        "greeting": "Assalomu alaykum! 🧮\nSmeta/hisob so'rash botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("what", "✍️ Loyiha/ishni yozing:"), ("phone", "📞 Telefon:")],
    },
    "partner": {
        "label": "Hamkorlik",
        "ico": "🤝",
        "greeting": "Assalomu alaykum! 🤝\nHamkorlik taklifi botiga xush kelibsiz.\n\"📝 So'rov yuborish\" tugmasini bosing.",
        "fields": [("name", "✍️ Ismingiz:"), ("company", "🏢 Kompaniya:"), ("phone", "📞 Telefon:"), ("offer", "💼 Taklifingiz:")],
    },
}

# Katalog shablonlari — shop dispatcher'ini qayta ishlatadi (faqat greeting farq qiladi).
CATALOG_TEMPLATES = {
    "restaurant": {"label": "Restoran/kafe", "ico": "🍽️", "greeting": "Assalomu alaykum! 🍽️\nRestoran botiga xush kelibsiz!\nMenyudan taom tanlang, savatga qo'shing va buyurtma bering."},
    "pharmacy": {"label": "Dorixona", "ico": "💊", "greeting": "Assalomu alaykum! 💊\nDorixona botiga xush kelibsiz!\nKatalogdan dori tanlang va buyurtma bering."},
    "market": {"label": "Oziq-ovqat", "ico": "🥬", "greeting": "Assalomu alaykum! 🥬\nMahsulotlar do'koni botiga xush kelibsiz!\nKatalogdan tanlang, savatga qo'shing va buyurtma bering."},
    "flowers": {"label": "Gul do'koni", "ico": "💐", "greeting": "Assalomu alaykum! 💐\nGul do'koni botiga xush kelibsiz!\nKatalogdan gul tanlang va buyurtma bering."},
    "auto": {"label": "Avtozapchast", "ico": "🔩", "greeting": "Assalomu alaykum! 🔩\nAvtozapchast botiga xush kelibsiz!\nKatalogdan qismlarni tanlang va buyurtma bering."},
    "books": {"label": "Kitob do'koni", "ico": "📚", "greeting": "Assalomu alaykum! 📚\nKitob do'koni botiga xush kelibsiz!\nKatalogdan kitob tanlang va buyurtma bering."},
}


async def request_save(bot_id: int, template: str, tg_user_id: int, tg_username: str, data: dict) -> int:
    async with db_lock:
        cur = db.execute(
            "INSERT INTO contact_requests (bot_id, template, tg_user_id, tg_username, data_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'new', ?)",
            (bot_id, template, tg_user_id, tg_username, json.dumps(data, ensure_ascii=False), datetime.utcnow().isoformat()),
        )
        db.commit()
        return cur.lastrowid


async def request_list_user(bot_id: int, tg_user_id: int, limit: int = 10) -> list:
    async with db_lock:
        rows = db.execute(
            "SELECT id, data_json, status, created_at FROM contact_requests WHERE bot_id=? AND tg_user_id=? ORDER BY id DESC LIMIT ?",
            (bot_id, tg_user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


async def request_list_all(bot_id: int, limit: int = 15) -> list:
    async with db_lock:
        rows = db.execute(
            "SELECT id, data_json, tg_username, status, created_at FROM contact_requests WHERE bot_id=? ORDER BY id DESC LIMIT ?",
            (bot_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


async def request_mark_done(req_id: int) -> bool:
    async with db_lock:
        cur = db.execute("UPDATE contact_requests SET status='done' WHERE id=? AND status='new'", (req_id,))
        db.commit()
    return cur.rowcount > 0


async def request_start(message: types.Message, bot_id: int, states: dict, template: str) -> None:
    """Request-engine uchun /start: birinchi foydalanuvchi admin."""
    uid = message.from_user.id if message.from_user else 0
    uname = (
        ("@" + message.from_user.username) if (message.from_user and message.from_user.username)
        else (message.from_user.full_name if message.from_user else "Admin")
    )
    admin = await get_bot_admin(bot_id)
    states.pop(uid, None)
    cfg = REQUEST_TEMPLATES[template]
    if admin is None:
        await set_bot_admin(bot_id, uid, uname)
        await message.answer(
            f"🎉 Tabriklaymiz! Siz bu botning ADMINIsiz.\n\n"
            f"Bu {cfg['label']} boti. Mijozlar so'rov yuboradi, siz \"📋 So'rovlar\" orqali ko'rasiz.",
            reply_markup=admin_menu(template),
        )
        return
    if admin["admin_id"] == uid:
        await message.answer("👋 Admin paneliga xush kelibsiz!\n\n📋 So'rovlar", reply_markup=admin_menu(template))
        return
    await message.answer(cfg["greeting"], reply_markup=reply_menu(template))


# ───────────────────────── DISPATCHER ─────────────────────────

def build_dispatcher(token: str, template: str, bot_id: int, td: str) -> Dispatcher:
    """Berilgan shablon uchun to'liq ishlaydigan dispatcher yaratadi.

    token  — Telegram bot tokeni
    template — support/shop/quiz/sub
    bot_id — managed_bots.id (ma'lumotlar bazasi so'rovlari uchun)
    td     — token_digest (sinov muddatini tekshirish uchun)
    """
    dp = Dispatcher()
    states = user_states[td]

    async def trial_ok(message: types.Message) -> bool:
        return await check_trial(message, td)

    def tg_user(message: types.Message) -> tuple[int, str]:
        uid = message.from_user.id if message.from_user else 0
        uname = ("@" + message.from_user.username) if (message.from_user and message.from_user.username) else (message.from_user.full_name if message.from_user else "Foydalanuvchi")
        return uid, uname

    # ── /start va /help ──
    @dp.message(CommandStart())
    async def start(message: types.Message):
        if not await trial_ok(message):
            return
        # NAVBAT: birinchi /start yozgan foydalanuvchi admin bo'ladi.
        if template == "navbat":
            await navbat_start_handler(message, bot_id, states)
            return
        # feedback / news / poll / info — ham birinchi /start = admin printsipi.
        if template in ("feedback", "news", "poll", "info"):
            await first_admin_start(message, bot_id, states, template)
            return
        # request-engine shablonlari (callback, consultation, ...) — ham birinchi /start = admin.
        if template in REQUEST_TEMPLATES:
            await request_start(message, bot_id, states, template)
            return
        greetings = {
            "support": "Assalomu alaykum! 👋\nYordam markaziga xush kelibsiz.\nSavolingizni yuboring — biz uni qabul qilamiz va raqami bilan tasdiqlaymiz.",
            "shop": "Assalomu alaykum! 🛍️\nDo'kon botiga xush kelibsiz!\nKatalogdan mahsulot tanlang, savatga qo'shing va buyurtma bering.",
            "quiz": "Salom! 🧠\nBilimingizni sinab ko'ring! Test 5 savoldan iborat. \"Testni boshlash\" tugmasini bosing.",
            "sub": "Assalomu alaykum! 🔐\nObuna botiga xush kelibsiz. Obuna tartibi haqida ma'lumot oling va obunangizni tekshiring.",
        }
        states.pop(message.from_user.id if message.from_user else 0, None)
        greeting = greetings.get(template) or CATALOG_TEMPLATES.get(template, {}).get("greeting", "Assalomu alaykum!")
        await message.answer(greeting, reply_markup=reply_menu(template))

    @dp.message(Command("help"))
    async def help_cmd(message: types.Message):
        if await trial_ok(message):
            await message.answer(
                "Mavjud buyruqlar:\n/start — bosh menyu\n/help — yordam\nQuyidagi tugmalardan foydalaning.",
                reply_markup=reply_menu(template),
            )

    # ── SUPPORT: savol yuborish ──
    if template == "support":
        @dp.message(F.text == "💬 Savol yuborish")
        async def ask_question(message: types.Message):
            if await trial_ok(message):
                states[message.from_user.id] = {"step": "awaiting_question"}
                await message.answer("✍️ Savolingizni yozib yuboring. Operator ko'rib chiqadi va javob beradi.")

        @dp.message(F.text == "📋 Mening so'rovlarim")
        async def my_tickets(message: types.Message):
            if not await trial_ok(message):
                return
            uid, _ = tg_user(message)
            tickets = await support_list_user_tickets(bot_id, uid)
            if not tickets:
                await message.answer("Sizda hozircha so'rovlar yo'q.")
                return
            lines = ["📋 Sizning so'rovlaringiz:\n"]
            for t in tickets:
                preview = t["preview"] or ""
                lines.append(f"№{t['ticket_no']} — {t['status']} — {preview}...")
            await message.answer("\n".join(lines), reply_markup=reply_menu(template))

        @dp.message(F.text == "ℹ️ Yordam")
        async def support_help(message: types.Message):
            if await trial_ok(message):
                await message.answer("Savolingizni yozib yuboring — biz uni qabul qilamiz va raqam bilan tasdiqlaymiz. ✅")

        @dp.message()
        async def support_fallback(message: types.Message):
            if not await trial_ok(message):
                return
            uid, uname = tg_user(message)
            st = states.get(uid)
            if st and st.get("step") == "awaiting_question":
                states.pop(uid, None)
                ticket_no = await support_create_ticket(bot_id, uid, uname, message.text or "")
                await message.answer(
                    f"✅ So'rovingiz qabul qilindi!\n\n🎫 Ticket raqami: #{ticket_no}\n"
                    f"Operator tez orada javob beradi.",
                    reply_markup=reply_menu(template),
                )
            else:
                await message.answer(
                    "Savolingizni yozing yoki \"💬 Savol yuborish\" tugmasini bosing.",
                    reply_markup=reply_menu(template),
                )

    # ── SHOP: katalog, savat, buyurtma (catalog-family shablonlari ham shu erda) ──
    elif template == "shop" or template in CATALOG_TEMPLATES:
        @dp.message(F.text == "🛍️ Katalog")
        async def catalog(message: types.Message):
            if not await trial_ok(message):
                return
            products = await shop_list_products(bot_id)
            if not products:
                await message.answer("🛍️ Hozircha mahsulotlar yo'q. Bot egasi tez orada katalogni to'ldiradi.")
                return
            kb = InlineKeyboardBuilder()
            for p in products:
                kb.button(text=f"{p['name']} — {fmt_price(p['price'])}", callback_data=f"add:{p['id']}")
            kb.adjust(1)
            await message.answer("🛍️ Mahsulotlar katalogi — tanlash uchun bosing:", reply_markup=kb.as_markup())

        @dp.callback_query(F.data.startswith("add:"))
        async def add_to_cart(callback: types.CallbackQuery):
            try:
                product_id = int(callback.data.split(":", 1)[1])
            except (ValueError, IndexError):
                await callback.answer("Noto'g'ri mahsulot.")
                return
            uid = callback.from_user.id
            await shop_cart_add(bot_id, uid, product_id)
            await callback.answer("✅ Savatga qo'shildi!")

        @dp.message(F.text == "🛒 Savat")
        async def cart_view(message: types.Message):
            if not await trial_ok(message):
                return
            uid, _ = tg_user(message)
            items = await shop_cart_list(bot_id, uid)
            if not items:
                await message.answer("🛒 Savatingiz bo'sh. Katalogdan mahsulot tanlang.")
                return
            lines = ["🛒 Sizning savatingiz:\n"]
            total = 0
            for it in items:
                lines.append(f"• {it['name']} ×{it['qty']} = {fmt_price(it['price'] * it['qty'])}")
                total += it["price"] * it["qty"]
            lines.append(f"\n💰 Jami: {fmt_price(total)}")
            kb = InlineKeyboardBuilder()
            kb.button(text="✅ Buyurtma berish", callback_data="checkout")
            kb.button(text="🗑 Savatni tozalash", callback_data="clearcart")
            kb.adjust(1)
            await message.answer("\n".join(lines), reply_markup=kb.as_markup())

        @dp.callback_query(F.data == "clearcart")
        async def clear_cart(callback: types.CallbackQuery):
            uid = callback.from_user.id
            await shop_cart_clear(bot_id, uid)
            await callback.answer("Savat tozalandi.")
            await callback.message.edit_text("🛒 Savat tozalandi. Yangi mahsulot tanlang.")

        @dp.callback_query(F.data == "checkout")
        async def checkout_start(callback: types.CallbackQuery):
            uid = callback.from_user.id
            items = await shop_cart_list(bot_id, uid)
            if not items:
                await callback.answer("Savat bo'sh!")
                return
            await callback.answer()
            states[uid] = {"step": "awaiting_name"}
            await callback.message.answer("📝 Buyurtma berish uchun ismingizni yozing:")

        @dp.message(F.text == "📦 Buyurtmalarim")
        async def my_orders(message: types.Message):
            if not await trial_ok(message):
                return
            uid, _ = tg_user(message)
            async with db_lock:
                rows = db.execute(
                    "SELECT id, total, status, created_at, items_json FROM shop_orders "
                    "WHERE bot_id=? AND tg_user_id=? ORDER BY id DESC LIMIT 5",
                    (bot_id, uid),
                ).fetchall()
            if not rows:
                await message.answer("📦 Sizda hozircha buyurtmalar yo'q.")
                return
            lines = ["📦 Buyurtmalaringiz:\n"]
            for r in rows:
                created = r["created_at"][:16].replace("T", " ")
                lines.append(f"№{r['id']} — {fmt_price(r['total'])} — {r['status']} — {created}")
            await message.answer("\n".join(lines), reply_markup=reply_menu(template))

        @dp.message(F.text == "☎️ Aloqa")
        async def shop_contact(message: types.Message):
            if await trial_ok(message):
                await message.answer(
                    "☎️ Aloqa: buyurtmalar shu bot orqali qabul qilinadi.\n"
                    "Bot egasi bilan bog'lanish uchun saytdagi \"Boshqarish\" bo'limidan foydalaning."
                )

        @dp.message()
        async def shop_fallback(message: types.Message):
            if not await trial_ok(message):
                return
            uid, uname = tg_user(message)
            st = states.get(uid)
            if st and st.get("step") == "awaiting_name":
                st["step"] = "awaiting_phone"
                st["name"] = (message.text or "").strip()
                await message.answer("📞 Endi telefon raqamingizni yuboring (masalan: +998901234567):")
                return
            if st and st.get("step") == "awaiting_phone":
                name = st.get("name", "")
                phone = (message.text or "").strip()
                states.pop(uid, None)
                order_id = await shop_create_order(bot_id, uid, uname, name, phone)
                if order_id:
                    await message.answer(
                        f"🎉 Buyurtma qabul qilindi!\n\n📦 Buyurtma №{order_id}\n"
                        f"👤 {name}\n📞 {phone}\n\nOperator tez orada bog'lanadi.",
                        reply_markup=reply_menu(template),
                    )
                else:
                    await message.answer("Savat bo'sh. Avval katalogdan mahsulot tanlang.", reply_markup=reply_menu(template))
                return
            await message.answer(
                "Iltimos, menyudan kerakli bo'limni tanlang. 🛍️ Katalog — mahsulotlar, 🛒 Savat — buyurtma berish.",
                reply_markup=reply_menu(template),
            )

    # ── QUIZ: test, natija ──
    elif template == "quiz":
        @dp.message(F.text == "🧠 Testni boshlash")
        async def quiz_start(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id
            states[uid] = {"step": "quiz", "q_index": 0, "score": 0}
            await ask_question(message, uid, 0)

        async def ask_question(message: types.Message, uid: int, q_index: int):
            q = QUIZ_QUESTIONS[q_index]
            kb = InlineKeyboardBuilder()
            for i, opt in enumerate(q["options"]):
                letter = chr(65 + i)  # A, B, C, D
                kb.button(text=f"{letter}) {opt}", callback_data=f"ans:{q_index}:{i}")
            kb.adjust(1)
            await message.answer(
                f"🧠 Savol {q_index + 1}/{len(QUIZ_QUESTIONS)}\n\n{q['q']}",
                reply_markup=kb.as_markup(),
            )

        @dp.callback_query(F.data.startswith("ans:"))
        async def quiz_answer(callback: types.CallbackQuery):
            uid = callback.from_user.id
            st = states.get(uid)
            if not st or st.get("step") != "quiz":
                await callback.answer("Test boshlanmagan. /start ni bosing.")
                return
            try:
                _, q_index_str, opt_str = callback.data.split(":")
                q_index = int(q_index_str)
                chosen = int(opt_str)
            except (ValueError, IndexError):
                await callback.answer("Noto'g'ri javob.")
                return
            correct = QUIZ_QUESTIONS[q_index]["correct"]
            if chosen == correct:
                st["score"] += 1
                await callback.answer("✅ To'g'ri!")
            else:
                correct_letter = chr(65 + correct)
                await callback.answer(f"❌ Noto'g'ri. To'g'ri javob: {correct_letter}")
            next_q = q_index + 1
            if next_q < len(QUIZ_QUESTIONS):
                st["q_index"] = next_q
                await callback.message.delete_reply_markup()
                await ask_question(callback.message, uid, next_q)
            else:
                score = st["score"]
                total = len(QUIZ_QUESTIONS)
                states.pop(uid, None)
                uname = ("@" + callback.from_user.username) if callback.from_user.username else callback.from_user.full_name
                await quiz_save_result(bot_id, uid, uname, score, total)
                await callback.message.delete_reply_markup()
                verdict = "🏆 Ajoyib!" if score >= 4 else ("👍 Yaxshi!" if score >= 3 else "📚 Yana urinib ko'ring!")
                await callback.message.answer(
                    f"🏁 Test tugadi!\n\n{verdict}\n📊 Natija: {score}/{total}\n\n"
                    f"Qayta urinish uchun \"🧠 Testni boshlash\" tugmasini bosing.",
                    reply_markup=reply_menu("quiz"),
                )

        @dp.message(F.text == "📊 Natijalarim")
        async def quiz_results(message: types.Message):
            if not await trial_ok(message):
                return
            uid, _ = tg_user(message)
            results = await quiz_last_results(bot_id, uid)
            if not results:
                await message.answer("📊 Siz hali test topshirmadingiz.")
                return
            lines = ["📊 Natijalaringiz:\n"]
            for r in results:
                created = r["created_at"][:16].replace("T", " ")
                lines.append(f"• {r['score']}/{r['total']} — {created}")
            await message.answer("\n".join(lines), reply_markup=reply_menu(template))

        @dp.message(F.text == "ℹ️ Qoidalar")
        async def quiz_rules(message: types.Message):
            if await trial_ok(message):
                await message.answer(
                    "📋 Qoidalar:\n• Test 5 savoldan iborat.\n• Har bir savol uchun 4 variant.\n"
                    "• To'g'ri javoblar soni ball sifatida hisoblanadi.\n• Testni qaytadan topshirish mumkin."
                )

        @dp.message()
        async def quiz_fallback(message: types.Message):
            if await trial_ok(message):
                await message.answer(
                    "Testni boshlash uchun \"🧠 Testni boshlash\" tugmasini bosing.",
                    reply_markup=reply_menu(template),
                )

    # ── SUB: obuna ──
    elif template == "sub":
        @dp.message(F.text == "📢 Obuna haqida")
        async def sub_info(message: types.Message):
            if await trial_ok(message):
                await message.answer(
                    "📢 Obuna bo'lish tartibi:\n\n"
                    "1. Quydagi kanalga obuna bo'ling (havola bot egasi tomonidan sozlanadi).\n"
                    "2. \"✅ Obunani tekshirish\" tugmasini bosing.\n"
                    "3. Bot obunangizni tasdiqlaydi.\n\n"
                    "Hozircha obuna havolasi sozlanmagan — bot egasi tez orada qo'shadi."
                )

        @dp.message(F.text == "✅ Obunani tekshirish")
        async def sub_check(message: types.Message):
            if await trial_ok(message):
                # To'liq tekshiruv kanal adminligini talab qiladi; demo rejimda tasdiqlaymiz.
                await message.answer(
                    "✅ Obunangiz qabul qilindi!\n\nEndi botning barcha imkoniyatlaridan foydalanishingiz mumkin."
                )

        @dp.message(F.text == "☎️ Admin bilan aloqa")
        async def sub_contact(message: types.Message):
            if await trial_ok(message):
                await message.answer("☎️ Admin bilan aloqa: bot egasi saytdagi ma'lumotlar orqali sozlanadi.")

        @dp.message()
        async def sub_fallback(message: types.Message):
            if await trial_ok(message):
                await message.answer(
                    "Menyudan kerakli bo'limni tanlang.",
                    reply_markup=reply_menu(template),
                )

    # ── NAVBAT: navbat (queue) boti — birinchi /start admin bo'ladi ──
    elif template == "navbat":
        async def is_admin(uid: int) -> bool:
            a = await navbat_get_admin(bot_id)
            return a is not None and a["admin_id"] == uid

        @dp.message(F.text == "⚙️ Sozlash")
        async def cfg_start(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu("navbat"))
                return
            s = await navbat_get_settings(bot_id)
            states[uid] = {"step": "cfg_business_name"}
            await message.answer(
                "⚙️ Navbat sozlamasi\n\n"
                "1️⃣ Biznes nomi (masalan: \"Sartarosh\", \"Shifokor\", \"Beauty salon\")\n"
                f"Hozirgi: {s['business_name']}\n\n"
                "Yangi nomni yozing:",
                reply_markup=admin_reply_menu(),
            )

        @dp.message(F.text == "📋 Bugungi navbat")
        async def today_queue(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu("navbat"))
                return
            rows, settings = await navbat_list_today(bot_id)
            if not rows:
                await message.answer("📋 Bugungi navbatlar yo'q.", reply_markup=admin_reply_menu())
                return
            kb = InlineKeyboardBuilder()
            lines = ["📋 Bugungi navbatlar:\n"]
            for r in rows:
                t = (r["slot_time"] or "")[11:16]
                mark = "✅" if r["status"] == "done" else ("⏳" if r["status"] == "waiting" else "❌")
                lines.append(f"{mark} {t} — {r['customer_name']} · {r['customer_phone']}")
                if r["status"] == "waiting":
                    kb.button(text=f"✅ {t} {r['customer_name']}", callback_data=f"qdone:{r['id']}")
            kb.adjust(1)
            await message.answer("\n".join(lines), reply_markup=kb.as_markup())

        @dp.callback_query(F.data.startswith("qdone:"))
        async def mark_done(callback: types.CallbackQuery):
            uid = callback.from_user.id
            if not await is_admin(uid):
                await callback.answer("Faqat admin uchun.")
                return
            try:
                entry_id = int(callback.data.split(":", 1)[1])
            except (ValueError, IndexError):
                await callback.answer("Noto'g'ri.")
                return
            async with db_lock:
                db.execute("UPDATE queue_entries SET status='done' WHERE id=? AND bot_id=?", (entry_id, bot_id))
                db.commit()
            await callback.answer("✅ Bajarildi deb belgilandi.")
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

        @dp.message(F.text == "📊 Statistika")
        async def stats_view(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu("navbat"))
                return
            s = await navbat_stats(bot_id)
            settings = await navbat_get_settings(bot_id)
            await message.answer(
                "📊 Statistika\n\n"
                f"📋 Biznes: {settings['business_name']}\n"
                f"🕐 Ish vaqti: {settings['work_start']} - {settings['work_end']}\n"
                f"⏱ Har mijozga: {settings['minutes_per_client']} daqiqa\n\n"
                f"📅 Bugun:\n"
                f"  • Jami navbatlar: {s['total_today']}\n"
                f"  • Kutilmoqda: {s['waiting']}\n"
                f"  • Bajarilgan: {s['done']}\n\n"
                f"📈 Jami (barcha vaqt): {s['total_all']}",
                reply_markup=admin_reply_menu(),
            )

        @dp.message(F.text == "🎫 Navbat olish")
        async def get_queue(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if await is_admin(uid):
                await message.answer("Admin sifatida navbat ololmaysiz.", reply_markup=admin_reply_menu())
                return
            slots, settings = await navbat_available_slots(bot_id, count=8)
            if not slots:
                await message.answer(
                    "⛔ Hozir bo'sh vaqt yo'q. Iltimos, keyinroq urinib ko'ring.",
                    reply_markup=reply_menu("navbat"),
                )
                return
            states[uid] = {"step": "picking_slot", "slots": [s.isoformat() for s in slots]}
            kb = InlineKeyboardBuilder()
            for i, s in enumerate(slots):
                kb.button(text=f"📅 {fmt_slot(s)}", callback_data=f"slot:{i}")
            kb.adjust(2)
            await message.answer(
                "🎫 Bo'sh vaqtlar — tanlang:\n\n"
                f"(Har mijozga {settings['minutes_per_client']} daqiqa ajratiladi)",
                reply_markup=kb.as_markup(),
            )

        @dp.callback_query(F.data.startswith("slot:"))
        async def pick_slot(callback: types.CallbackQuery):
            uid = callback.from_user.id
            st = states.get(uid)
            if not st or st.get("step") != "picking_slot":
                await callback.answer("Iltimos, \"🎫 Navbat olish\" tugmasini bosing.")
                return
            try:
                idx = int(callback.data.split(":", 1)[1])
                chosen = st["slots"][idx]
            except (ValueError, IndexError):
                await callback.answer("Noto'g'ri tanlov.")
                return
            st["step"] = "awaiting_name"
            st["chosen_slot"] = chosen
            await callback.answer()
            await callback.message.edit_text(
                f"✅ Tanlangan vaqt: {fmt_slot_iso(chosen)}\n\n📝 Iltimos, ismingizni yozing:"
            )

        @dp.message(F.text == "📅 Mening vaqtim")
        async def my_queue(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if await is_admin(uid):
                await message.answer("Admin uchun mijoz navbati yo'q.", reply_markup=admin_reply_menu())
                return
            rows = await navbat_my_slot(bot_id, uid)
            if not rows:
                await message.answer(
                    "📅 Sizda hozircha navbat yo'q. \"🎫 Navbat olish\" tugmasini bosing.",
                    reply_markup=reply_menu("navbat"),
                )
                return
            lines = ["📅 Sizning navbatlaringiz:\n"]
            for r in rows:
                mark = "✅" if r["status"] == "done" else ("⏳" if r["status"] == "waiting" else "❌")
                lines.append(f"{mark} {fmt_slot_iso(r['slot_time'])}")
            await message.answer("\n".join(lines), reply_markup=reply_menu("navbat"))

        @dp.message(F.text == "ℹ️ Ma'lumot")
        async def navbat_info(message: types.Message):
            if not await trial_ok(message):
                return
            settings = await navbat_get_settings(bot_id)
            uid = message.from_user.id if message.from_user else 0
            menu = admin_reply_menu() if await is_admin(uid) else reply_menu("navbat")
            await message.answer(
                f"ℹ️ {settings['business_name']}\n\n"
                f"🕐 Ish vaqti: {settings['work_start']} - {settings['work_end']}\n"
                f"⏱ Har mijozga: {settings['minutes_per_client']} daqiqa\n\n"
                "Navbat olish uchun \"🎫 Navbat olish\" tugmasini bosing va bo'sh vaqtlardan birini tanlang.",
                reply_markup=menu,
            )

        @dp.message()
        async def navbat_fallback(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            uname = (
                ("@" + message.from_user.username) if (message.from_user and message.from_user.username)
                else (message.from_user.full_name if message.from_user else "Foydalanuvchi")
            )
            st = states.get(uid)

            # Admin: sozlama FSM (biznes nomi → boshlanish → tugash → daqiqa)
            if st and st.get("step") == "cfg_business_name":
                s = await navbat_get_settings(bot_id)
                name = (message.text or "").strip() or s["business_name"]
                st["cfg"] = {"business_name": name}
                st["step"] = "cfg_work_start"
                await message.answer(
                    f"✅ Biznes nomi: {name}\n\n"
                    f"2️⃣ Ish boshlanish vaqti (HH:MM, masalan 09:00)\nHozirgi: {s['work_start']}"
                )
                return
            if st and st.get("step") == "cfg_work_start":
                ws = (message.text or "").strip()
                if not valid_hhmm(ws):
                    await message.answer("⛔ Noto'g'ri format. HH:MM (masalan 09:00) kiriting:")
                    return
                st["cfg"]["work_start"] = ws
                st["step"] = "cfg_work_end"
                await message.answer(f"✅ Boshlanish: {ws}\n\n3️⃣ Ish tugash vaqti (HH:MM, masalan 18:00):")
                return
            if st and st.get("step") == "cfg_work_end":
                we = (message.text or "").strip()
                if not valid_hhmm(we):
                    await message.answer("⛔ Noto'g'ri format. HH:MM (masalan 18:00) kiriting:")
                    return
                cfg = st["cfg"]
                if parse_hhmm(we) <= parse_hhmm(cfg["work_start"]):
                    await message.answer("⛔ Tugash vaqti boshlanishdan keyin bo'lishi kerak. Qaytadan kiriting (HH:MM):")
                    return
                cfg["work_end"] = we
                st["step"] = "cfg_minutes"
                await message.answer(f"✅ Tugash: {we}\n\n4️⃣ Har mijozga necha daqiqa ajratiladi? (masalan: 30 yoki 60)")
                return
            if st and st.get("step") == "cfg_minutes":
                raw = (message.text or "").strip()
                try:
                    m = int(raw)
                    if not 5 <= m <= 480:
                        raise ValueError()
                except ValueError:
                    await message.answer("⛔ Iltimos, 5 dan 480 gacha son kiriting (daqiqa):")
                    return
                cfg = st["cfg"]
                await navbat_update_settings(bot_id, cfg["business_name"], cfg["work_start"], cfg["work_end"], m)
                states.pop(uid, None)
                await message.answer(
                    "✅ Sozlamalar saqlandi!\n\n"
                    f"📋 Biznes: {cfg['business_name']}\n"
                    f"🕐 Ish vaqti: {cfg['work_start']} - {cfg['work_end']}\n"
                    f"⏱ Har mijozga: {m} daqiqa",
                    reply_markup=admin_reply_menu(),
                )
                return

            # Mijoz: navbat olish FSM (ism → telefon)
            if st and st.get("step") == "awaiting_name":
                name = (message.text or "").strip()
                if len(name) < 2:
                    await message.answer("⛔ Iltimos, to'g'ri ism kiriting:")
                    return
                st["step"] = "awaiting_phone"
                st["name"] = name
                await message.answer("📞 Endi telefon raqamingizni yuboring (masalan: +998901234567):")
                return
            if st and st.get("step") == "awaiting_phone":
                phone = (message.text or "").strip()
                if len(phone) < 5:
                    await message.answer("⛔ Iltimos, to'g'ri telefon raqam kiriting:")
                    return
                name = st.get("name", "")
                slot = st.get("chosen_slot")
                states.pop(uid, None)
                booking_id = await navbat_create_booking(bot_id, uid, uname, name, phone, slot)
                if booking_id is None:
                    await message.answer(
                        "⛔ Uzr, bu vaqt band qilingan. Iltimos, \"🎫 Navbat olish\" tugmasidan boshqa vaqt tanlang.",
                        reply_markup=reply_menu("navbat"),
                    )
                    return
                await message.answer(
                    "🎉 Tabriklaymiz! Navbatingiz qabul qilindi.\n\n"
                    f"🎫 Raqam: #{booking_id}\n"
                    f"👤 Ism: {name}\n"
                    f"📞 Tel: {phone}\n"
                    f"📅 Vaqt: {fmt_slot_iso(slot)}\n\n"
                    "Iltimos, belgilangan vaqtda keling. ✅",
                    reply_markup=reply_menu("navbat"),
                )
                admin = await navbat_get_admin(bot_id)
                if admin:
                    try:
                        await message.bot.send_message(
                            admin["admin_id"],
                            f"🔔 Yangi navbat!\n\n#{booking_id}\n👤 {name}\n📞 {phone}\n📅 {fmt_slot_iso(slot)}",
                        )
                    except Exception:
                        pass
                return

            # Hech qanday FSM yo'q — menyuga yo'naltirish
            if await is_admin(uid):
                await message.answer(
                    "Menyudan bo'lim tanlang: ⚙️ Sozlash, 📋 Bugungi navbat, 📊 Statistika.",
                    reply_markup=admin_reply_menu(),
                )
            else:
                await message.answer(
                    "Menyudan bo'lim tanlang: 🎫 Navbat olish, 📅 Mening vaqtim, ℹ️ Ma'lumot.",
                    reply_markup=reply_menu("navbat"),
                )

    # ── FEEDBACK: fikr-mulohaza (birinchi /start = admin) ──
    elif template == "feedback":
        async def is_admin(uid: int) -> bool:
            a = await get_bot_admin(bot_id)
            return a is not None and a["admin_id"] == uid

        @dp.message(F.text == "⭐ Baholash")
        async def fb_rate(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if await is_admin(uid):
                await message.answer("Admin mijoz sifatida baholay olmaydi.", reply_markup=admin_menu(template))
                return
            kb = InlineKeyboardBuilder()
            for i in range(1, 6):
                kb.button(text="⭐" * i, callback_data=f"fbr:{i}")
            kb.adjust(5)
            states[uid] = {"step": "fb_rating"}
            await message.answer("⭐ Bahoni tanlang (1-5 yulduz):", reply_markup=kb.as_markup())

        @dp.callback_query(F.data.startswith("fbr:"))
        async def fb_rating_cb(callback: types.CallbackQuery):
            uid = callback.from_user.id
            st = states.get(uid)
            if not st or st.get("step") != "fb_rating":
                await callback.answer("Avval \"⭐ Baholash\" tugmasini bosing.")
                return
            try:
                rating = int(callback.data.split(":", 1)[1])
                if not 1 <= rating <= 5:
                    raise ValueError()
            except ValueError:
                await callback.answer("Noto'g'ri.")
                return
            st["rating"] = rating
            st["step"] = "fb_comment"
            await callback.answer()
            await callback.message.edit_text(f"✅ Baho: {'⭐' * rating}\n\n✍️ Endi izohingizni yozing (yoki \"-\" yuboring):")

        @dp.message(F.text == "📋 Mening baholarim")
        async def fb_my(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            rows = await feedback_list_user(bot_id, uid)
            if not rows:
                await message.answer("📋 Sizda hozircha baholar yo'q.", reply_markup=reply_menu(template))
                return
            lines = ["📋 Sizning baholaringiz:\n"]
            for r in rows:
                stars = "⭐" * r["rating"]
                cmt = r["comment"] or "—"
                lines.append(f"{stars} — {cmt}")
            await message.answer("\n".join(lines), reply_markup=reply_menu(template))

        @dp.message(F.text == "⭐ Barcha fikrlar")
        async def fb_all(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu(template))
                return
            rows = await feedback_list_all(bot_id)
            if not rows:
                await message.answer("⭐ Hozircha fikrlar yo'q.", reply_markup=admin_menu(template))
                return
            lines = ["⭐ Barcha fikrlar:\n"]
            for r in rows:
                stars = "⭐" * r["rating"]
                who = r["tg_username"] or "mijoz"
                cmt = r["comment"] or "—"
                lines.append(f"{stars} [{who}] {cmt}")
            await message.answer("\n".join(lines), reply_markup=admin_menu(template))

        @dp.message(F.text == "📊 O'rtacha ball")
        async def fb_avg(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu(template))
                return
            s = await feedback_average(bot_id)
            avg_stars = "⭐" * round(s["avg"]) if s["avg"] else "—"
            await message.answer(
                f"📊 Statistika\n\n📊 O'rtacha ball: {s['avg']} / 5\n{avg_stars}\n📝 Jami fikrlar: {s['count']}",
                reply_markup=admin_menu(template),
            )

        @dp.message(F.text == "ℹ️ Ma'lumot")
        async def fb_info(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            menu = admin_menu(template) if await is_admin(uid) else reply_menu(template)
            await message.answer(
                "ℹ️ Fikr-mulohaza boti\n\n"
                "Mijozlar biznesni 1-5 yulduz baholaydi va izoh qoldiradi. "
                "Admin barcha fikrlarni va o'rtacha ballni ko'radi.",
                reply_markup=menu,
            )

        @dp.message()
        async def fb_fallback(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            uname = (
                ("@" + message.from_user.username) if (message.from_user and message.from_user.username)
                else (message.from_user.full_name if message.from_user else "Foydalanuvchi")
            )
            st = states.get(uid)
            if st and st.get("step") == "fb_comment":
                rating = st.get("rating", 5)
                comment = (message.text or "").strip()
                if comment == "-":
                    comment = ""
                states.pop(uid, None)
                await feedback_save(bot_id, uid, uname, rating, comment)
                await message.answer(
                    f"✅ Rahmat! Fikringiz qabul qilindi.\n\nBaho: {'⭐' * rating}",
                    reply_markup=reply_menu(template),
                )
                return
            await message.answer("Menyudan bo'lim tanlang.", reply_markup=reply_menu(template))

    # ── NEWS: yangiliklar (birinchi /start = admin) ──
    elif template == "news":
        async def is_admin(uid: int) -> bool:
            a = await get_bot_admin(bot_id)
            return a is not None and a["admin_id"] == uid

        @dp.message(F.text == "📢 Obuna bo'lish")
        async def news_sub(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if await is_admin(uid):
                await message.answer("Admin obuna bo'lolmaydi.", reply_markup=admin_menu(template))
                return
            uname = ("@" + message.from_user.username) if (message.from_user and message.from_user.username) else (message.from_user.full_name if message.from_user else "mijoz")
            ok = await news_subscribe(bot_id, uid, uname)
            if ok:
                await message.answer("✅ Siz yangiliklarga obuna bo'ldingiz! Endi barcha yangiliklarni shu yerdan olasiz.", reply_markup=reply_menu(template))
            else:
                await message.answer("Siz allaqachon obuna bo'lgansiz.", reply_markup=reply_menu(template))

        @dp.message(F.text == "🔕 Obunani bekor qilish")
        async def news_unsub_msg(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            ok = await news_unsubscribe(bot_id, uid)
            if ok:
                await message.answer("🔕 Obunangiz bekor qilindi.", reply_markup=reply_menu(template))
            else:
                await message.answer("Siz obuna emassiz.", reply_markup=reply_menu(template))

        @dp.message(F.text == "📨 Yangilik yuborish")
        async def news_send(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu(template))
                return
            states[uid] = {"step": "news_body"}
            await message.answer("📨 Yangilik matnini yozing — u barcha obunachilarga tarqatiladi:", reply_markup=admin_menu(template))

        @dp.message(F.text == "👥 Obunachilar")
        async def news_count(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu(template))
                return
            c = await news_subscriber_count(bot_id)
            await message.answer(f"👥 Obunachilar soni: {c}", reply_markup=admin_menu(template))

        @dp.message(F.text == "ℹ️ Ma'lumot")
        async def news_info(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            menu = admin_menu(template) if await is_admin(uid) else reply_menu(template)
            await message.answer(
                "ℹ️ Yangiliklar boti\n\n"
                "Mijozlar \"📢 Obuna bo'lish\" tugmasi orqali obunachi bo'ladi. "
                "Admin yangilik matnini yuboradi — u barcha obunachilarga yetkaziladi.",
                reply_markup=menu,
            )

        @dp.message()
        async def news_fallback(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            st = states.get(uid)
            if st and st.get("step") == "news_body":
                if not await is_admin(uid):
                    states.pop(uid, None)
                    await message.answer("⛔ Faqat admin yangilik yubora oladi.", reply_markup=reply_menu(template))
                    return
                body = (message.text or "").strip()
                if len(body) < 3:
                    await message.answer("⛔ Matn juda qisqa. Qaytadan yozing:")
                    return
                states.pop(uid, None)
                post_id = await news_create_post(bot_id, body)
                subs = await news_subscriber_ids(bot_id)
                sent = 0
                for sid in subs:
                    try:
                        await message.bot.send_message(sid, f"📨 Yangilik:\n\n{body}")
                        sent += 1
                    except Exception:
                        pass
                await news_mark_sent(post_id, sent)
                await message.answer(
                    f"✅ Yangilik tarqatildi!\n\n👥 Obunachilar: {len(subs)}\n📬 Yuborildi: {sent}",
                    reply_markup=admin_menu(template),
                )
                return
            await message.answer("Menyudan bo'lim tanlang.", reply_markup=reply_menu(template))

    # ── POLL: so'rovnomalar (birinchi /start = admin) ──
    elif template == "poll":
        async def is_admin(uid: int) -> bool:
            a = await get_bot_admin(bot_id)
            return a is not None and a["admin_id"] == uid

        async def show_active_poll(message: types.Message, uid: int):
            poll = await poll_get_active(bot_id)
            if not poll:
                if await is_admin(uid):
                    await message.answer("📭 Hozir faol so'rov yo'q. \"📝 So'rov yaratish\" tugmasini bosing.", reply_markup=admin_menu(template))
                else:
                    await message.answer("📭 Hozir faol so'rov yo'q.", reply_markup=reply_menu(template))
                return
            options = json.loads(poll["options_json"])
            kb = InlineKeyboardBuilder()
            for i, opt in enumerate(options):
                kb.button(text=opt, callback_data=f"pv:{poll['id']}:{i}")
            kb.adjust(1)
            await message.answer(f"📊 {poll['question']}", reply_markup=kb.as_markup())

        @dp.message(F.text == "📊 Ovoz berish")
        async def poll_vote_start(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if await is_admin(uid):
                await message.answer("Admin ovoz berolmaydi.", reply_markup=admin_menu(template))
                return
            await show_active_poll(message, uid)

        @dp.callback_query(F.data.startswith("pv:"))
        async def poll_vote_cb(callback: types.CallbackQuery):
            uid = callback.from_user.id
            try:
                _, poll_id_str, choice_str = callback.data.split(":")
                poll_id = int(poll_id_str)
                choice = int(choice_str)
            except (ValueError, IndexError):
                await callback.answer("Noto'g'ri.")
                return
            ok = await poll_vote(poll_id, uid, choice)
            if not ok:
                await callback.answer("Siz allaqachon ovoz bergansiz.")
                return
            await callback.answer("✅ Ovoz qabul qilindi!")
            await callback.message.edit_text("✅ Ovozingiz qabul qilindi! \"📈 Natijalar\" tugmasidan ko'ring.")

        @dp.message(F.text == "📈 Natijalar")
        async def poll_results_msg(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            poll = await poll_get_active(bot_id)
            menu = admin_menu(template) if await is_admin(uid) else reply_menu(template)
            if not poll:
                await message.answer("📭 Hozir faol so'rov yo'q.", reply_markup=menu)
                return
            options = json.loads(poll["options_json"])
            res = await poll_results(poll["id"])
            counts = {r["choice"]: r["c"] for r in res}
            total = sum(counts.values())
            lines = [f"📊 {poll['question']}\n", f"Jami ovozlar: {total}\n"]
            for i, opt in enumerate(options):
                c = counts.get(i, 0)
                pct = round(100 * c / total) if total else 0
                bar = "█" * (pct // 10)
                lines.append(f"{opt}: {c} ({pct}%) {bar}")
            await message.answer("\n".join(lines), reply_markup=menu)

        @dp.message(F.text == "📝 So'rov yaratish")
        async def poll_create_start(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu(template))
                return
            states[uid] = {"step": "poll_question"}
            await message.answer("📝 Yangi so'rov.\n\nSavolni yozing:", reply_markup=admin_menu(template))

        @dp.message(F.text == "📊 Faol so'rov")
        async def poll_active(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu(template))
                return
            await show_active_poll(message, uid)

        @dp.message(F.text == "ℹ️ Ma'lumot")
        async def poll_info(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            menu = admin_menu(template) if await is_admin(uid) else reply_menu(template)
            await message.answer(
                "ℹ️ So'rovnomalar boti\n\n"
                "Admin savol va variantlarni kiritadi. Mijozlar ovoz beradi va natijani ko'radi.",
                reply_markup=menu,
            )

        @dp.message()
        async def poll_fallback(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            st = states.get(uid)
            if st and st.get("step") == "poll_question":
                q = (message.text or "").strip()
                if len(q) < 3:
                    await message.answer("⛔ Savol juda qisqa. Qaytadan yozing:")
                    return
                st["question"] = q
                st["step"] = "poll_options"
                await message.answer(
                    "✅ Savol qabul qilindi.\n\nEndi variantlarni yozing — har bir variant yangi qatorda (2-6 ta):\n\n"
                    "Masalan:\nHa\nYo'q\nBilmayman"
                )
                return
            if st and st.get("step") == "poll_options":
                if not await is_admin(uid):
                    states.pop(uid, None)
                    await message.answer("⛔ Faqat admin so'rov yaratadi.", reply_markup=reply_menu(template))
                    return
                raw = (message.text or "").strip()
                opts = [o.strip() for o in raw.split("\n") if o.strip()]
                if not 2 <= len(opts) <= 6:
                    await message.answer("⛔ 2 dan 6 gacha variant kerak (har biri yangi qator). Qaytadan yozing:")
                    return
                q = st.get("question", "")
                states.pop(uid, None)
                await poll_create(bot_id, q, opts)
                await message.answer(
                    f"✅ So'rov yaratildi!\n\n📊 {q}\n" + "\n".join(f"{i+1}. {o}" for i, o in enumerate(opts)),
                    reply_markup=admin_menu(template),
                )
                return
            await message.answer("Menyudan bo'lim tanlang.", reply_markup=reply_menu(template))

    # ── INFO: biznes ma'lumoti (birinchi /start = admin) ──
    elif template == "info":
        async def is_admin(uid: int) -> bool:
            a = await get_bot_admin(bot_id)
            return a is not None and a["admin_id"] == uid

        async def render_info(message: types.Message, uid: int):
            s = await info_get_settings(bot_id)
            lines = [f"ℹ️ {s['business_name']}\n"]
            if s["about"]:
                lines.append(f"📝 Haqida:\n{s['about']}\n")
            if s["services"]:
                lines.append(f"🛠 Xizmatlar:\n{s['services']}\n")
            if s["phone"]:
                lines.append(f"📞 Telefon: {s['phone']}")
            if s["address"]:
                lines.append(f"📍 Manzil: {s['address']}")
            if s["work_hours"]:
                lines.append(f"🕐 Ish vaqti: {s['work_hours']}")
            await message.answer("\n".join(lines), reply_markup=reply_menu(template))

        @dp.message(F.text == "ℹ️ Biznes haqida")
        async def info_about(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if await is_admin(uid):
                await message.answer("Admin mijoz ko'rinishini ko'radi 👇", reply_markup=admin_menu(template))
            await render_info(message, uid)

        @dp.message(F.text == "📞 Aloqa")
        async def info_phone(message: types.Message):
            if not await trial_ok(message):
                return
            s = await info_get_settings(bot_id)
            uid = message.from_user.id if message.from_user else 0
            menu = admin_menu(template) if await is_admin(uid) else reply_menu(template)
            await message.answer(f"📞 Aloqa: {s['phone'] or 'sozlanmagan'}", reply_markup=menu)

        @dp.message(F.text == "📍 Manzil")
        async def info_addr(message: types.Message):
            if not await trial_ok(message):
                return
            s = await info_get_settings(bot_id)
            uid = message.from_user.id if message.from_user else 0
            menu = admin_menu(template) if await is_admin(uid) else reply_menu(template)
            await message.answer(f"📍 Manzil: {s['address'] or 'sozlanmagan'}\n🕐 {s['work_hours'] or '—'}", reply_markup=menu)

        @dp.message(F.text == "⚙️ Ma'lumot sozlash")
        async def info_cfg(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu(template))
                return
            states[uid] = {"step": "info_name", "cfg": {}}
            s = await info_get_settings(bot_id)
            extra = f"\n(hozirgi: {s['business_name']})" if s["business_name"] != "Biznes" else ""
            await message.answer(f"⚙️ Ma'lumot sozlash\n\n1️⃣ Biznes nomi:{extra}", reply_markup=admin_menu(template))

        @dp.message(F.text == "👁 Ko'rish")
        async def info_view(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu(template))
                return
            s = await info_get_settings(bot_id)
            await message.answer(
                "👁 Joriy ma'lumot:\n\n"
                f"📋 {s['business_name']}\n📝 {s['about'] or '—'}\n🛠 {s['services'] or '—'}\n"
                f"📞 {s['phone'] or '—'}\n📍 {s['address'] or '—'}\n🕐 {s['work_hours'] or '—'}",
                reply_markup=admin_menu(template),
            )

        @dp.message(F.text == "ℹ️ Ma'lumot")
        async def info_info(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            menu = admin_menu(template) if await is_admin(uid) else reply_menu(template)
            await message.answer(
                "ℹ️ Biznes ma'lumot boti\n\n"
                "Admin biznes nomi, haqida, xizmatlar, telefon, manzil va ish vaqtini sozlaydi. "
                "Mijozlar buni ko'radi.",
                reply_markup=menu,
            )

        @dp.message()
        async def info_fallback(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            st = states.get(uid)
            cfg = st.get("cfg", {}) if st else {}
            step = st.get("step") if st else None
            if step == "info_name":
                cfg["business_name"] = (message.text or "").strip() or "Biznes"
                st["step"] = "info_about"
                await message.answer(f"✅ Nomi: {cfg['business_name']}\n\n2️⃣ Biznes haqida (matn yoki \"-\"):")
                return
            if step == "info_about":
                raw = (message.text or "").strip()
                cfg["about"] = "" if raw == "-" else raw
                st["step"] = "info_services"
                await message.answer("✅ Saqlandi.\n\n3️⃣ Xizmatlar (har biri yangi qator yoki \"-\"):")
                return
            if step == "info_services":
                raw = (message.text or "").strip()
                cfg["services"] = "" if raw == "-" else raw
                st["step"] = "info_phone"
                await message.answer("✅ Saqlandi.\n\n4️⃣ Telefon raqami (yoki \"-\"):")
                return
            if step == "info_phone":
                raw = (message.text or "").strip()
                cfg["phone"] = "" if raw == "-" else raw
                st["step"] = "info_address"
                await message.answer("✅ Saqlandi.\n\n5️⃣ Manzil (yoki \"-\"):")
                return
            if step == "info_address":
                raw = (message.text or "").strip()
                cfg["address"] = "" if raw == "-" else raw
                st["step"] = "info_hours"
                await message.answer("✅ Saqlandi.\n\n6️⃣ Ish vaqti (masalan: Du-Sha 9:00-18:00 yoki \"-\"):")
                return
            if step == "info_hours":
                raw = (message.text or "").strip()
                cfg["work_hours"] = "" if raw == "-" else raw
                states.pop(uid, None)
                await info_update_settings(bot_id, cfg)
                await message.answer(
                    "✅ Ma'lumot saqlandi!\n\n"
                    f"📋 {cfg['business_name']}\n📝 {cfg['about'] or '—'}\n🛠 {cfg['services'] or '—'}\n"
                    f"📞 {cfg['phone'] or '—'}\n📍 {cfg['address'] or '—'}\n🕐 {cfg['work_hours'] or '—'}",
                    reply_markup=admin_menu(template),
                )
                return
            await message.answer("Menyudan bo'lim tanlang.", reply_markup=reply_menu(template))

    # ── REQUEST ENGINE: 15 biznes shabloni (callback, consultation, complaint, ...) ──
    elif template in REQUEST_TEMPLATES:
        cfg = REQUEST_TEMPLATES[template]
        fields = cfg["fields"]

        async def is_admin(uid: int) -> bool:
            a = await get_bot_admin(bot_id)
            return a is not None and a["admin_id"] == uid

        @dp.message(F.text == "📝 So'rov yuborish")
        async def req_new(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if await is_admin(uid):
                await message.answer("Admin so'rov yuborolmaydi.", reply_markup=admin_menu(template))
                return
            states[uid] = {"step": "req_field", "idx": 0, "data": {}}
            await message.answer(fields[0][1], reply_markup=reply_menu(template))

        @dp.message(F.text == "📋 Mening so'rovlarim")
        async def req_my(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            rows = await request_list_user(bot_id, uid)
            if not rows:
                await message.answer("📋 Sizda hozircha so'rovlar yo'q.", reply_markup=reply_menu(template))
                return
            lines = ["📋 Sizning so'rovlaringiz:\n"]
            for r in rows:
                status = "✅" if r["status"] == "done" else "⏳"
                data = json.loads(r["data_json"])
                summary = " · ".join(f"{k}: {v}" for k, v in data.items() if v)
                lines.append(f"{status} #{r['id']} — {summary}")
            await message.answer("\n".join(lines), reply_markup=reply_menu(template))

        @dp.message(F.text == "📋 So'rovlar")
        async def req_all(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            if not await is_admin(uid):
                await message.answer("⛔ Bu bo'lim faqat admin uchun.", reply_markup=reply_menu(template))
                return
            rows = await request_list_all(bot_id)
            if not rows:
                await message.answer("📋 Hozircha so'rovlar yo'q.", reply_markup=admin_menu(template))
                return
            for r in rows:
                data = json.loads(r["data_json"])
                status = "✅ Bajarildi" if r["status"] == "done" else "🆕 Yangi"
                body = "\n".join(f"  {k}: {v}" for k, v in data.items() if v)
                kb = InlineKeyboardBuilder()
                if r["status"] == "new":
                    kb.button(text="✅ Bajarildi", callback_data=f"rqd:{r['id']}")
                await message.answer(
                    f"📋 #{r['id']} [{status}]\n👤 {r['tg_username'] or 'mijoz'}\n{body}",
                    reply_markup=kb.as_markup() if r["status"] == "new" else None,
                )
            await message.answer(f"Jami: {len(rows)} so'rov", reply_markup=admin_menu(template))

        @dp.callback_query(F.data.startswith("rqd:"))
        async def req_done_cb(callback: types.CallbackQuery):
            uid = callback.from_user.id
            if not await is_admin(uid):
                await callback.answer("⛔ Faqat admin.")
                return
            try:
                req_id = int(callback.data.split(":", 1)[1])
            except ValueError:
                await callback.answer("Noto'g'ri.")
                return
            ok = await request_mark_done(req_id)
            if ok:
                await callback.answer("✅ Bajarildi deb belgilandi!")
                await callback.message.edit_text((callback.message.text or "") + "\n\n✅ Bajarildi")
            else:
                await callback.answer("So'rov allaqachon bajarilgan yoki topilmadi.")

        @dp.message(F.text == "ℹ️ Ma'lumot")
        async def req_info(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            menu = admin_menu(template) if await is_admin(uid) else reply_menu(template)
            await message.answer(
                f"ℹ️ {cfg['label']} boti\n\n"
                "Mijoz \"📝 So'rov yuborish\" orqali forma to'ldiradi, admin esa \"📋 So'rovlar\" "
                "orqali barchasini ko'radi va bajarilganlarni ✅ belgilaydi.",
                reply_markup=menu,
            )

        @dp.message()
        async def req_fallback(message: types.Message):
            if not await trial_ok(message):
                return
            uid = message.from_user.id if message.from_user else 0
            uname = (
                ("@" + message.from_user.username) if (message.from_user and message.from_user.username)
                else (message.from_user.full_name if message.from_user else "Foydalanuvchi")
            )
            st = states.get(uid)
            if st and st.get("step") == "req_field":
                idx = st.get("idx", 0)
                key, _prompt = fields[idx]
                val = (message.text or "").strip()
                if val == "-":
                    val = ""
                st["data"][key] = val
                idx += 1
                if idx >= len(fields):
                    data = st["data"]
                    states.pop(uid, None)
                    req_id = await request_save(bot_id, template, uid, uname, data)
                    admin = await get_bot_admin(bot_id)
                    if admin and admin["admin_id"] != uid:
                        body = "\n".join(f"  {k}: {v}" for k, v in data.items() if v)
                        try:
                            await message.bot.send_message(
                                admin["admin_id"],
                                f"🔔 Yangi so'rov! #{req_id}\n📋 {cfg['label']}\n👤 {uname}\n{body}",
                            )
                        except Exception:
                            pass
                    summary = " · ".join(f"{k}: {v}" for k, v in data.items() if v)
                    await message.answer(
                        f"✅ So'rovingiz qabul qilindi! #{req_id}\n\n{summary}\n\nTez orada siz bilan bog'lanamiz.",
                        reply_markup=reply_menu(template),
                    )
                    return
                st["idx"] = idx
                await message.answer(fields[idx][1], reply_markup=reply_menu(template))
                return
            await message.answer("Menyudan bo'lim tanlang.", reply_markup=reply_menu(template))

    return dp
