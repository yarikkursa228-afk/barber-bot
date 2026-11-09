import os, json, hmac, hashlib, sqlite3, shutil, uuid, asyncio, datetime
from urllib.parse import parse_qsl
from dotenv import load_dotenv
load_dotenv()

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp
from fastapi import FastAPI, Depends, HTTPException, Header, File, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel 
from upstash_blob import Bucket

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "barber.db")
DEV_MODE = os.environ.get("DEV_MODE", "0") == "1"
WEBAPP_URL = os.environ["WEBAPP_URL"]
if not WEBAPP_URL.endswith('/'): WEBAPP_URL += '/'

# --- БОТ ---
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start(m: Message):
    user_id = m.from_user.id
    if user_id in ADMIN_IDS:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📅 Открыть админку", web_app=WebAppInfo(url=WEBAPP_URL))]])
        await m.answer("Привет, Барбер!", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✂️ Записаться", web_app=WebAppInfo(url=WEBAPP_URL + "client"))],
            [InlineKeyboardButton(text="📋 Мои записи", web_app=WebAppInfo(url=WEBAPP_URL + "client?view=my"))],
            [InlineKeyboardButton(text="⭐ Оставить отзыв", web_app=WebAppInfo(url=WEBAPP_URL + "client?view=reviews"))]
        ])
        await m.answer("Привет! Выбери действие:", reply_markup=kb)

async def start_bot():
    import traceback
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL)))
        print("Бот успешно запущен и начал слушать сообщения!")
        await dp.start_polling(bot)
    except Exception as e:
        print(f"КРИТИЧЕСКАЯ ОШИБКА БОТА: {e}")
        traceback.print_exc()

