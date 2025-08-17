import os
import json
import asyncio
from typing import Optional, Dict, List

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

import aiosqlite

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, AIORateLimiter, filters
)

# =========================
# تحميل الإعدادات
# =========================
with open("config.json", "r", encoding="utf-8") as f:
    CFG = json.load(f)

BOT_NAME: str = CFG.get("BOT_NAME", "Batman")
BOT_TOKEN: str = CFG["BOT_TOKEN"]
ADMIN_IDS: List[int] = CFG["ADMIN_IDS"]
WEBHOOK_HOST: str = CFG["WEBHOOK_HOST"].rstrip("/")
WEBHOOK_SECRET: str = CFG["WEBHOOK_SECRET"]
APP_PORT: int = int(CFG.get("APP_PORT", 10000))
CONTACT_URL: str = CFG.get("CONTACT_URL", "https://t.me/e2E12")
MAINTENANCE_DEFAULT: bool = bool(CFG.get("MAINTENANCE", False))

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "batman.db")

ACCOUNTS_FILE = "accounts.json"
if not os.path.exists(ACCOUNTS_FILE):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"instagram": [], "telegram": []}, f, ensure_ascii=False, indent=2)

def load_accounts():
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("instagram", []), data.get("telegram", [])
    except Exception:
        return [], []

def save_accounts(insta_list, tg_list):
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"instagram": insta_list, "telegram": tg_list}, f, ensure_ascii=False, indent=2)

instagram_accounts, telegram_accounts = load_accounts()

# =========================
# FastAPI & Telegram
# =========================
app = FastAPI(title=f"{BOT_NAME} Control Bot")
application: Application = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .rate_limiter(AIORateLimiter())
    .build()
)

# =========================
# قاعدة البيانات
# =========================
INIT_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  username TEXT, first_name TEXT, last_name TEXT,
  is_banned INTEGER DEFAULT 0,
  is_vip INTEGER DEFAULT 0,
  joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  action TEXT,
  extra TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

async def adb():
    return await aiosqlite.connect(DB_PATH)

async def init_db():
    async with await adb() as con:
        await con.executescript(INIT_SQL)
        # احفظ وضع الصيانة الافتراضي مرة واحدة
        cur = await con.execute("SELECT value FROM settings WHERE key='maintenance'")
        row = await cur.fetchone()
        if row is None:
            await con.execute("INSERT INTO settings(key,value) VALUES('maintenance', ?)", (json.dumps(MAINTENANCE_DEFAULT),))
        await con.commit()

asyncio.get_event_loop().run_until_complete(init_db())

# =========================
# أدوات
# =========================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def ensure_user(update: Update):
    if not update.effective_user:
        return
    u = update.effective_user
    async with await adb() as con:
        await con.execute("""
            INSERT INTO users(user_id, username, first_name, last_name)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name
        """, (u.id, u.username, u.first_name, u.last_name))
        await con.commit()

async def user_is_banned(user_id: int) -> bool:
    async with await adb() as con:
        cur = await con.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    return bool(row and row[0] == 1)

async def log_action(user_id: int, action: str, extra: Optional[str]=None):
    async with await adb() as con:
        await con.execute("INSERT INTO logs(user_id, action, extra) VALUES(?,?,?)",
                          (user_id, action, extra))
        await con.commit()

async def get_maintenance() -> bool:
    async with await adb() as con:
        cur = await con.execute("SELECT value FROM settings WHERE key='maintenance'")
        row = await cur.fetchone()
    return bool(json.loads(row[0]) if row else False)

async def set_maintenance(val: bool):
    async with await adb() as con:
        await con.execute("INSERT INTO settings(key,value) VALUES('maintenance',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          ('maintenance', json.dumps(val)))
        await con.commit()

# =========================
# واجهة الأزرار
# =========================
def stopped_message():
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 تواصل مع المالك", url=CONTACT_URL)]
    ])
    return "⛔ البوت في وضع الصيانة أو التوقف حالياً.", kb

