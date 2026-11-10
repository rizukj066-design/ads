import os
import time
import logging
import sqlite3
import asyncio
from datetime import datetime
from pathlib import Path

# --- RENDER KE LIYE NAYA IMPORT ---
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
# ---------------------------------

from dotenv import load_dotenv
from cryptography.fernet import Fernet

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, MessageOriginChannel, MessageOriginChat
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest
from telegram.error import BadRequest

from pyrogram import Client, filters as pyro_filters
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    FloodWait, PhoneNumberInvalid, ChatWriteForbidden, ChannelPrivate,
    UserBannedInChannel, ChatAdminRequired, SlowmodeWait, PeerFlood, PeerIdInvalid,
    AuthKeyDuplicated, AuthKeyUnregistered, BadRequest as PyroBadRequest
)
from pyrogram.enums import ChatType

# ==================== SETUP ====================
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logging.getLogger("pyrogram").setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)

# ==================== CONFIG ====================
MASTER_BOT_TOKEN = os.getenv("MASTER_BOT_TOKEN", "").strip()
API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "").strip()

if not MASTER_BOT_TOKEN:
    raise SystemExit("❌ MASTER_BOT_TOKEN missing. Create a .env file.")
if not API_ID or not API_HASH:
    raise SystemExit("❌ API_ID / API_HASH missing in .env")

_KEY_FILE = Path("secret.key")
_FERNET_RAW = os.getenv("FERNET_KEY", "").strip()
if not _FERNET_RAW:
    if _KEY_FILE.exists():
        _FERNET_RAW = _KEY_FILE.read_text().strip()
    else:
        _FERNET_RAW = Fernet.generate_key().decode()
        _KEY_FILE.write_text(_FERNET_RAW)
        logger.warning("🔐 Generated new FERNET_KEY → saved in secret.key")
fernet = Fernet(_FERNET_RAW.encode() if isinstance(_FERNET_RAW, str) else _FERNET_RAW)

def encrypt_session(s: str) -> str:
    return fernet.encrypt(s.encode()).decode()

def decrypt_session(s: str) -> str:
    return fernet.decrypt(s.encode()).decode()

tg_bot = Bot(token=MASTER_BOT_TOKEN)

POWERED_BY_TEXT = "━━━ Powered by Chairman ━━━"
DEVELOPER_TEXT = "Developed by: @Yo_cum"
PREMIUM_USERNAME = "@yo_cum"
ABOUT_POWERED_USERNAME = "@gameyopq"

# 🔴 RENDER KE LIYE FIXED DB PATH (Yahan /data disk mount hoti hai)
DB_PATH = os.path.join(os.getenv("RENDER_DISK_PATH", "."), "adsbot.db")

# ==================== STATE ====================
pending_logins = {}          
waiting_input = {}           
last_replied_users = {}      
user_chat_cache = {}         
target_selections = {}       
target_page = {}             
active_ad_tasks = {}         
active_target_tasks = {}     
background_tasks = []        
auto_reply_listeners = set() 

# ==================== CLIENT MANAGER ====================
class ClientManager:
    def __init__(self):
        self._clients: dict[str, Client] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._startup_lock = asyncio.Lock()

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self._startup_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def get(self, session_str: str) -> Client:
        lock = await self._get_lock(session_str)
        async with lock:
            c = self._clients.get(session_str)
            if c and c.is_connected:
                return c
            if c:
                try: await c.stop()
                except Exception: pass
                self._clients.pop(session_str, None)
            name = f"acc_{abs(hash(session_str)) % 10**10}"
            client = Client(
                name=name, api_id=API_ID, api_hash=API_HASH,
                session_string=session_str, 
                in_memory=False,  # 🔴 FIXED: Save peer cache to disk
                no_updates=False
            )
            await client.start()
            self._clients[session_str] = client
            return client

    async def stop(self, session_str: str):
        c = self._clients.pop(session_str, None)
        if c:
            try: await c.stop()
            except Exception: pass

    async def stop_all(self):
        for s in list(self._clients.keys()):
            await self.stop(s)

client_manager = ClientManager()

# ==================== DATABASE ====================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, plan TEXT DEFAULT 'free')""")
    c.execute("""CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, phone TEXT,
        session TEXT, name TEXT, active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, text TEXT, image TEXT,
        from_chat_id INTEGER, message_id INTEGER, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS target_ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, text TEXT, image TEXT,
        from_chat_id INTEGER, message_id INTEGER, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id TEXT,
        chat_title TEXT, status TEXT DEFAULT 'stopped', last_run REAL DEFAULT 0,
        UNIQUE(owner_id, chat_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        owner_id INTEGER PRIMARY KEY,
        ad_status TEXT DEFAULT 'stopped',
        interval INTEGER DEFAULT 5,
        target_interval INTEGER DEFAULT 5,
        auto_reply INTEGER DEFAULT 0,
        auto_reply_text TEXT DEFAULT '',
        auto_join INTEGER DEFAULT 0,
        auto_join_links TEXT DEFAULT '',
        last_run REAL DEFAULT 0,
        target_last_run REAL DEFAULT 0
    )""")
    conn.commit()
    conn.close()

def db_get(query, params=()):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(query, params)
        return c.fetchone()
    finally:
        conn.close()

def db_all(query, params=()):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(query, params)
        return c.fetchall()
    finally:
        conn.close()

def db_run(query, params=()):
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
    finally:
        conn.close()

def db_run_many(statements: list):
    conn = get_db()
    try:
        c = conn.cursor()
        for q, p in statements:
            c.execute(q, p)
        conn.commit()
    finally:
        conn.close()

init_db()

def migrate_sessions():
    rows = db_all("SELECT id, session FROM accounts")
    migrated = 0
    for acc_id, sess in rows:
        if not sess: continue
        try:
            fernet.decrypt(sess.encode())
        except Exception:
            db_run("UPDATE accounts SET session=? WHERE id=?", (encrypt_session(sess), acc_id))
            migrated += 1
    if migrated:
        logger.info(f"🔐 Migrated {migrated} plaintext session(s) → encrypted.")

migrate_sessions()

# ==================== HELPERS ====================
def get_user_settings(user_id):
    row = db_get(
        "SELECT ad_status, interval, auto_reply, auto_reply_text, auto_join, "
        "auto_join_links, last_run, target_interval, target_last_run FROM settings WHERE owner_id=?",
        (user_id,)
    )
    if not row:
        db_run("INSERT OR IGNORE INTO settings (owner_id) VALUES (?)", (user_id,))
        return ("stopped", 5, 0, "", 0, "", 0.0, 5, 0.0)
    return row

def get_accounts(user_id, active_only=True, decrypt=True):
    q = "SELECT id, phone, session, name FROM accounts WHERE owner_id=?"
    if active_only:
        q += " AND active=1"
    rows = db_all(q, (user_id,))
    if not decrypt:
        return rows
    out = []
    for acc_id, phone, sess_enc, name in rows:
        try:
            out.append((acc_id, phone, decrypt_session(sess_enc), name))
        except Exception as e:
            logger.error(f"❌ Cannot decrypt session for account {acc_id}: {e}")
    return out