# --- БАЗА ДАННЫХ ---
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, time TEXT NOT NULL,
                client_name TEXT NOT NULL, client_phone TEXT, service TEXT, price INTEGER,
                status TEXT NOT NULL DEFAULT 'confirmed', client_tg_id INTEGER, admin_note TEXT,
                promo_code TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        for col in ['client_tg_id INTEGER', 'admin_note TEXT', 'promo_code TEXT']:
            try: c.execute(f"ALTER TABLE appointments ADD COLUMN {col}")
            except sqlite3.OperationalError: pass

        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('name', 'MERGE HAIR')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('address', 'Пионерская улица, 54')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('avatar', 'https://i.pinimg.com/736x/8f/c2/73/8fc2735c66b46b1a88c74e36e7be7b31.jpg')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('logo', 'https://i.pinimg.com/736x/8f/c2/73/8fc2735c66b46b1a88c74e36e7be7b31.jpg')")
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('master_name', 'Дима')") 
        
        c.execute("""CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, duration TEXT, price INTEGER NOT NULL)""")
        if c.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 0:
            c.executemany("INSERT INTO services (name, duration, price) VALUES (?, ?, ?)", [
                ('Мужская стрижка', '1 ч', 50), ('Удаление волос воском', '15 мин', 10),
                ('Женская стрижка', '1 ч 20 мин', 70), ('Стрижка чёлки', '20 мин', 30)])

        c.execute("""CREATE TABLE IF NOT EXISTS blocked_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, time TEXT NOT NULL)""")
                
        c.execute("""CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, client_name TEXT NOT NULL, 
                rating INTEGER NOT NULL, text TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

        c.execute("""CREATE TABLE IF NOT EXISTS promo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, 
                discount INTEGER NOT NULL, is_active INTEGER DEFAULT 1)""")

init_db()

def validate_init_data(init_data: str) -> dict:
    try: parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError: raise HTTPException(401, "Bad initData")
    received = parsed.pop("hash", None)
    if not received: raise HTTPException(401, "No hash")
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received): raise HTTPException(401, "Bad hash")
    return parsed

def auth(x_init_data: str = Header(default="")):
    if DEV_MODE and not x_init_data: return {"id": 0, "first_name": "Dev"}
    if not x_init_data: raise HTTPException(401, "No initData")
    parsed = validate_init_data(x_init_data)
    user = json.loads(parsed.get("user", "{}"))
    if ADMIN_IDS and user.get("id") not in ADMIN_IDS: raise HTTPException(403, "Forbidden")
    return user

class AppointmentIn(BaseModel):
    date: str; time: str; client_name: str; client_phone: str | None = None
    service: str | None = None; price: int | None = None; admin_note: str | None = None
    promo_code: str | None = None

class StatusUpdate(BaseModel): status: str
class SettingsIn(BaseModel): 
    name: str; address: str; avatar: str; logo: str; master_name: str
class ServiceIn(BaseModel): name: str; duration: str; price: int
class BlockIn(BaseModel): date: str; time: str
class ReviewIn(BaseModel): client_name: str; rating: int; text: str | None = None
class PromoIn(BaseModel): code: str; discount: int

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(start_bot())

async def send_tg_message(chat_id: int, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text})

# --- API (ОСТАЛЬНОЙ КОД БЕЗ ИЗМЕНЕНИЙ) ---
@app.get("/api/stats")
def get_stats(start_date: str, end_date: str, user=Depends(auth)):
    with db() as c:
        row = c.execute("SELECT SUM(price) as total, COUNT(*) as count FROM appointments WHERE status='done' AND date >= ? AND date <= ?", (start_date, end_date)).fetchone()
        total = row["total"] or 0
        count = row["count"] or 0
        avg = round(total / count) if count > 0 else 0
        return {"total": total, "count": count, "avg": avg}

@app.get("/api/clients")
def get_clients(user=Depends(auth)):
    with db() as c:
        rows = c.execute("""
            SELECT client_name, client_phone, COUNT(*) as visits, SUM(price) as total_spent 
            FROM appointments WHERE status='done' GROUP BY client_phone ORDER BY total_spent DESC
        """).fetchall()
        return [dict(r) for r in rows]

@app.get("/api/promo/validate")
def validate_promo(code: str):
    with db() as c:
        row = c.execute("SELECT discount FROM promo_codes WHERE code=? AND is_active=1", (code.upper(),)).fetchone()
        if row: return {"valid": True, "discount": row["discount"]}
        raise HTTPException(400, "Промокод не найден")

@app.get("/api/promo")
def get_promos(user=Depends(auth)):
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM promo_codes").fetchall()]

@app.post("/api/promo")
def add_promo(data: PromoIn, user=Depends(auth)):
    with db() as c:
        try: c.execute("INSERT INTO promo_codes (code, discount) VALUES (?, ?)", (data.code.upper(), data.discount))
        except sqlite3.IntegrityError: raise HTTPException(400, "Такой код уже есть")
    return {"ok": True}

@app.delete("/api/promo/{promo_id}")
def delete_promo(promo_id: int, user=Depends(auth)):
    with db() as c: c.execute("DELETE FROM promo_codes WHERE id=?", (promo_id,))

@app.get("/api/reviews")
def get_reviews():
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT 20").fetchall()]

@app.get("/api/reviews/stats")
def get_reviews_stats():
    with db() as c:
        row = c.execute("SELECT COUNT(*) as count, AVG(rating) as avg_rating FROM reviews").fetchone()
        count = row["count"] or 0
        avg_rating = round(row["avg_rating"] or 0, 1)
        return {"count": count, "avg_rating": avg_rating}

@app.post("/api/reviews")
def add_review(data: ReviewIn):
    with db() as c: c.execute("INSERT INTO reviews (client_name, rating, text) VALUES (?, ?, ?)", (data.client_name, data.rating, data.text))
    return {"ok": True}

@app.get("/api/settings")
def get_settings():
    with db() as c: rows = c.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}

@app.post("/api/settings")
def update_settings(data: SettingsIn, user=Depends(auth)):
    with db() as c:
        c.execute("UPDATE settings SET value=? WHERE key='name'", (data.name,))
        c.execute("UPDATE settings SET value=? WHERE key='address'", (data.address,))
        c.execute("UPDATE settings SET value=? WHERE key='avatar'", (data.avatar,))
        c.execute("UPDATE settings SET value=? WHERE key='logo'", (data.logo,))
        c.execute("UPDATE settings SET value=? WHERE key='master_name'", (data.master_name,))
    return {"ok": True}

from upstash_blob import Bucket

bucket = Bucket.from_env()  # читает UPSTASH_BLOB_TOKEN

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), user=Depends(auth)):
    try:
        contents = await file.read()
        # Сохраняем файл в папку barber на Upstash
        blob = bucket.put(f"barber/{file.filename}", contents)
        return {"url": blob.url}
    except Exception as e:
        raise HTTPException(500, f"Ошибка загрузки: {e}")
@app.get("/api/services")
def get_services():
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM services ORDER BY id").fetchall()]

@app.post("/api/services")
def add_service(data: ServiceIn, user=Depends(auth)):
    with db() as c: c.execute("INSERT INTO services (name, duration, price) VALUES (?, ?, ?)", (data.name, data.duration, data.price))

@app.put("/api/services/{srv_id}")
def update_service(srv_id: int, data: ServiceIn, user=Depends(auth)):
    with db() as c: c.execute("UPDATE services SET name=?, duration=?, price=? WHERE id=?", (data.name, data.duration, data.price, srv_id))

@app.delete("/api/services/{srv_id}")
def delete_service(srv_id: int, user=Depends(auth)):
    with db() as c: c.execute("DELETE FROM services WHERE id=?", (srv_id,))

@app.get("/api/blocked")
def get_blocked(date: str, user=Depends(auth)):
    with db() as c: return [r["time"] for r in c.execute("SELECT time FROM blocked_slots WHERE date=?", (date,)).fetchall()]

@app.post("/api/blocked")
def block_slot(data: BlockIn, user=Depends(auth)):
    with db() as c: c.execute("INSERT INTO blocked_slots (date, time) VALUES (?, ?)", (data.date, data.time))

@app.delete("/api/blocked/{date}/{time}")
def unblock_slot(date: str, time: str, user=Depends(auth)):
    with db() as c: c.execute("DELETE FROM blocked_slots WHERE date=? AND time=?", (date, time))

@app.get("/api/appointments")
def list_appointments(date: str, user=Depends(auth)):
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM appointments WHERE date=? ORDER BY time", (date,)).fetchall()]

@app.get("/api/calendar")
def calendar_month(year: int, month: int, user=Depends(auth)):
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year+1:04d}-01-01" if month == 12 else f"{year:04d}-{month+1:02d}-01"
    with db() as c: return {r["date"]: r["cnt"] for r in c.execute("SELECT date, COUNT(*) cnt FROM appointments WHERE date>=? AND date<? GROUP BY date", (start, end)).fetchall()}

@app.post("/api/appointments")
def create_appt_admin(data: AppointmentIn, user=Depends(auth)):
    with db() as c:
        cur = c.execute("INSERT INTO appointments(date,time,client_name,client_phone,service,price,admin_note) VALUES(?,?,?,?,?,?,?)", 
                        (data.date, data.time, data.client_name, data.client_phone, data.service, data.price, data.admin_note))
        return {"id": cur.lastrowid}

@app.put("/api/appointments/{apt_id}")
def update_appt(apt_id: int, data: AppointmentIn, user=Depends(auth)):
    with db() as c:
        c.execute("UPDATE appointments SET date=?, time=?, client_name=?, client_phone=?, service=?, price=?, admin_note=? WHERE id=?", 
                  (data.date, data.time, data.client_name, data.client_phone, data.service, data.price, data.admin_note, apt_id))

@app.patch("/api/appointments/{apt_id}")
def update_status(apt_id: int, upd: StatusUpdate, user=Depends(auth)):
    with db() as c: c.execute("UPDATE appointments SET status=? WHERE id=?", (upd.status, apt_id))

@app.delete("/api/appointments/{apt_id}")
def delete_appt(apt_id: int, user=Depends(auth)):
    with db() as c: c.execute("DELETE FROM appointments WHERE id=?", (apt_id,))

@app.get("/api/slots")
def get_slots(date: str):
    with db() as c:
        booked = [r["time"] for r in c.execute("SELECT time FROM appointments WHERE date=? AND status != 'cancelled'", (date,)).fetchall()]
        blocked = [r["time"] for r in c.execute("SELECT time FROM blocked_slots WHERE date=?", (date,)).fetchall()]
        return list(set(booked + blocked))

@app.get("/api/my_appointments")
def my_appts(x_init_data: str = Header(default="")):
    parsed = validate_init_data(x_init_data)
    user_id = json.loads(parsed.get("user", "{}")).get("id")
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM appointments WHERE client_tg_id=? AND status='confirmed' AND date >= date('now')", (user_id,)).fetchall()]

@app.post("/api/book")
async def book_appt(data: AppointmentIn, x_init_data: str = Header(default="")):
    user_id = None
    if x_init_data:
        parsed = validate_init_data(x_init_data)
        user_id = json.loads(parsed.get("user", "{}")).get("id")

    final_price = data.price
    promo_text = ""

    if data.promo_code:
        with db() as c:
            row = c.execute("SELECT discount FROM promo_codes WHERE code=? AND is_active=1", (data.promo_code.upper(),)).fetchone()
            if row:
                discount = row["discount"]
                final_price = int(data.price * (1 - discount/100))
                promo_text = f"\n🏷️ Промокод: {data.promo_code} (-{discount}%)"
            else:
                raise HTTPException(400, "Неверный промокод")

    with db() as c:
        if c.execute("SELECT id FROM appointments WHERE date=? AND time=? AND status != 'cancelled'", (data.date, data.time)).fetchone():
            raise HTTPException(400, "Занято")
        c.execute("INSERT INTO appointments(date,time,client_name,client_phone,service,price,client_tg_id,promo_code) VALUES(?,?,?,?,?,?,?,?)", 
                  (data.date, data.time, data.client_name, data.client_phone, data.service, final_price, user_id, data.promo_code))
    
    for admin in ADMIN_IDS:
        await send_tg_message(admin, f"🔥 Новая запись!\n📅 {data.date} в {data.time}\n✂️ {data.service} ({final_price} BYN){promo_text}\n👤 {data.client_name}\n📞 {data.client_phone}")
    return {"ok": True}

@app.post("/api/cancel_my/{apt_id}")
def cancel_my(apt_id: int, x_init_data: str = Header(default="")):
    parsed = validate_init_data(x_init_data)
    user_id = json.loads(parsed.get("user", "{}")).get("id")
    with db() as c: c.execute("UPDATE appointments SET status='cancelled' WHERE id=? AND client_tg_id=?", (apt_id, user_id))
    return {"ok": True}

app.mount("/static", StaticFiles(directory="static"), name="static")
@app.get("/")
def index(): return FileResponse("static/index.html")
@app.get("/client")
def client_index(): return FileResponse("static/client.html")