def admin_panel():
    rows = [
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="adm_stats"),
         InlineKeyboardButton("👥 المستخدمون", callback_data="adm_users")],
        [InlineKeyboardButton("📣 إذاعة", callback_data="adm_broadcast"),
         InlineKeyboardButton("🔍 بحث", callback_data="adm_search")],
        [InlineKeyboardButton("🚫 حظر/✅ فك", callback_data="adm_ban_menu"),
         InlineKeyboardButton("💎 تبديل VIP", callback_data="adm_vip")],
        [InlineKeyboardButton("🧩 الحسابات", callback_data="adm_accounts"),
         InlineKeyboardButton("📝 السجلّات", callback_data="adm_logs")],
        [InlineKeyboardButton("🧰 نسخ احتياطي", callback_data="adm_backup"),
         InlineKeyboardButton("♻️ صيانة: تبديل", callback_data="adm_toggle_maint")],
        [InlineKeyboardButton("🔄 تحديث اللوحة", callback_data="adm_refresh")]
    ]
    return InlineKeyboardMarkup(rows)

def accounts_menu():
    rows = [
        [InlineKeyboardButton("📸 إنستغرام", callback_data="acc_insta"),
         InlineKeyboardButton("💬 تيليجرام", callback_data="acc_tg")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="adm_refresh")]
    ]
    return InlineKeyboardMarkup(rows)

def list_accounts_kb(kind: str, items: List[str]):
    rows = [[InlineKeyboardButton(f"• {item}", callback_data=f"noop")] for item in items] or [[InlineKeyboardButton("— لا يوجد —", callback_data="noop")]]
    rows += [
        [InlineKeyboardButton("➕ إضافة", callback_data=f"acc_{kind}_add"),
         InlineKeyboardButton("➖ حذف", callback_data=f"acc_{kind}_del")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="adm_accounts")]
    ]
    return InlineKeyboardMarkup(rows)

# حالات مؤقتة للأدمن
ADMIN_STATE: Dict[int, Dict[str, str]] = {}

# =========================
# الأوامر
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    user = update.effective_user
    if not user:
        return

    # وضع صيانة أو حظر
    if not is_admin(user.id):
        if await get_maintenance() or await user_is_banned(user.id):
            text, kb = stopped_message()
            await update.effective_message.reply_text(text, reply_markup=kb)
            return

    if is_admin(user.id):
        await update.effective_message.reply_text(
            f"أهلاً يا {BOT_NAME} 🦇\nلوحتك السرية:",
            reply_markup=admin_panel()
        )
    else:
        await update.effective_message.reply_text(
            f"مرحباً 👋\nأنا بوت {BOT_NAME} 🦇 — حارس الظلال هنا ✨"
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/start — بدء\n"
        "/help — مساعدة\n"
        "/id — عرض آيدي\n",
        parse_mode=ParseMode.HTML
    )

async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.effective_message.reply_text(f"🆔 آيديك: <code>{u.id}</code>", parse_mode=ParseMode.HTML)