def get_plan(user_id):
    row = db_get("SELECT plan FROM users WHERE user_id=?", (user_id,))
    if not row:
        db_run("INSERT OR IGNORE INTO users (user_id, plan) VALUES (?, 'free')", (user_id,))
        return "free"
    return row[0]

def account_limit(plan):
    return 2 if plan == "free" else 20

async def notify_status(user_id, text):
    try:
        await tg_bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"notify_status to {user_id} failed: {type(e).__name__}: {e}")

def short_title(title: str, n: int = 24) -> str:
    return (title[:n] + "…") if len(title) > n else title

# ==================== CHAT RESOLVER ====================
async def resolve_chat_input(client: Client, raw: str):
    s = raw.strip().strip(",;")
    if not s: raise ValueError("empty")
    
    topic_id = None
    lower = s.lower()

    if "t.me/+" in lower or "/joinchat/" in lower:
        raise ValueError("private invite link — join manually first, or use numeric chat ID")

    if "/" in s and not s.startswith("http") and "t.me" not in s:
        parts = s.split("/")
        if len(parts) == 2 and parts[0].replace("-", "").isdigit() and parts[1].isdigit():
            s = parts[0]
            topic_id = int(parts[1])

    if "t.me/c/" in lower:
        path = s.split("t.me/c/")[1].split("?")[0].split("/")
        if len(path) >= 1:
            s = f"-100{path[0]}"
            if len(path) >= 2 and path[1].isdigit():
                topic_id = int(path[1])
                
    elif "t.me/" in lower:
        path = s.split("t.me/")[1].split("?")[0].split("/")
        if len(path) >= 1:
            s = path[0]
            if len(path) >= 2 and path[1].isdigit():
                topic_id = int(path[1])

    if s.startswith("@"):
        s = s[1:]

    if s.lstrip("-").isdigit():
        try:
            chat = await client.get_chat(int(s))
            return chat, topic_id
        except Exception as e:
            raise ValueError(f"cannot resolve ID {s} ({type(e).__name__})")

    try:
        chat = await client.get_chat(s)
        return chat, topic_id
    except Exception as e:
        raise ValueError(f"cannot resolve '{s}' ({type(e).__name__})")

# ==================== KEYBOARDS ====================
def main_keyboard(user_id):
    settings = get_user_settings(user_id)
    ad_status = settings[0]

    kb = []
    kb.append([InlineKeyboardButton("👤 ADD ACCOUNT", callback_data="add_account")])
    kb.append([
        InlineKeyboardButton("📢 SET MAIN AD", callback_data="set_ad"),
        InlineKeyboardButton("⏱ MAIN INTERVAL", callback_data="interval")
    ])

    if ad_status == "stopped":
        kb.append([InlineKeyboardButton("▶️ RUN ADS (ALL CHATS)", callback_data="run_ads_all")])
        kb.append([
            InlineKeyboardButton("👥 RUN ADS (GROUPS)", callback_data="run_ads_gp"),
            InlineKeyboardButton("📺 RUN ADS (CHANNELS)", callback_data="run_ads_ch")
        ])
    else:
        mode_name = ad_status.replace("running_", "").upper()
        kb.append([InlineKeyboardButton(f"🛑 STOP ADS ({mode_name})", callback_data="stop_ads")])

    kb.append([InlineKeyboardButton("🎯 TARGET ADS SYSTEM", callback_data="target_menu")])
    kb.append([
        InlineKeyboardButton("🤖 AUTO REPLY", callback_data="auto_reply"),
        InlineKeyboardButton("🚪 AUTO JOIN", callback_data="auto_join")
    ])
    kb.append([
        InlineKeyboardButton("💎 PREMIUM", callback_data="go_premium"),
        InlineKeyboardButton("ℹ️ ABOUT BOT", callback_data="about_bot")
    ])
    return InlineKeyboardMarkup(kb)

def target_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ SELECT TARGET CHATS", callback_data="select_targets")],
        [
            InlineKeyboardButton("📢 SET TARGET AD", callback_data="set_target_ad"),
            InlineKeyboardButton("⏱ TARGET INTERVAL", callback_data="target_interval")
        ],
        [InlineKeyboardButton("⚙️ MANAGE TARGETS", callback_data="manage_targets")],
        [InlineKeyboardButton("🔙 BACK TO DASHBOARD", callback_data="back")]
    ])

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO DASHBOARD", callback_data="back")]])

def target_back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK TO TARGET MENU", callback_data="target_menu")]])

# ==================== DASHBOARD ====================
def dashboard_text(user_id):
    st = get_user_settings(user_id)
    plan = get_plan(user_id)
    accounts = get_accounts(user_id, decrypt=False)

    status_map = {
        "running_all": "🟢 ALL CHATS",
        "running_groups": "🟢 GROUPS ONLY",
        "running_channels": "🟢 CHANNELS ONLY",
        "stopped": "🔴 STOPPED"
    }
    ad_status_text = status_map.get(st[0], "🔴 STOPPED")
    active_targets = db_all("SELECT id FROM targets WHERE owner_id=? AND status='running'", (user_id,))
    target_status_text = f"🟢 {len(active_targets)} ACTIVE" if active_targets else "🔴 STOPPED"

    return (
        "╭───────「 💎 PREMIUM DASHBOARD 」───────╮\n"
        "│  🚀 MANAGE • ADVERTISE • GROW\n"
        "╰────────────────────────────╯\n\n"
        f"{POWERED_BY_TEXT}\n\n"
        f"👤 <b>Hosted Accounts:</b> <code>{len(accounts)}/{account_limit(plan)}</code>\n"
        f"⚙️ <b>Service Status:</b> <code>{'Active' if accounts else 'Not Set'}</code>\n"
        f"📢 <b>Main Ads:</b> <code>{ad_status_text}</code>\n"
        f"🎯 <b>Target Ads:</b> <code>{target_status_text}</code>\n"
        f"⏱ <b>Main Interval:</b> <code>{st[1]} min</code>\n"
        f"⏱ <b>Target Interval:</b> <code>{st[7]} min</code>\n"
        f"💎 <b>Plan:</b> <code>{plan.upper()}</code>\n\n"
        f"╭────────────────────────────╮\n"
        f"  {DEVELOPER_TEXT}\n"
        "╰────────────────────────────╯"
    )

async def show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, text="✅ Saved!"):
    uid = update.effective_user.id
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text + "\n\n" + dashboard_text(uid),
        reply_markup=main_keyboard(uid),
        parse_mode="HTML"
    )

# ==================== TARGET PAGE RENDERER ====================
PER_PAGE = 8

