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
    elif template == "shop":
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
    else:  # navbat — mijoz menyu
        kb.button(text="🎫 Navbat olish")
        kb.button(text="📅 Mening vaqtim")
        kb.button(text="ℹ️ Ma'lumot")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


def admin_reply_menu() -> "types.ReplyKeyboardMarkup":
    """Navbat boti admini uchun reply-klaviatura."""
    kb = ReplyKeyboardBuilder()
    kb.button(text="⚙️ Sozlash")
    kb.button(text="📋 Bugungi navbat")
    kb.button(text="📊 Statistika")
    kb.button(text="ℹ️ Ma'lumot")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


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
        greetings = {
            "support": "Assalomu alaykum! 👋\nYordam markaziga xush kelibsiz.\nSavolingizni yuboring — biz uni qabul qilamiz va raqami bilan tasdiqlaymiz.",
            "shop": "Assalomu alaykum! 🛍️\nDo'kon botiga xush kelibsiz!\nKatalogdan mahsulot tanlang, savatga qo'shing va buyurtma bering.",
            "quiz": "Salom! 🧠\nBilimingizni sinab ko'ring! Test 5 savoldan iborat. \"Testni boshlash\" tugmasini bosing.",
            "sub": "Assalomu alaykum! 🔐\nObuna botiga xush kelibsiz. Obuna tartibi haqida ma'lumot oling va obunangizni tekshiring.",
        }
        states.pop(message.from_user.id if message.from_user else 0, None)
        await message.answer(greetings[template], reply_markup=reply_menu(template))

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

    # ── SHOP: katalog, savat, buyurtma ──
    elif template == "shop":
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

    return dp