# نصوص عامة (إن احتجناها لحالات الإذاعة/بحث)
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    state = ADMIN_STATE.get(user.id)
    if not (state and is_admin(user.id)):
        return

    mode = state.get("mode")

    # إذاعة
    if mode == "broadcast_wait":
        msg = update.effective_message.text or ""
        sent, failed = 0, 0
        async with await adb() as con:
            cur = await con.execute("SELECT user_id FROM users WHERE is_banned=0")
            rows = await cur.fetchall()
        for (uid,) in rows:
            try:
                await context.bot.send_message(chat_id=uid, text=msg)
                await asyncio.sleep(0.03)
                sent += 1
            except Exception:
                failed += 1
        ADMIN_STATE.pop(user.id, None)
        await update.effective_message.reply_text(f"تم الإرسال ✅\nنجح: {sent} • فشل: {failed}")
        await log_action(user.id, "broadcast", f"sent={sent}, failed={failed}")
        return

    # بحث
    if mode == "search_wait":
        q = (update.effective_message.text or "").strip()
        async with await adb() as con:
            cur = await con.execute("""
              SELECT user_id, username, first_name, last_name, is_banned, is_vip, joined_at
              FROM users
              WHERE CAST(user_id AS TEXT) LIKE ?
                 OR IFNULL(username,'') LIKE ?
                 OR IFNULL(first_name,'') LIKE ?
                 OR IFNULL(last_name,'') LIKE ?
              LIMIT 30
            """, (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"))
            rows = await cur.fetchall()
        if not rows:
            await update.effective_message.reply_text("لا نتائج.")
        else:
            lines = []
            for r in rows:
                uid, un, fn, ln, banned, vip, joined = r
                lines.append(
                    f"• <b>{uid}</b> | @{un or '-'} | {fn or ''} {ln or ''} | "
                    f"{'🚫' if banned else '✅'} | {'💎' if vip else '—'} | {joined}"
                )
            await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        ADMIN_STATE.pop(user.id, None)
        return

    # إضافة/حذف حساب
    if mode in ("add_insta", "add_tg", "del_insta", "del_tg"):
        name = (update.effective_message.text or "").strip()
        global instagram_accounts, telegram_accounts
        changed = False

        if mode == "add_insta" and name:
            if name not in instagram_accounts:
                instagram_accounts.append(name); changed=True
        elif mode == "add_tg" and name:
            if name not in telegram_accounts:
                telegram_accounts.append(name); changed=True
        elif mode == "del_insta":
            if name in instagram_accounts:
                instagram_accounts = [x for x in instagram_accounts if x != name]; changed=True
        elif mode == "del_tg":
            if name in telegram_accounts:
                telegram_accounts = [x for x in telegram_accounts if x != name]; changed=True

        if changed:
            save_accounts(instagram_accounts, telegram_accounts)
            await update.effective_message.reply_text("تم الحفظ ✅", reply_markup=admin_panel())
            await log_action(user.id, "accounts_update", f"mode={mode}, name={name}")
        else:
            await update.effective_message.reply_text("لم يحدث تغيير.")

        ADMIN_STATE.pop(user.id, None)
        return

# كول باك للأزرار
async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    u = update.effective_user
    if not (q and u):
        return
    await q.answer()

    if not is_admin(u.id):
        text, kb = stopped_message()
        await q.edit_message_text(text, reply_markup=kb)
        return

    data = q.data

    if data == "adm_refresh":
        await q.edit_message_reply_markup(reply_markup=admin_panel()); return

    if data == "adm_stats":
        async with await adb() as con:
            total = (await (await con.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            banned = (await (await con.execute("SELECT COUNT(*) FROM users WHERE is_banned=1")).fetchone())[0]
            vip = (await (await con.execute("SELECT COUNT(*) FROM users WHERE is_vip=1")).fetchone())[0]
        await q.edit_message_text(
            f"📊 <b>الإحصائيات</b>\n- المستخدمون: <b>{total}</b>\n- المحظورون: <b>{banned}</b>\n- VIP: <b>{vip}</b>",
            parse_mode=ParseMode.HTML, reply_markup=admin_panel()
        )
        await log_action(u.id, "stats"); return

    if data == "adm_users":
        async with await adb() as con:
            cur = await con.execute("""
              SELECT user_id, username, first_name, last_name, is_banned, is_vip, joined_at
              FROM users ORDER BY joined_at DESC LIMIT 20
            """); rows = await cur.fetchall()
        if not rows:
            await q.edit_message_text("لا يوجد مستخدمون بعد.", reply_markup=admin_panel()); return
        lines = []
        for r in rows:
            uid, un, fn, ln, banned, vip, joined = r
            lines.append(
                f"• <b>{uid}</b> | @{un or '-'} | {fn or ''} {ln or ''} | "
                f"{'🚫' if banned else '✅'} | {'💎' if vip else '—'} | {joined}"
            )
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=admin_panel()); return

    if data == "adm_broadcast":
        ADMIN_STATE[u.id] = {"mode": "broadcast_wait"}
        await q.edit_message_text("أرسل نص الإذاعة الآن…", reply_markup=admin_panel()); return

    if data == "adm_search":
        ADMIN_STATE[u.id] = {"mode": "search_wait"}
        await q.edit_message_text("أرسل كلمة البحث (آيدي/يوزر/اسم)…", reply_markup=admin_panel()); return

    if data == "adm_ban_menu":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 حظر مستخدم", callback_data="adm_ban")],
            [InlineKeyboardButton("✅ فك الحظر", callback_data="adm_unban")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="adm_refresh")]
        ])
        await q.edit_message_text("اختر الإجراء:", reply_markup=kb); return

    if data in ("adm_ban", "adm_unban", "adm_vip"):
        mode = {"adm_ban": "ban", "adm_unban": "unban", "adm_vip": "vip"}[data]
        ADMIN_STATE[u.id] = {"mode": f"{mode}_wait"}
        await q.edit_message_text("أرسل آيدي المستخدم:", reply_markup=admin_panel()); return

    if data == "adm_logs":
        async with await adb() as con:
            cur = await con.execute("SELECT user_id, action, extra, created_at FROM logs ORDER BY id DESC LIMIT 20")
            rows = await cur.fetchall()
        if not rows:
            await q.edit_message_text("لا توجد سجلات بعد.", reply_markup=admin_panel()); return
        lines = [f"• {t} | {act} | by {uid} | {extra or ''}" for uid, act, extra, t in rows]
        await q.edit_message_text("\n".join(lines), reply_markup=admin_panel()); return

    if data == "adm_accounts":
        await q.edit_message_text("اختر نوع الحساب:", reply_markup=accounts_menu()); return

    if data in ("acc_insta", "acc_tg"):
        kind = "insta" if data == "acc_insta" else "tg"
        items = instagram_accounts if kind == "insta" else telegram_accounts
        title = "📸 إنستغرام" if kind == "insta" else "💬 تيليجرام"
        await q.edit_message_text(f"{title}:", reply_markup=list_accounts_kb(kind, items)); return

    if data in ("acc_insta_add", "acc_tg_add", "acc_insta_del", "acc_tg_del"):
        mode = data.replace("acc_", "").replace("_add", "").replace("_del", "")
        # mode: insta / tg  + implicit add/del
        if data.endswith("_add"):
            ADMIN_STATE[u.id] = {"mode": f"add_{mode}"}
            await q.edit_message_text("أرسل اسم/معرّف الحساب لإضافته:", reply_markup=admin_panel()); return
        else:
            ADMIN_STATE[u.id] = {"mode": f"del_{mode}"}
            await q.edit_message_text("أرسل الاسم/المعرّف لحذفه:", reply_markup=admin_panel()); return

    if data == "adm_backup":
        # تصدير users & logs & accounts كملفات
        # users.json
        async with await adb() as con:
            cur = await con.execute("SELECT user_id, username, first_name, last_name, is_banned, is_vip, joined_at FROM users")
            users_rows = await cur.fetchall()
            cur = await con.execute("SELECT id, user_id, action, extra, created_at FROM logs ORDER BY id DESC")
            logs_rows = await cur.fetchall()
        users_list = [dict(user_id=r[0], username=r[1], first_name=r[2], last_name=r[3],
                           is_banned=r[4], is_vip=r[5], joined_at=r[6]) for r in users_rows]
        logs_list = [dict(id=r[0], user_id=r[1], action=r[2], extra=r[3], created_at=r[4]) for r in logs_rows]

        users_path = os.path.join(DATA_DIR, "users_export.json")
        logs_path = os.path.join(DATA_DIR, "logs_export.json")
        acc_path = os.path.join(DATA_DIR, "accounts_export.json")

        with open(users_path, "w", encoding="utf-8") as f: json.dump(users_list, f, ensure_ascii=False, indent=2)
        with open(logs_path, "w", encoding="utf-8") as f: json.dump(logs_list, f, ensure_ascii=False, indent=2)
        with open(acc_path, "w", encoding="utf-8") as f:
            json.dump({"instagram": instagram_accounts, "telegram": telegram_accounts}, f, ensure_ascii=False, indent=2)

        await context.bot.send_document(chat_id=u.id, document=InputFile(users_path))
        await context.bot.send_document(chat_id=u.id, document=InputFile(logs_path))
        await context.bot.send_document(chat_id=u.id, document=InputFile(acc_path))
        await q.edit_message_text("تم إرسال ملفات النسخ الاحتياطي ✅", reply_markup=admin_panel())
        await log_action(u.id, "backup"); return

    if data == "adm_toggle_maint":
        val = not (await get_maintenance())
        await set_maintenance(val)
        await q.edit_message_text(f"وضع الصيانة: {'مفعّل' if val else 'متوقف'}", reply_markup=admin_panel())
        await log_action(u.id, "toggle_maintenance", f"value={val}"); return

    # زر بلا فعل
    if data == "noop":
        return

