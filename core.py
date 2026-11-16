"""الأساس المشترك: قاعدة البيانات + البوت + أدوات مساعدة + نظام اللوج"""
import inspect
import io
import os
import re
import sqlite3
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "tracker.db")
DEFAULT_TZ = os.getenv("TZ_NAME", "Africa/Cairo")
MESSAGE_CONTENT = os.getenv("MESSAGE_CONTENT", "0") == "1"

C_MAIN, C_OK, C_WARN, C_BAD, C_GRAY = 0x5865F2, 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x95A5A6

# ------------------------------------------------------------------ قاعدة البيانات
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS settings (
        guild_id        INTEGER PRIMARY KEY,
        rate            REAL    NOT NULL DEFAULT 25,
        point_value     REAL    NOT NULL DEFAULT 0,
        session_active  INTEGER NOT NULL DEFAULT 0,
        session_started REAL,
        last_tick       REAL,
        min_members     INTEGER NOT NULL DEFAULT 1,
        ignore_deaf     INTEGER NOT NULL DEFAULT 1,
        ignore_mute     INTEGER NOT NULL DEFAULT 0,
        log_channel_id  INTEGER,
        manager_role_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS tracked_channels (
        guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    );
    CREATE TABLE IF NOT EXISTS users (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        seconds REAL NOT NULL DEFAULT 0,
        earned  REAL NOT NULL DEFAULT 0,
        adjust  REAL NOT NULL DEFAULT 0,
        spent   REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        by_id INTEGER NOT NULL, amount REAL NOT NULL, reason TEXT, ts REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS config (
        guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT,
        PRIMARY KEY (guild_id, key)
    );
    CREATE TABLE IF NOT EXISTS channel_rates (
        guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, rate REAL NOT NULL,
        PRIMARY KEY (guild_id, channel_id)
    );
    CREATE TABLE IF NOT EXISTS role_rates (
        guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL, rate REAL NOT NULL,
        PRIMARY KEY (guild_id, role_id)
    );
    CREATE TABLE IF NOT EXISTS boosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL,
        multiplier REAL NOT NULL, days TEXT NOT NULL DEFAULT '',
        start_min INTEGER NOT NULL DEFAULT 0, end_min INTEGER NOT NULL DEFAULT 0,
        date_from TEXT, date_to TEXT, active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS people (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, blocked INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS people_channels (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id, channel_id)
    );
    CREATE TABLE IF NOT EXISTS payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        by_id INTEGER NOT NULL, amount REAL NOT NULL, note TEXT, period INTEGER NOT NULL, ts REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS balances (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, carry REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS shop_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL,
        description TEXT, price REAL NOT NULL, stock INTEGER, role_id INTEGER,
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        item_id INTEGER, item_name TEXT, price REAL, status TEXT NOT NULL DEFAULT 'pending',
        handled_by INTEGER, ts REAL NOT NULL, message_id INTEGER, channel_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER NOT NULL, topic TEXT,
        status TEXT NOT NULL DEFAULT 'open', claimed_by INTEGER, created REAL NOT NULL,
        closed REAL, closed_by INTEGER, close_reason TEXT
    );
    CREATE TABLE IF NOT EXISTS warns (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        by_id INTEGER NOT NULL, reason TEXT, penalty REAL NOT NULL DEFAULT 0,
        ts REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1
    );
    """
)


def _add_column(table: str, coldef: str) -> None:
    """ترقية قواعد البيانات القديمة (النسخة الأولى من البوت)."""
    name = coldef.split()[0]
    cols = [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]
    if name not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")


_add_column("users", "spent REAL NOT NULL DEFAULT 0")
_add_column("orders", "delivery_info TEXT")
db.commit()

SETTING_KEYS = {
    "rate", "point_value", "session_active", "session_started", "last_tick",
    "min_members", "ignore_deaf", "ignore_mute", "log_channel_id", "manager_role_id",
}


def get_settings(guild_id: int) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO settings(guild_id) VALUES (?)", (guild_id,))
    return db.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,)).fetchone()


def set_settings(guild_id: int, **kw) -> None:
    get_settings(guild_id)
    for k in kw:
        if k not in SETTING_KEYS:
            raise ValueError(k)
    cols = ", ".join(f"{k}=?" for k in kw)
    db.execute(f"UPDATE settings SET {cols} WHERE guild_id=?", (*kw.values(), guild_id))
    db.commit()


def cfg_get(guild_id: int, key: str, default=None):
    r = db.execute("SELECT value FROM config WHERE guild_id=? AND key=?", (guild_id, key)).fetchone()
    return r["value"] if r and r["value"] is not None else default


def cfg_set(guild_id: int, key: str, value) -> None:
    if value is None:
        db.execute("DELETE FROM config WHERE guild_id=? AND key=?", (guild_id, key))
    else:
        db.execute(
            "INSERT INTO config(guild_id,key,value) VALUES (?,?,?) "
            "ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
            (guild_id, key, str(value)),
        )
    db.commit()


def cfg_int(guild_id: int, key: str, default: int = 0) -> int:
    v = cfg_get(guild_id, key)
    return int(float(v)) if v and "." in v else (int(v) if v and v.isdigit() else default)
def _old_cfg_int(guild_id: int, key: str, default: int = 0) -> int:
    try:
        return int(float(cfg_get(guild_id, key, default)))
    except (TypeError, ValueError):
        return default


def cfg_float(guild_id: int, key: str, default: float = 0.0) -> float:
    try:
        return float(cfg_get(guild_id, key, default))
    except (TypeError, ValueError):
        return default


def tracked_ids(guild_id: int) -> list[int]:
    rows = db.execute("SELECT channel_id FROM tracked_channels WHERE guild_id=?", (guild_id,)).fetchall()
    return [r["channel_id"] for r in rows]


def get_tz(guild_id: int) -> ZoneInfo:
    try:
        return ZoneInfo(cfg_get(guild_id, "tz", DEFAULT_TZ))
    except Exception:
        return ZoneInfo("UTC")


# ------------------------------------------------------------------ أدوات مساعدة
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")


def norm(text: Optional[str]) -> str:
    """بيحول الأرقام العربية لإنجليزي."""
    return (text or "").translate(_AR_DIGITS)


def to_float(text: str) -> float:
    return float(norm(text).replace(",", "").strip())


def to_int(text: str) -> int:
    return int(float(norm(text).replace(",", "").strip()))


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}س {sec % 3600 // 60}د"


def fmt_num(x: float) -> str:
    return f"{x:,.2f}"


def parse_ids(text: str) -> list[int]:
    """بيقبل IDs أو منشن أو لينكات، ومتعدد (مفصول بمسافة أو فاصلة)."""
    ids = []
    for token in re.split(r"[\s,،]+", norm(text).strip()):
        found = re.findall(r"\d{15,20}", token)
        if found:
            ids.append(int(found[-1]))
    return ids


VOICEISH = (discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel)


def classify_ids(guild: discord.Guild, text: str):
    """بيرجع (قائمة (id, channel) صالحة، قائمة ids غلط)."""
    ok, bad = [], []
    for cid in parse_ids(text):
        ch = guild.get_channel(cid)
        if isinstance(ch, VOICEISH):
            ok.append((cid, ch))
        else:
            bad.append(cid)
    return ok, bad


def chan_label(ch) -> str:
    return f"📁 {ch.name}" if isinstance(ch, discord.CategoryChannel) else ch.mention


async def member_from_text(guild: discord.Guild, text: str) -> Optional[discord.Member]:
    ids = parse_ids(text)
    if not ids:
        return None
    m = guild.get_member(ids[0])
    if m:
        return m
    try:
        return await guild.fetch_member(ids[0])
    except discord.HTTPException:
        return None


def emb(title: Optional[str] = None, desc: Optional[str] = None, color: int = C_MAIN) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=color, timestamp=discord.utils.utcnow())
    e.set_footer(text="Dev by DAWLA")
    return e


def is_manager(member, guild_id: int) -> bool:
    if not isinstance(member, discord.Member):
        return False
    p = member.guild_permissions
    if p.administrator or p.manage_guild:
        return True
    rid = get_settings(guild_id)["manager_role_id"]
    return bool(rid and any(r.id == rid for r in member.roles))


def manager_check(interaction: discord.Interaction) -> bool:
    if is_manager(interaction.user, interaction.guild_id):
        return True
    raise app_commands.CheckFailure("❌ الأمر ده للمسؤولين بس.")


async def say(interaction: discord.Interaction, content=None, *, ephemeral: bool = False, **kw):
    if interaction.response.is_done():
        return await interaction.followup.send(content, ephemeral=ephemeral, **kw)
    return await interaction.response.send_message(content, ephemeral=ephemeral, **kw)


async def notify_user(user_id: int, *, content=None, embed=None, file=None) -> bool:
    try:
        u = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await u.send(content=content, embed=embed, file=file)
        return True
    except (discord.HTTPException, AttributeError):
        return False


class Form(discord.ui.Modal):
    """مودال جاهز: fields = [(key, label, {kwargs لـ TextInput})]، والـ handler(interaction, values)."""

    def __init__(self, title: str, fields: list, handler):
        super().__init__(title=title[:45])
        self._handler = handler
        self._inputs: dict[str, discord.ui.TextInput] = {}
        for key, label, kw in fields:
            ti = discord.ui.TextInput(label=label[:45], **kw)
            self._inputs[key] = ti
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        await self._handler(interaction, {k: (v.value or "").strip() for k, v in self._inputs.items()})

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print("modal error:", repr(error))
        await say(interaction, "⚠️ حصل خطأ، راجع القيم وجرب تاني.")


SHORT, PARA = discord.TextStyle.short, discord.TextStyle.paragraph

# ------------------------------------------------------------------ اللوجات
LOG_CATS = {
    "points": "النقاط", "voice": "الفويس", "pay": "المدفوعات", "shop": "المتجر",
    "tickets": "التيكيتات", "warns": "الورنات", "settings": "الإعدادات",
    "members": "الأعضاء", "messages": "الرسائل", "server": "السيرفر",
}


async def glog(guild: discord.Guild, cat: str, text: Optional[str] = None, *,
               embed: Optional[discord.Embed] = None, file: Optional[discord.File] = None) -> None:
    """بيبعت لروم اللوج الخاص بالقسم (أو الروم الافتراضي)."""
    try:
        if cfg_get(guild.id, f"logoff:{cat}") == "1":
            return
        cid = cfg_get(guild.id, f"log:{cat}") or get_settings(guild.id)["log_channel_id"]
        if not cid:
            return
        ch = guild.get_channel(int(cid))
        if ch is None:
            return
        if embed is None:
            embed = emb(desc=text)
            embed.set_author(name=f"سجل {LOG_CATS.get(cat, cat)}")
        await ch.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:  # اللوج مايكسرش أي أمر
        print("log error:", cat, repr(e))


# ------------------------------------------------------------------ البوت
SETUP_HOOKS: list[Callable] = []


class TrackerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        if MESSAGE_CONTENT:
            intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

    async def setup_hook(self):
        for fn in SETUP_HOOKS:
            r = fn(self)
            if inspect.isawaitable(r):
                await r
    async def on_ready(self):
        print(f"شغال كـ {self.user} على {len(self.guilds)} سيرفر")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Dev by DAWLA"))
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"SYNCED TO: {guild.name}")


bot = TrackerBot()


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = str(error) or "❌ مش مسموحلك بالأمر ده."
    else:
        print("command error:", repr(error))
        msg = "⚠️ حصل خطأ غير متوقع، جرب تاني."
    try:
        await say(interaction, msg, ephemeral=isinstance(error, app_commands.CheckFailure))
    except discord.HTTPException:
        pass