def render_target_selection_page(uid):
    chats = user_chat_cache.get(uid, [])
    selected = target_selections.setdefault(uid, set())
    page = target_page.get(uid, 0)
    total_pages = max(1, (len(chats) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    target_page[uid] = page

    start = page * PER_PAGE
    end = start + PER_PAGE

    kb = []
    for offset, (c_id, c_title) in enumerate(chats[start:end]):
        idx = start + offset
        icon = "✅" if c_id in selected else "❌"
        kb.append([InlineKeyboardButton(f"{icon} {short_title(c_title)}", callback_data=f"tg_i_{idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"tg_p_{page-1}"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="tg_noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"tg_p_{page+1}"))
    if nav:
        kb.append(nav)

    kb.append([InlineKeyboardButton("➕ ADD VIA LINK / ID", callback_data="tg_man")])
    kb.append([InlineKeyboardButton("✅ SAVE SELECTION", callback_data="tg_sv")])
    kb.append([InlineKeyboardButton("🔙 CANCEL", callback_data="tg_bk")])

    text = (
        "🎯 <b>Select Target Chats</b>\n\n"
        f"Selected: <b>{len(selected)}</b> / {len(chats)}\n\n"
        "Tap to toggle ❌ → ✅.\n"
        "Use ➕ to add links. For forum topics, link will look like <code>t.me/username/12345</code>."
    )
    return text, InlineKeyboardMarkup(kb)

# ==================== COMMANDS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    get_user_settings(uid)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=dashboard_text(uid),
        reply_markup=main_keyboard(uid),
        parse_mode="HTML"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Commands</b>\n\n"
        "/start — Dashboard\n"
        "/cancel — Abort current input\n"
        "/accounts — Manage accounts\n"
        "/auto_reply_off — Disable auto-reply\n"
        "/help — This help",
        parse_mode="HTML"
    )

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    waiting_input.pop(uid, None)
    target_selections.pop(uid, None)
    user_chat_cache.pop(uid, None)
    target_page.pop(uid, None)

    if uid in pending_logins:
        c = pending_logins[uid].get("client")
        if c:
            try: await c.disconnect()
            except Exception: pass
        pending_logins.pop(uid, None)

    await update.message.reply_text("❌ Cancelled. Use /start to return.")

async def auto_reply_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_run("UPDATE settings SET auto_reply=0 WHERE owner_id=?", (update.effective_user.id,))
    await update.message.reply_text("🔴 Auto-reply OFF.")

async def accounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    accounts = get_accounts(uid, decrypt=False)
    if not accounts:
        await update.message.reply_text("No accounts. Use /start → Add Account.")
        return
    kb = []
    for idx, (acc_id, phone, _, name) in enumerate(accounts, 1):
        kb.append([InlineKeyboardButton(
            f"🗑 Remove #{idx} — {name or phone}",
            callback_data=f"rm_acc_{acc_id}"
        )])
    await update.message.reply_text(
        f"👤 <b>Your Accounts ({len(accounts)})</b>\n\nTap to remove:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML"
    )

# ==================== BUTTON HANDLER ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query: return
    try: await query.answer()
    except BadRequest: pass

    uid = query.from_user.id
    data = query.data
    settings = get_user_settings(uid)

    try:
        if data == "back":
            waiting_input.pop(uid, None)
            target_selections.pop(uid, None)
            user_chat_cache.pop(uid, None)
            target_page.pop(uid, None)
            await query.edit_message_text(dashboard_text(uid), reply_markup=main_keyboard(uid), parse_mode="HTML")

        elif data == "add_account":
            plan = get_plan(uid)
            accounts = get_accounts(uid, decrypt=False)
            if len(accounts) >= account_limit(plan):
                await query.edit_message_text(
                    f"⚠️ <b>Limit Reached</b> ({account_limit(plan)} accounts for {plan} plan).\n\nUpgrade: {PREMIUM_USERNAME}",
                    reply_markup=back_keyboard(), parse_mode="HTML"
                )
                return
            waiting_input[uid] = "phone"
            await query.edit_message_text(
                "📱 <b>Add Account</b>\n\nSend phone number in international format:\n<code>+919876543210</code>\n\nSend /cancel to abort.",
                reply_markup=back_keyboard(), parse_mode="HTML"
            )

        elif data.startswith("rm_acc_"):
            acc_id = int(data.replace("rm_acc_", ""))
            acc = db_get("SELECT owner_id, session FROM accounts WHERE id=?", (acc_id,))
            if not acc or acc[0] != uid:
                await query.answer("Not found.", show_alert=True)
                return
            try:
                await client_manager.stop(decrypt_session(acc[1]))
            except Exception: pass
            db_run("DELETE FROM accounts WHERE id=?", (acc_id,))
            await query.answer("✅ Account removed.", show_alert=True)
            remaining = get_accounts(uid, decrypt=False)
            if not remaining:
                await query.edit_message_text("✅ Account removed. No accounts left.")
            else:
                kb = []
                for idx, (a_id, phone, _, name) in enumerate(remaining, 1):
                    kb.append([InlineKeyboardButton(
                        f"🗑 Remove #{idx} — {name or phone}",
                        callback_data=f"rm_acc_{a_id}"
                    )])
                await query.edit_message_text(
                    f"👤 <b>Your Accounts ({len(remaining)})</b>",
                    reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
                )

        elif data == "set_ad":
            waiting_input[uid] = "ad_text"
            await query.edit_message_text(
                "📝 <b>Set Main Advertisement</b>\n\n"
                "Forward your ad from a channel/group, or send text/photo directly.\n\n"
                "/cancel to abort.",
                reply_markup=back_keyboard(), parse_mode="HTML"
            )

        elif data == "interval":
            waiting_input[uid] = "interval"
            await query.edit_message_text(
                f"⏱ <b>Main Interval</b>\n\nCurrent: <code>{settings[1]}</code> min\n\nSend new interval (1-120).",
                reply_markup=back_keyboard(), parse_mode="HTML"
            )

        elif data in ["run_ads_all", "run_ads_gp", "run_ads_ch"]:
            accounts = get_accounts(uid)
            if not accounts:
                await query.edit_message_text("⚠️ No accounts added.", reply_markup=back_keyboard())
                return
            ad = db_get("SELECT text, image, from_chat_id, message_id FROM ads WHERE owner_id=? ORDER BY id DESC LIMIT 1", (uid,))
            if not ad:
                await query.edit_message_text("⚠️ No advertisement set.", reply_markup=back_keyboard())
                return
            mode_map = {"run_ads_all": "running_all", "run_ads_gp": "running_groups", "run_ads_ch": "running_channels"}
            target_status = mode_map[data]
            db_run("UPDATE settings SET ad_status=?, last_run=0 WHERE owner_id=?", (target_status, uid))
            mode_name = target_status.replace("running_", "").upper()
            await query.answer(f"🚀 Started ({mode_name})!", show_alert=True)
            await query.edit_message_text(
                f"🚀 <b>Main Ads Started ({mode_name})</b>\n\nLive progress will arrive as messages.",
                reply_markup=main_keyboard(uid), parse_mode="HTML"
            )
            asyncio.create_task(trigger_main_run(uid, target_status))

        elif data == "stop_ads":
            db_run("UPDATE settings SET ad_status='stopped' WHERE owner_id=?", (uid,))
            for acc in get_accounts(uid):
                key = f"main_{acc[2]}"
                t = active_ad_tasks.get(key)
                if t and not t.done(): t.cancel()
            await query.answer("🛑 Stopped!", show_alert=True)
            await query.edit_message_text("🛑 <b>Main Ads Stopped</b>", reply_markup=main_keyboard(uid), parse_mode="HTML")

        elif data == "target_menu":
            waiting_input.pop(uid, None)
            await query.edit_message_text(
                "🎯 <b>TARGET ADS SYSTEM</b>\n\nSelect specific groups/channels.",
                reply_markup=target_main_keyboard(), parse_mode="HTML"
            )

        elif data == "target_interval":
            waiting_input[uid] = "target_interval"
            await query.edit_message_text(
                f"⏱ <b>Target Interval</b>\n\nCurrent: <code>{settings[7]}</code> min\n\nSend new interval (1-120).",
                reply_markup=target_back_keyboard(), parse_mode="HTML"
            )

        elif data == "set_target_ad":
            waiting_input[uid] = "target_ad_text"
            await query.edit_message_text(
                "📝 <b>Set TARGET Advertisement</b>\n\nForward the ad.\n/cancel to abort.",
                reply_markup=target_back_keyboard(), parse_mode="HTML"
            )

        elif data == "select_targets":
            accounts = get_accounts(uid)
            if not accounts:
                await query.edit_message_text("⚠️ Add an account first.", reply_markup=target_back_keyboard())
                return
            await query.edit_message_text("⏳ <b>Loading chats…</b>", parse_mode="HTML")

            try:
                client = await client_manager.get(accounts[0][2])
                chats = []
                async for dialog in client.get_dialogs(limit=None):
                    if dialog.chat and dialog.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
                        title = dialog.chat.title or dialog.chat.first_name or "Unknown"
                        chats.append((str(dialog.chat.id), title))
            except Exception as e:
                logger.error(f"select_targets fetch error: {type(e).__name__}: {e}")
                await query.edit_message_text(
                    f"⚠️ <b>Could not load chats</b>\n\n{type(e).__name__}\nTry /start again.",
                    reply_markup=target_back_keyboard(), parse_mode="HTML"
                )
                return

            if not chats:
                await query.edit_message_text(
                    "⚠️ No groups/channels found.\n\nUse ➕ Add via link to add manually.",
                    reply_markup=target_back_keyboard()
                )
                return

            user_chat_cache[uid] = chats
            existing = {r[1] for r in db_all("SELECT id, chat_id FROM targets WHERE owner_id=?", (uid,))}
            target_selections[uid] = set(existing) & {c[0] for c in chats}
            target_page[uid] = 0

            text_r, kb = render_target_selection_page(uid)
            await query.edit_message_text(text_r, reply_markup=kb, parse_mode="HTML")

        elif data == "tg_noop":
            pass

        elif data.startswith("tg_i_"):
            if uid not in user_chat_cache:
                await query.answer("Session expired — restart selection.", show_alert=True)
                return
            try: idx = int(data.replace("tg_i_", ""))
            except ValueError: return
            chats = user_chat_cache[uid]
            if idx < 0 or idx >= len(chats):
                await query.answer("Invalid.", show_alert=True); return
            c_id = chats[idx][0]
            sel = target_selections.setdefault(uid, set())
            if c_id in sel: sel.remove(c_id)
            else: sel.add(c_id)
            text_r, kb = render_target_selection_page(uid)
            await query.edit_message_text(text_r, reply_markup=kb, parse_mode="HTML")

        elif data.startswith("tg_p_"):
            if uid not in user_chat_cache:
                await query.answer("Session expired.", show_alert=True); return
            try: target_page[uid] = int(data.replace("tg_p_", ""))
            except ValueError: return
            text_r, kb = render_target_selection_page(uid)
            await query.edit_message_text(text_r, reply_markup=kb, parse_mode="HTML")

        elif data == "tg_man":
            if uid not in user_chat_cache:
                await query.answer("Restart selection first.", show_alert=True); return
            waiting_input[uid] = "manual_chats"
            await query.edit_message_text(
                "🔗 <b>Add Chats Manually</b>\n\n"
                "Send links/IDs separated by spaces:\n\n"
                "🔹 <code>t.me/username</code>\n"
                "🔹 <code>t.me/username/12345</code> <i>(Topic inside Forum)</i>\n"
                "🔹 <code>-1001234567890/45</code> <i>(Manual Chat/Topic ID)</i>\n\n"
                "Or send /cancel.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 CANCEL", callback_data="tg_bk")]]),
                parse_mode="HTML"
            )

        elif data == "tg_sv":
            if uid not in target_selections or not target_selections[uid]:
                await query.edit_message_text("⚠️ No chats selected.", reply_markup=target_back_keyboard())
                return
            chats = dict(user_chat_cache.get(uid, []))
            selected = target_selections[uid]
            existing = {r[1]: r[0] for r in db_all("SELECT id, chat_id FROM targets WHERE owner_id=?", (uid,))}

            stmts = []
            for c_id in selected:
                title = chats.get(c_id, "Unknown")
                if c_id in existing:
                    stmts.append(("UPDATE targets SET chat_title=? WHERE id=?", (title, existing[c_id])))
                else:
                    stmts.append(("INSERT OR IGNORE INTO targets (owner_id, chat_id, chat_title) VALUES (?, ?, ?)",
                                  (uid, c_id, title)))
            for c_id, t_id in existing.items():
                if c_id not in selected:
                    stmts.append(("DELETE FROM targets WHERE id=?", (t_id,)))
            if stmts: db_run_many(stmts)

            target_selections.pop(uid, None)
            user_chat_cache.pop(uid, None)
            target_page.pop(uid, None)

            await query.answer("✅ Targets Saved!", show_alert=True)
            await query.edit_message_text("✅ <b>Targets saved successfully!</b>", reply_markup=target_main_keyboard(), parse_mode="HTML")

        elif data == "tg_bk":
            waiting_input.pop(uid, None)
            target_selections.pop(uid, None)
            user_chat_cache.pop(uid, None)
            target_page.pop(uid, None)
            await query.edit_message_text("🎯 <b>Target Menu</b>", reply_markup=target_main_keyboard(), parse_mode="HTML")

        elif data == "manage_targets":
            targets = db_all("SELECT id, chat_id, chat_title, status FROM targets WHERE owner_id=?", (uid,))
            if not targets:
                await query.edit_message_text("⚠️ No target chats.", reply_markup=target_back_keyboard())
                return
            kb = []
            for t_id, c_id, c_title, status in targets:
                icon = "🟢" if status == "running" else "🔴"
                kb.append([InlineKeyboardButton(f"{icon} {short_title(c_title, 18)}", callback_data=f"manage_single_{t_id}")])
            kb.append([InlineKeyboardButton("▶️ START ALL", callback_data="start_all_targets")])
            kb.append([InlineKeyboardButton("🛑 STOP ALL", callback_data="stop_all_targets")])
            kb.append([InlineKeyboardButton("🔙 BACK", callback_data="target_menu")])
            await query.edit_message_text("⚙️ <b>Manage Target Chats</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

        elif data.startswith("manage_single_"):
            t_id = data.replace("manage_single_", "")
            row = db_get("SELECT chat_title, status FROM targets WHERE id=? AND owner_id=?", (t_id, uid))
            if not row:
                await query.answer("Not found.", show_alert=True); return
            c_title, status = row
            status_text = "🟢 RUNNING" if status == "running" else "🔴 STOPPED"
            kb = []
            if status == "stopped":
                kb.append([InlineKeyboardButton("▶️ START", callback_data=f"start_single_{t_id}")])
            else:
                kb.append([InlineKeyboardButton("🛑 STOP", callback_data=f"stop_single_{t_id}")])
            kb.append([InlineKeyboardButton("🗑 REMOVE", callback_data=f"del_single_{t_id}")])
            kb.append([InlineKeyboardButton("🔙 BACK", callback_data="manage_targets")])
            await query.edit_message_text(
                f"⚙️ <b>Target</b>\n\nChat: <code>{c_title}</code>\nStatus: <b>{status_text}</b>",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
            )

        elif data.startswith("start_single_"):
            t_id = data.replace("start_single_", "")
            db_run("UPDATE targets SET status='running', last_run=0 WHERE id=? AND owner_id=?", (t_id, uid))
            await query.answer("✅ Started!", show_alert=True)
            row = db_get("SELECT chat_title FROM targets WHERE id=?", (t_id,))
            kb = [
                [InlineKeyboardButton("🛑 STOP", callback_data=f"stop_single_{t_id}")],
                [InlineKeyboardButton("🗑 REMOVE", callback_data=f"del_single_{t_id}")],
                [InlineKeyboardButton("🔙 BACK", callback_data="manage_targets")]
            ]
            await query.edit_message_text(
                f"⚙️ <b>Target:</b> {row[0]}\nStatus: <b>🟢 RUNNING</b>",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
            )
            asyncio.create_task(trigger_target_run(uid))

        elif data.startswith("stop_single_"):
            t_id = data.replace("stop_single_", "")
            db_run("UPDATE targets SET status='stopped' WHERE id=? AND owner_id=?", (t_id, uid))
            await query.answer("🛑 Stopped!", show_alert=True)
            row = db_get("SELECT chat_title FROM targets WHERE id=?", (t_id,))
            kb = [
                [InlineKeyboardButton("▶️ START", callback_data=f"start_single_{t_id}")],
                [InlineKeyboardButton("🗑 REMOVE", callback_data=f"del_single_{t_id}")],
                [InlineKeyboardButton("🔙 BACK", callback_data="manage_targets")]
            ]
            await query.edit_message_text(
                f"⚙️ <b>Target:</b> {row[0]}\nStatus: <b>🔴 STOPPED</b>",
                reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
            )
            for acc in get_accounts(uid):
                key = f"tgt_{acc[2]}"
                t = active_target_tasks.get(key)
                if t and not t.done(): t.cancel()

        elif data.startswith("del_single_"):
            t_id = data.replace("del_single_", "")
            db_run("DELETE FROM targets WHERE id=? AND owner_id=?", (t_id, uid))
            await query.answer("🗑 Removed", show_alert=True)
            targets = db_all("SELECT id, chat_id, chat_title, status FROM targets WHERE owner_id=?", (uid,))
            if not targets:
                await query.edit_message_text("⚠️ No targets left.", reply_markup=target_back_keyboard())
                return
            kb = []
            for t_id2, _, c_title2, status2 in targets:
                icon = "🟢" if status2 == "running" else "🔴"
                kb.append([InlineKeyboardButton(f"{icon} {short_title(c_title2, 18)}", callback_data=f"manage_single_{t_id2}")])
            kb.append([InlineKeyboardButton("▶️ START ALL", callback_data="start_all_targets")])
            kb.append([InlineKeyboardButton("🛑 STOP ALL", callback_data="stop_all_targets")])
            kb.append([InlineKeyboardButton("🔙 BACK", callback_data="target_menu")])
            await query.edit_message_text("⚙️ <b>Manage Target Chats</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

        elif data == "start_all_targets":
            db_run("UPDATE targets SET status='running', last_run=0 WHERE owner_id=?", (uid,))
            await query.answer("✅ All Started!", show_alert=True)
            asyncio.create_task(trigger_target_run(uid))

        elif data == "stop_all_targets":
            db_run("UPDATE targets SET status='stopped' WHERE owner_id=?", (uid,))
            for acc in get_accounts(uid):
                key = f"tgt_{acc[2]}"
                t = active_target_tasks.get(key)
                if t and not t.done(): t.cancel()
            await query.answer("🛑 All Stopped!", show_alert=True)

        elif data == "auto_reply":
            status = "🟢 ON" if settings[2] else "🔴 OFF"
            txt = settings[3] or "(not set)"
            await query.edit_message_text(
                f"🤖 <b>Auto Reply — {status}</b>\n\nCurrent: <code>{txt}</code>\n\nSend new text to enable.\n/cancel to abort.",
                reply_markup=back_keyboard(), parse_mode="HTML"
            )
            waiting_input[uid] = "auto_reply"

        elif data == "auto_join":
            status = "🟢 ON" if settings[4] else "🔴 OFF"
            await query.edit_message_text(
                f"🚪 <b>Auto Join — {status}</b>\n\nSend links (space separated).\n/cancel to abort.",
                reply_markup=back_keyboard(), parse_mode="HTML"
            )
            waiting_input[uid] = "auto_join"

        elif data == "go_premium":
            await query.edit_message_text(f"💎 <b>Go Premium</b>\n\nContact {PREMIUM_USERNAME}", reply_markup=back_keyboard(), parse_mode="HTML")

        elif data == "about_bot":
            await query.edit_message_text(f"ℹ️ <b>AdsBot</b>\n\nPowered by {ABOUT_POWERED_USERNAME}", reply_markup=back_keyboard(), parse_mode="HTML")

    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"BadRequest in button: {e}")
    except Exception as e:
        logger.error(f"button_handler error: {type(e).__name__}: {e}")

# ==================== INPUT HANDLER ====================
async def input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    action = waiting_input.get(uid)
    if not action: return
    text = (update.message.text or "").strip()

    if action == "phone":
        if not text.startswith("+"):
            await update.message.reply_text("⚠️ Phone must start with +. Try again or /cancel.")
            return
        try:
            client = Client(
                name=f"login_{uid}_{int(time.time())}",
                api_id=API_ID, api_hash=API_HASH, in_memory=False
            )
            await client.connect()
            sent = await client.send_code(text)
            pending_logins[uid] = {
                "client": client, "phone": text,
                "hash": sent.phone_code_hash,
                "created_at": time.time()
            }
            waiting_input[uid] = "code"
            await update.message.reply_text("✅ Code sent. Send the login code or /cancel.")
        except FloodWait as e:
            waiting_input.pop(uid, None)
            hours = e.value // 3600
            mins = (e.value % 3600) // 60
            s = f"{hours}h {mins}m" if hours else f"{mins}m"
            await update.message.reply_text(f"⏳ FloodWait: retry after {s}.")
        except Exception as e:
            waiting_input.pop(uid, None)
            await update.message.reply_text(f"❌ Error: {type(e).__name__}: {e}")
        return

    if action == "code":
        state = pending_logins.get(uid)
        if not state:
            waiting_input.pop(uid, None); return
        try:
            await state["client"].sign_in(state["phone"], state["hash"], text)
        except SessionPasswordNeeded:
            waiting_input[uid] = "password"
            await update.message.reply_text("🔐 2FA password required. Send or /cancel.")
            return
        except (PhoneCodeInvalid, PhoneCodeExpired) as e:
            await update.message.reply_text(f"❌ {type(e).__name__}. Try again or /cancel.")
            return
        except Exception as e:
            await update.message.reply_text(f"❌ Sign-in error: {type(e).__name__}: {e}")
            return

        await _finalize_login(update, context, uid, state)
        return

    if action == "password":
        state = pending_logins.get(uid)
        if not state:
            waiting_input.pop(uid, None); return
        try:
            await state["client"].check_password(text)
        except Exception as e:
            await update.message.reply_text(f"❌ 2FA failed: {type(e).__name__}")
            return
        await _finalize_login(update, context, uid, state)
        return

    if action in ["ad_text", "target_ad_text"]:
        from_chat_id, msg_id = None, None
        
        f_origin = getattr(update.message, "forward_origin", None)
        if f_origin:
            if isinstance(f_origin, MessageOriginChannel):
                from_chat_id = f_origin.chat.id
                msg_id = f_origin.message_id
            elif isinstance(f_origin, MessageOriginChat):
                from_chat_id = f_origin.sender_chat.id
                msg_id = f_origin.message_id

        caption = update.message.caption or (text if text else None)
        photo_id = update.message.photo[-1].file_id if update.message.photo else None

        if not caption and not photo_id and not from_chat_id:
            await update.message.reply_text("⚠️ Empty ad. Forward or send text/photo.")
            return

        if action == "ad_text":
            db_run("INSERT INTO ads (owner_id, text, image, from_chat_id, message_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (uid, caption, photo_id, from_chat_id, msg_id, datetime.now().isoformat()))
            waiting_input.pop(uid, None)
            await show_dashboard(update, context, "✅ Main Ad Saved!")
        else:
            db_run("INSERT INTO target_ads (owner_id, text, image, from_chat_id, message_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (uid, caption, photo_id, from_chat_id, msg_id, datetime.now().isoformat()))
            waiting_input.pop(uid, None)
            await update.message.reply_text("✅ Target Ad Saved!")
            await context.bot.send_message(chat_id=uid, text="🎯 <b>Target Menu</b>",
                                           reply_markup=target_main_keyboard(), parse_mode="HTML")
        return

    if action in ("interval", "target_interval"):
        try:
            val = int(text)
            if not 1 <= val <= 120: raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ Send a number between 1-120 or /cancel.")
            return
        if action == "interval":
            db_run("UPDATE settings SET interval=? WHERE owner_id=?", (val, uid))
            waiting_input.pop(uid, None)
            await show_dashboard(update, context, f"✅ Main interval = <code>{val}</code> min.")
        else:
            db_run("UPDATE settings SET target_interval=? WHERE owner_id=?", (val, uid))
            waiting_input.pop(uid, None)
            await update.message.reply_text(f"✅ Target interval = <code>{val}</code> min.")
            await context.bot.send_message(chat_id=uid, text="🎯 <b>Target Menu</b>",
                                           reply_markup=target_main_keyboard(), parse_mode="HTML")
        return

    if action == "auto_reply":
        if not text:
            await update.message.reply_text("⚠️ Send some text or /cancel.")
            return
        db_run("UPDATE settings SET auto_reply=1, auto_reply_text=? WHERE owner_id=?", (text, uid))
        waiting_input.pop(uid, None)
        await show_dashboard(update, context, "✅ Auto Reply ON.")
        return

    if action == "auto_join":
        if not text:
            await update.message.reply_text("⚠️ Send links or /cancel.")
            return
        db_run("UPDATE settings SET auto_join=1, auto_join_links=? WHERE owner_id=?", (text, uid))
        waiting_input.pop(uid, None)
        await show_dashboard(update, context, "✅ Auto Join ON.")
        background_tasks.append(asyncio.create_task(perform_auto_join(uid)))
        return

    if action == "manual_chats":
        accounts = get_accounts(uid)
        if not accounts:
            await update.message.reply_text("⚠️ No accounts.")
            waiting_input.pop(uid, None); return
        try:
            client = await client_manager.get(accounts[0][2])
        except Exception as e:
            await update.message.reply_text(f"❌ Client error: {type(e).__name__}")
            waiting_input.pop(uid, None); return

        tokens = text.split()
        if not tokens:
            await update.message.reply_text("⚠️ Send at least one link/ID.")
            return

        cache = user_chat_cache.setdefault(uid, [])
        sel = target_selections.setdefault(uid, set())
        existing_ids = {c[0] for c in cache}

        added, errors = [], []
        for raw in tokens:
            try:
                chat, topic_id = await resolve_chat_input(client, raw)
                
                if chat.username:
                    base_id = f"@{chat.username}"
                else:
                    base_id = str(chat.id)

                if topic_id:
                    c_id = f"{base_id}/{topic_id}"
                    title_suffix = f" (Topic {topic_id})"
                else:
                    c_id = base_id
                    title_suffix = ""
                
                c_title = (chat.title or chat.first_name or chat.username or "Unknown") + title_suffix
                
                if c_id in existing_ids:
                    sel.add(c_id)
                    added.append(f"✅ {c_title} (already listed)")
                else:
                    cache.append((c_id, c_title))
                    sel.add(c_id)
                    existing_ids.add(c_id)
                    added.append(f"✅ {c_title}")
            except ValueError as ve:
                errors.append(f"❌ {raw} — {ve}")
            except Exception as e:
                errors.append(f"❌ {raw} — {type(e).__name__}")

        msg = ""
        if added: msg += "<b>Added:</b>\n" + "\n".join(added) + "\n\n"
        if errors: msg += "<b>Errors:</b>\n" + "\n".join(errors)
        await update.message.reply_text(msg or "Nothing added.", parse_mode="HTML")

        waiting_input.pop(uid, None)
        page_text, kb = render_target_selection_page(uid)
        await context.bot.send_message(chat_id=uid, text=page_text, reply_markup=kb, parse_mode="HTML")
        return

# ==================== LOGIN FINALIZER ====================
async def _finalize_login(update, context, uid, state):
    try:
        session_str = await state["client"].export_session_string()
        try:
            me = await state["client"].get_me()
            name = f"{me.first_name or ''} {me.last_name or ''}".strip() or state["phone"]
        except Exception:
            name = state["phone"]

        db_run("INSERT INTO accounts (owner_id, phone, session, name) VALUES (?, ?, ?, ?)",
               (uid, state["phone"], encrypt_session(session_str), name))
        try: await state["client"].disconnect()
        except Exception: pass

        pending_logins.pop(uid, None)
        waiting_input.pop(uid, None)
        await show_dashboard(update, context, f"✅ Account Added: <code>{name}</code>")
    except Exception as e:
        logger.error(f"finalize_login error: {type(e).__name__}: {e}")
        await update.message.reply_text(f"❌ Failed to save account: {type(e).__name__}")

# ==================== TRIGGERS ====================
async def trigger_target_run(owner_id):
    accounts = get_accounts(owner_id)
    if not accounts: return
    for acc in accounts:
        sess = acc[2]
        key = f"tgt_{sess}"
        t = active_target_tasks.get(key)
        if t and not t.done(): continue
        task = asyncio.create_task(process_target_batch(sess, owner_id))
        active_target_tasks[key] = task

async def trigger_main_run(owner_id, mode):
    for acc in get_accounts(owner_id):
        sess = acc[2]
        key = f"main_{sess}"
        t = active_ad_tasks.get(key)
        if t and not t.done(): continue
        task = asyncio.create_task(process_main_batch(sess, owner_id, mode))
        active_ad_tasks[key] = task

# ==================== SEND HELPER ====================
async def send_ad_to_chat(client: Client, chat_id_str, from_chat_id, msg_id, ad_text, ad_image, t_id=None) -> bool:
    topic_id = None
    if "/" in str(chat_id_str):
        parts = str(chat_id_str).split("/")
        chat_id = parts[0]
        topic_id = int(parts[1])
    else:
        chat_id = chat_id_str

    async def _send():
        kwargs = {}
        if topic_id:
            kwargs["reply_to_message_id"] = topic_id

        if from_chat_id and msg_id:
            await client.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=msg_id, **kwargs)
        elif ad_image:
            await client.send_photo(chat_id=chat_id, photo=ad_image, caption=ad_text or "", **kwargs)
        elif ad_text:
            await client.send_message(chat_id=chat_id, text=ad_text, **kwargs)
        else:
            raise ValueError("empty ad")

    try:
        await _send()
        return True
    except PeerIdInvalid:
        logger.warning(f"PeerIdInvalid for {chat_id} — resolving…")
        try:
            await client.get_chat(chat_id)
            await _send()
            return True
        except Exception as e:
            logger.error(f"❌ Could not resolve/send to {chat_id}: {type(e).__name__}")
            return False
    except (ChatWriteForbidden, ChannelPrivate, UserBannedInChannel):
        logger.warning(f"⛔ No write permission: {chat_id}")
        return False
    except FloodWait as e:
        logger.warning(f"⏳ FloodWait {e.value}s on {chat_id}")
        await asyncio.sleep(min(e.value, 60))
        return False
    except SlowmodeWait as e:
        logger.info(f"⏱ Slowmode {e.value}s on {chat_id}. Updating next run time.")
        if t_id:
            db_run("UPDATE targets SET last_run=? WHERE id=?", (time.time() + e.value, t_id))
        return False
    except PyroBadRequest as e:
        if "TOPIC_CLOSED" in str(e):
            logger.error(f"❌ TOPIC_CLOSED for {chat_id}. Skipping for 24 hours.")
            if t_id:
                db_run("UPDATE targets SET last_run=? WHERE id=?", (time.time() + 86400, t_id))
            return False
        if "CHANNEL_INVALID" in str(e):
            logger.warning(f"⚠️ CHANNEL_INVALID for {chat_id} — Retrying with fresh peer...")
            try:
                await client.get_chat(chat_id)
                await _send()
                return True
            except Exception:
                logger.error(f"❌ Invalid channel: {chat_id}")
                if t_id:
                    db_run("UPDATE targets SET last_run=? WHERE id=?", (time.time() + 86400, t_id))
                return False
        logger.error(f"❌ BadRequest to {chat_id}: {e}")
        return False
    except AuthKeyDuplicated:
        raise
    except Exception as e:
        logger.error(f"❌ Send failed to {chat_id}: {type(e).__name__}: {e}")
        return False

# ==================== ENGINES ====================
async def process_target_batch(session_str, owner_id):
    targets = db_all("SELECT id, chat_id, chat_title, last_run FROM targets WHERE owner_id=? AND status='running'", (owner_id,))
    if not targets: return

    ad = db_get("SELECT from_chat_id, message_id, text, image FROM target_ads WHERE owner_id=? ORDER BY id DESC LIMIT 1", (owner_id,))
    if not ad:
        await notify_status(owner_id, "⚠️ Target Ad not set.")
        return

    success, skipped, failed = 0, 0, 0
    try:
        client = await client_manager.get(session_str)
        await notify_status(owner_id, f"⏳ <b>Target Batch Started…</b>\nChecking {len(targets)} chats")

        try:
            async for _ in client.get_dialogs(limit=200): pass
        except Exception: pass

        for i, (t_id, chat_id, title, t_last_run) in enumerate(targets, 1):
            if time.time() < t_last_run:
                skipped += 1
                continue
                
            try:
                ok = await send_ad_to_chat(client, chat_id, ad[0], ad[1], ad[2], ad[3], t_id=t_id)
            except AuthKeyDuplicated:
                await notify_status(owner_id, "⚠️ <b>Session killed</b> (AuthKeyDuplicated). Re-login required.")
                return
                
            if ok:
                success += 1
                logger.info(f"✅ Target Ad → {title}")
            else:
                failed += 1
                logger.error(f"❌ Target Ad failed: {title}")
                
            if i % 5 == 0 or i == len(targets):
                await notify_status(owner_id, f"🚀 <b>Target Progress:</b> {i}/{len(targets)}\n✅ Sent: {success} | ⏭ Skipped: {skipped} | ❌ Failed: {failed}")
            await asyncio.sleep(2.0)

        await notify_status(owner_id, f"✅ <b>Target Batch Done!</b>\n📊 Sent: {success} | Skipped: {skipped} | Failed: {failed}")
    except asyncio.CancelledError:
        await notify_status(owner_id, "🛑 Target batch cancelled.")
        raise
    except Exception as e:
        logger.error(f"Target engine error: {type(e).__name__}: {e}")
        await notify_status(owner_id, f"⚠️ Target engine error: {type(e).__name__}")

async def process_main_batch(session_str, owner_id, mode):
    ad = db_get("SELECT from_chat_id, message_id, text, image FROM ads WHERE owner_id=? ORDER BY id DESC LIMIT 1", (owner_id,))
    if not ad:
        await notify_status(owner_id, "⚠️ Main Ad not set.")
        return

    sent, skipped, failed = 0, 0, 0
    try:
        client = await client_manager.get(session_str)
        my_id = client.me.id

        dialogs = []
        async for d in client.get_dialogs(limit=None):
            if not d.chat or d.chat.id == my_id: continue
            if mode == "running_groups" and d.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): continue
            if mode == "running_channels" and d.chat.type != ChatType.CHANNEL: continue
            if mode == "running_all" and d.chat.type == ChatType.BOT: continue
            dialogs.append(d)

        total = len(dialogs)
        if total == 0:
            await notify_status(owner_id, "⚠️ No chats found.")
            return

        await notify_status(owner_id, f"⏳ <b>Main Batch Started</b>\nMode: {mode.replace('running_','').upper()}\nChats: {total}")

        for i, d in enumerate(dialogs, 1):
            try:
                ok = await send_ad_to_chat(client, d.chat.id, ad[0], ad[1], ad[2], ad[3])
            except AuthKeyDuplicated:
                await notify_status(owner_id, "⚠️ <b>Session killed</b> (AuthKeyDuplicated). Re-login required.")
                return
            if ok: sent += 1
            else: failed += 1
            if i % 10 == 0 or i == total:
                await notify_status(owner_id, f"🚀 <b>Main Progress:</b> {i}/{total}\n✅ Sent: {sent} | ❌ Failed: {failed}")
            await asyncio.sleep(2.0)

        await notify_status(owner_id, f"✅ <b>Main Batch Done!</b>\n📊 Sent: {sent} | Failed: {failed}")
    except asyncio.CancelledError:
        await notify_status(owner_id, "🛑 Main batch cancelled.")
        raise
    except Exception as e:
        logger.error(f"Main engine error: {type(e).__name__}: {e}")
        await notify_status(owner_id, f"⚠️ Main engine error: {type(e).__name__}")

# ==================== SCHEDULER LOOP ====================
async def ad_loop():
    while True:
        try:
            running = db_all("SELECT owner_id, interval, last_run, ad_status FROM settings WHERE ad_status != 'stopped'")
            for owner_id, interval, last_run, ad_status in running:
                now = time.time()
                if now - last_run < interval * 60: continue
                db_run("UPDATE settings SET last_run=? WHERE owner_id=?", (now, owner_id))
                for acc in get_accounts(owner_id):
                    key = f"main_{acc[2]}"
                    t = active_ad_tasks.get(key)
                    if t and not t.done(): continue
                    active_ad_tasks[key] = asyncio.create_task(process_main_batch(acc[2], owner_id, ad_status))

            target_owners = db_all("SELECT DISTINCT owner_id FROM targets WHERE status='running'")
            for (owner_id,) in target_owners:
                st = get_user_settings(owner_id)
                now = time.time()
                if now - st[8] < st[7] * 60: continue
                db_run("UPDATE settings SET target_last_run=? WHERE owner_id=?", (now, owner_id))
                for acc in get_accounts(owner_id):
                    key = f"tgt_{acc[2]}"
                    t = active_target_tasks.get(key)
                    if t and not t.done(): continue
                    active_target_tasks[key] = asyncio.create_task(process_target_batch(acc[2], owner_id))
        except Exception as e:
            logger.error(f"ad_loop: {type(e).__name__}: {e}")
        await asyncio.sleep(10)

# ==================== AUTO JOIN ====================
async def perform_auto_join(owner_id):
    row = db_get("SELECT auto_join_links FROM settings WHERE owner_id=?", (owner_id,))
    if not row or not row[0]: return
    links = row[0].split()
    for acc in get_accounts(owner_id):
        try:
            client = await client_manager.get(acc[2])
        except Exception:
            continue
        for link in links:
            try:
                await client.join_chat(link)
                await asyncio.sleep(2)
            except FloodWait as e:
                await asyncio.sleep(min(e.value, 300))
            except Exception:
                pass

# ==================== AUTO REPLY ====================
async def auto_reply_loop():
    while True:
        try:
            rows = db_all("""
                SELECT a.owner_id, a.session
                FROM accounts a JOIN settings s ON a.owner_id = s.owner_id
                WHERE a.active=1 AND s.auto_reply=1
            """)
            for owner_id, sess_enc in rows:
                try:
                    sess = decrypt_session(sess_enc)
                except Exception:
                    continue
                key = (sess, owner_id)
                if key in auto_reply_listeners: continue
                try:
                    client = await client_manager.get(sess)
                    asyncio.create_task(listen_auto_reply(client, owner_id))
                    auto_reply_listeners.add(key)
                    logger.info(f"🤖 Auto-reply listener active for owner {owner_id}")
                except Exception as e:
                    logger.error(f"auto_reply setup: {type(e).__name__}")
        except Exception as e:
            logger.error(f"auto_reply_loop: {type(e).__name__}: {e}")
        await asyncio.sleep(30)

async def listen_auto_reply(client: Client, owner_id):
    @client.on_message(pyro_filters.private & ~pyro_filters.me)
    async def _reply(c, m):
        if not m.from_user: return
        row = db_get("SELECT auto_reply, auto_reply_text FROM settings WHERE owner_id=?", (owner_id,))
        if not row or not row[0] or not row[1]: return

        now = time.time()
        uk = (owner_id, m.from_user.id)
        if uk in last_replied_users and now - last_replied_users[uk] < 3600: return
        try:
            await m.reply_text(row[1])
            last_replied_users[uk] = now
            await asyncio.sleep(2)
        except Exception:
            pass

# ==================== CLEANUP LOOP ====================
async def cleanup_loop():
    while True:
        try:
            now = time.time()
            for uid in list(pending_logins.keys()):
                st = pending_logins[uid]
                if now - st.get("created_at", now) > 600:
                    c = st.get("client")
                    if c:
                        try: await c.disconnect()
                        except Exception: pass
                    pending_logins.pop(uid, None)
                    waiting_input.pop(uid, None)
                    try: await tg_bot.send_message(uid, "⏰ Login timed out. /start again.")
                    except Exception: pass
            for k in list(last_replied_users.keys()):
                if now - last_replied_users[k] > 7200:
                    last_replied_users.pop(k, None)
        except Exception as e:
            logger.error(f"cleanup_loop: {type(e).__name__}: {e}")
        await asyncio.sleep(60)

# ==================== SHUTDOWN ====================
async def post_shutdown(application):
    logger.info("Shutting down…")
    all_tasks = set(background_tasks)
    all_tasks.update(active_ad_tasks.values())
    all_tasks.update(active_target_tasks.values())
    for t in all_tasks:
        if not t.done(): t.cancel()
    if all_tasks:
        await asyncio.gather(*all_tasks, return_exceptions=True)
    for st in pending_logins.values():
        c = st.get("client")
        if c:
            try: await c.disconnect()
            except Exception: pass
    pending_logins.clear()
    await client_manager.stop_all()
    await asyncio.sleep(0.5)
    logger.info("Shutdown complete.")

# ==================== MAIN ====================
def main():
    # --- RENDER KE LIYE DUMMY WEB SERVER (PORT 8080) ---
    def run_dummy_server():
        port = int(os.environ.get("PORT", 8080))
        server = HTTPServer(('0.0.0.0', port), BaseHTTPRequestHandler)
        server.serve_forever()
    
    threading.Thread(target=run_dummy_server, daemon=True).start()
    # ---------------------------------------------------

    req = HTTPXRequest(connect_timeout=20.0, read_timeout=20.0, write_timeout=20.0, pool_timeout=30.0)
    req2 = HTTPXRequest(connect_timeout=20.0, read_timeout=20.0, write_timeout=20.0, pool_timeout=30.0)

    app = Application.builder().token(MASTER_BOT_TOKEN).request(req).get_updates_request(req2).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("accounts", accounts_cmd))
    app.add_handler(CommandHandler("auto_reply_off", auto_reply_off_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.FORWARDED) & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
        input_handler
    ))

    async def post_init(application):
        background_tasks.append(asyncio.create_task(ad_loop()))
        background_tasks.append(asyncio.create_task(auto_reply_loop()))
        background_tasks.append(asyncio.create_task(cleanup_loop()))
        logger.info("🚀 Background loops started.")

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    logger.info("Master bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()