# أوضاع إدخال آيدي للحظر/فك/VIP
async def admin_text_modes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or not is_admin(u.id):
        return
    state = ADMIN_STATE.get(u.id)
    if not state:
        return
    mode = state.get("mode")
    if mode not in ("ban_wait", "unban_wait", "vip_wait"):
        return

    raw = (update.effective_message.text or "").strip()
    try:
        target_id = int(raw)
    except ValueError:
        await update.effective_message.reply_text("أدخل آيدي رقمي صحيح.")
        return

    async with await adb() as con:
        cur = await con.execute("SELECT user_id FROM users WHERE user_id=?", (target_id,))
        exists = await cur.fetchone()

    if not exists:
        await update.effective_message.reply_text("المستخدم غير موجود بقاعدة البيانات.")
        ADMIN_STATE.pop(u.id, None); return

    if mode == "ban_wait":
        async with await adb() as con:
            await con.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (target_id,))
            await con.commit()
        await update.effective_message.reply_text("تم الحظر 🚫"); await log_action(u.id, "ban", f"target={target_id}")

    elif mode == "unban_wait":
        async with await adb() as con:
            await con.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (target_id,))
            await con.commit()
        await update.effective_message.reply_text("تم فك الحظر ✅"); await log_action(u.id, "unban", f"target={target_id}")

    else:  # vip_wait
        async with await adb() as con:
            await con.execute("UPDATE users SET is_vip = CASE WHEN is_vip=1 THEN 0 ELSE 1 END WHERE user_id=?", (target_id,))
            await con.commit()
            (new_vip,) = await (await con.execute("SELECT is_vip FROM users WHERE user_id=?", (target_id,))).fetchone()
        await update.effective_message.reply_text(f"تم التبديل: {'💎 VIP' if new_vip else 'غير VIP'}")
        await log_action(u.id, "toggle_vip", f"target={target_id}")

    ADMIN_STATE.pop(u.id, None)

# =========================
# ربط الهاندلرز
# =========================
application.add_handler(CommandHandler("start", start_cmd))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(CommandHandler("id", id_cmd))
application.add_handler(CallbackQueryHandler(admin_cb))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_modes))

# =========================
# Webhook: FastAPI
# =========================
@app.on_event("startup")
async def on_startup():
    # ضبط الويبهوك بمفتاح سرّي
    await application.bot.set_webhook(
        url=f"{WEBHOOK_HOST}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query"]
    )

@app.on_event("shutdown")
async def on_shutdown():
    await application.stop()

@app.get("/")
async def root():
    return {"status": "ok", "bot": BOT_NAME, "webhook": WEBHOOK_PATH}

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: Optional[str] = Header(None)):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    # ممر المعالجة — آمن مع PTB v21
    await application.process_update(update)
    return JSONResponse({"ok": True})
