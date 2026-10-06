import asyncio, base64, hashlib, hmac, json, os, sqlite3, time
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, MenuButtonWebApp, WebAppInfo
from anthropic import AsyncAnthropic

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
MODEL = os.getenv("MODEL", "claude-sonnet-4-6")
PRICE_STARS = int(os.getenv("PRICE_STARS", "100"))
FREE_ANALYSES = int(os.getenv("FREE_ANALYSES", "1"))
FREE_CHAT = int(os.getenv("FREE_CHAT", "3"))
PERIOD = 30 * 24 * 3600

bot, dp, ai = Bot(TOKEN), Dispatcher(), AsyncAnthropic()
db = sqlite3.connect(os.getenv("DB", "doctor.db"), check_same_thread=False, timeout=30)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=30000")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, profile TEXT DEFAULT '{}',
  sub_until INTEGER DEFAULT 0, used_analyses INTEGER DEFAULT 0, used_chat INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER,
  kind TEXT, value TEXT, note TEXT, ai TEXT, date TEXT);
CREATE TABLE IF NOT EXISTS chat(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER,
  role TEXT, text TEXT, ts INTEGER);""")

SYSTEM = """Ты — AI Doctor, внимательный медицинский ассистент в Telegram. Отвечай на языке пользователя, просто и тепло.
Задачи: расшифровывать анализы и исследования (МРТ, КТ, УЗИ), анализировать дневник показателей ВМЕСТЕ (сахар, давление, анализы и т.д.), давать советы по питанию, образу жизни, витаминам, добавкам и травам.
Правила:
- Ты не ставишь диагноз и не заменяешь врача. Говори «это может указывать на…», «обсудите с врачом».
- Для каждого показателя: значение → норма → что значит → что делать. Отмечай отклонения (↑/↓).
- Питание: конкретно — что добавить, что ограничить, примеры блюд на день.
- Лекарства: не назначай и не меняй дозировки рецептурных препаратов. Можно объяснять, как они действуют, и о чём спросить врача.
- Травы и добавки: всегда предупреждай о противопоказаниях и взаимодействиях с лекарствами, учитывай аллергии, хронические болезни, беременность из профиля.
- Если видишь опасные признаки (давление ≥180/120, сахар <3.0 или >16 ммоль/л, боль в груди, одышка, неврологические симптомы, кровотечение) — первой строкой напиши: «⚠️ Срочно вызовите скорую (103/112)».
- Если на фото нечитаемо — попроси переснять. Не выдумывай значения.
- Учитывай профиль, историю записей и прошлые сообщения. Если данных мало — скажи, какие добавить в дневник.
- Структура: короткий вывод, затем разделы с эмодзи-заголовками, в конце — 2–3 вопроса к врачу."""

def uid_from(req):
    d = dict(parse_qsl(req.headers.get("X-Init", ""), keep_blank_values=True))
    h = d.pop("hash", "")
    s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
        raise web.HTTPUnauthorized()
    try:
        auth_date = int(d.get("auth_date", 0))
    except ValueError:
        raise web.HTTPUnauthorized()
    if time.time() - auth_date > 86400 * 3:
        raise web.HTTPUnauthorized()
    try:
        uid = json.loads(d["user"])["id"]
    except Exception:
        raise web.HTTPUnauthorized()
    db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,)); db.commit()
    return uid

def user(uid): return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
def active(u): return u["sub_until"] > time.time()

def status(uid):
    u = user(uid)
    return {"profile": json.loads(u["profile"]), "sub_until": u["sub_until"], "active": active(u),
            "free_analyses": max(0, FREE_ANALYSES - u["used_analyses"]),
            "free_chat": max(0, FREE_CHAT - u["used_chat"]), "price": PRICE_STARS}

def context(uid):
    u = user(uid)
    rows = db.execute("SELECT kind,value,note,date FROM records WHERE uid=? ORDER BY date DESC, id DESC LIMIT 60", (uid,)).fetchall()
    recs = "\n".join(f"{r['date']} | {r['kind']} | {r['value']} | {r['note'] or ''}" for r in rows) or "нет записей"
    return f"ПРОФИЛЬ: {u['profile']}\nДНЕВНИК (дата | тип | значение | заметка):\n{recs}"

async def ask(uid, content):
    hist = db.execute("SELECT role,text FROM chat WHERE uid=? ORDER BY id DESC LIMIT 12", (uid,)).fetchall()[::-1]
    msgs = [{"role": h["role"], "content": h["text"]} for h in hist]
    msgs.append({"role": "user", "content": content})
    while msgs and msgs[0]["role"] != "user": msgs.pop(0)
    r = await ai.messages.create(model=MODEL, max_tokens=2000,
                                 system=SYSTEM + "\n\n" + context(uid), messages=msgs)
    return "".join(b.text for b in r.content if b.type == "text")

def log(uid, role, text):
    db.execute("INSERT INTO chat(uid,role,text,ts) VALUES(?,?,?,?)", (uid, role, text, int(time.time()))); db.commit()

async def jreq(req):
    uid = uid_from(req)
    return uid, (await req.json() if req.can_read_body else {})

async def api_me(req): return web.json_response(status(uid_from(req)))

async def api_profile(req):
    uid, b = await jreq(req)
    db.execute("UPDATE users SET profile=? WHERE id=?", (json.dumps(b, ensure_ascii=False)[:3000], uid)); db.commit()
    return web.json_response({"ok": True})

async def api_records(req):
    uid, b = await jreq(req)
    if req.method == "GET":
        rows = db.execute("SELECT * FROM records WHERE uid=? ORDER BY date DESC,id DESC LIMIT 300", (uid,)).fetchall()
        return web.json_response([dict(r) for r in rows])
    if req.method == "POST":
        db.execute("INSERT INTO records(uid,kind,value,note,date) VALUES(?,?,?,?,?)",
                   (uid, b["kind"], str(b.get("value", ""))[:500], str(b.get("note", ""))[:1000], b["date"]))
    else:
        db.execute("DELETE FROM records WHERE id=? AND uid=?", (b["id"], uid))
    db.commit()
    return web.json_response({"ok": True})

async def api_history(req):
    uid = uid_from(req)
    rows = db.execute("SELECT role,text FROM chat WHERE uid=? ORDER BY id DESC LIMIT 40", (uid,)).fetchall()[::-1]
    return web.json_response([dict(r) for r in rows])

def paywall(): return web.json_response({"error": "paywall"}, status=402)

async def api_chat(req):
    uid, b = await jreq(req)
    u = user(uid)
    if not active(u) and u["used_chat"] >= FREE_CHAT: return paywall()
    text = str(b.get("text", ""))[:4000].strip()
    if not text: return web.json_response({"error": "empty"}, status=400)
    try:
        ans = await ask(uid, text)
    except Exception as e:
        return web.json_response({"error": "ai", "message": str(e)[:300]}, status=502)
    if not active(u): db.execute("UPDATE users SET used_chat=used_chat+1 WHERE id=?", (uid,))
    log(uid, "user", text); log(uid, "assistant", ans)
    return web.json_response({"text": ans, **status(uid)})

async def api_analyze(req):
    uid, b = await jreq(req)
    u = user(uid)
    if not active(u) and u["used_analyses"] >= FREE_ANALYSES: return paywall()
    kind, note, date = b.get("kind", "lab"), str(b.get("note", ""))[:500], b.get("date")
    images = b.get("images") or []
    if not date or not images: return web.json_response({"error": "missing_data"}, status=400)
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": i}}
               for i in images[:4]]
    content.append({"type": "text", "text": f"Тип: {kind}. Дата: {date}. Комментарий: {note}\nРасшифруй, сравни с моим дневником и дай советы по питанию и образу жизни."})
    try:
        ans = await ask(uid, content)
    except Exception as e:
        return web.json_response({"error": "ai", "message": str(e)[:300]}, status=502)
    if not active(u): db.execute("UPDATE users SET used_analyses=used_analyses+1 WHERE id=?", (uid,))
    db.execute("INSERT INTO records(uid,kind,value,note,ai,date) VALUES(?,?,?,?,?,?)",
               (uid, kind, "фото", note, ans, date))
    log(uid, "user", f"[Загружено фото: {kind}, {date}]"); log(uid, "assistant", ans)
    return web.json_response({"text": ans, **status(uid)})

async def api_invoice(req):
    uid_from(req)
    link = await bot.create_invoice_link(
        title="AI Doctor — 30 дней", description="Безлимитные анализы, дневник и советы. Отмена в любой момент.",
        payload="sub30", currency="XTR", prices=[LabeledPrice(label="30 дней", amount=PRICE_STARS)],
        subscription_period=PERIOD)
    return web.json_response({"link": link})

async def api_wipe(req):
    uid = uid_from(req)
    for t in ("records", "chat"): db.execute(f"DELETE FROM {t} WHERE uid=?", (uid,))
    db.execute("UPDATE users SET profile='{}' WHERE id=?", (uid,)); db.commit()
    return web.json_response({"ok": True})

@dp.message(Command("start"))
async def start(m):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🩺 Открыть AI Doctor", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("Привет! Я AI Doctor 🩺\n\n• Расшифрую фото анализов\n• Веду дневник: сахар, давление, МРТ, КТ, УЗИ\n• Анализирую всё вместе и советую по питанию, добавкам и травам\n\n"
                   f"Первый анализ бесплатно, потом {PRICE_STARS} ⭐ в месяц.\nЯ не заменяю врача.", reply_markup=kb)

@dp.message(Command("paysupport"))
async def paysupport(m): await m.answer("Вопросы по оплате: напишите администратору бота. Подписку можно отменить в Telegram: Настройки → Telegram Stars → Подписки.")

@dp.message(F.photo)
async def photo_hint(m):
    await m.answer("Фото лучше загружать в приложении — там я сохраню результат в ваш дневник 👇",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📷 Загрузить анализы", web_app=WebAppInfo(url=WEBAPP_URL))]]))

@dp.pre_checkout_query()
async def pre(q): await q.answer(ok=True)

@dp.message(F.successful_payment)
async def paid(m):
    u = user(m.from_user.id)
    if not u: db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (m.from_user.id,)); db.commit(); u = user(m.from_user.id)
    until = max(u["sub_until"], int(time.time())) + PERIOD
    db.execute("UPDATE users SET sub_until=? WHERE id=?", (until, m.from_user.id)); db.commit()
    await m.answer("✅ Подписка активна на 30 дней. Откройте приложение — всё безлимитно!")

async def main():
    app = web.Application(client_max_size=25 * 1024 * 1024)
    app.add_routes([
        web.get("/health", lambda r: web.json_response({"ok": True})),
        web.get("/api/me", api_me), web.post("/api/profile", api_profile),
        web.route("*", "/api/records", api_records), web.get("/api/history", api_history),
        web.post("/api/chat", api_chat), web.post("/api/analyze", api_analyze),
        web.post("/api/invoice", api_invoice), web.post("/api/wipe", api_wipe),
        web.get("/", lambda r: web.FileResponse("static/index.html"))
    ])
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="AI Doctor", web_app=WebAppInfo(url=WEBAPP_URL)))
    await dp.start_polling(bot)

asyncio.run(main())