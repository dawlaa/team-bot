"""البوستات (نقاط مضاعفة) + معدلات حسب الفويس/الرول + الأشخاص المحسوبين"""
import re
import time
from datetime import datetime
from typing import Optional, Union
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from core import (C_OK, bot, cfg_float, cfg_get, cfg_set, chan_label, classify_ids, db, emb, fmt_num,
                  get_tz, glog, manager_check, norm, say, to_float)

# ------------------------------------------------------------------ أيام وأوقات
DAY_NAMES_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
_DAY_ALIASES = {
    0: {"mon", "monday", "الاثنين", "اثنين", "الاتنين", "اتنين"},
    1: {"tue", "tuesday", "الثلاثاء", "ثلاثاء", "التلات", "تلات"},
    2: {"wed", "wednesday", "الاربعاء", "اربعاء", "الاربع", "اربع"},
    3: {"thu", "thursday", "الخميس", "خميس"},
    4: {"fri", "friday", "الجمعه", "الجمعة", "جمعه", "جمعة"},
    5: {"sat", "saturday", "السبت", "سبت"},
    6: {"sun", "sunday", "الاحد", "احد"},
}
_ALL_WORDS = {"", "all", "كل", "الكل", "كل يوم", "everyday"}


def parse_days(text: str) -> str:
    """'fri,sat' أو 'الجمعة السبت' أو 'all' → '4,5' (الاثنين=0)."""
    t = norm(text).strip().lower()
    if t in _ALL_WORDS:
        return ""
    out = set()
    for tok in re.split(r"[\s,،/\-]+", t):
        if not tok:
            continue
        key = tok.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        for d, names in _DAY_ALIASES.items():
            if key in names:
                out.add(d)
                break
        else:
            raise ValueError(f"يوم غير مفهوم: {tok}")
    return ",".join(str(d) for d in sorted(out))


def days_text(days: str) -> str:
    if not days:
        return "كل يوم"
    return "، ".join(DAY_NAMES_AR[int(d)] for d in days.split(",") if d != "")


def parse_hhmm(text: str) -> int:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", norm(text).strip())
    if not m:
        raise ValueError("الوقت لازم يكون بصيغة 18:00")
    h, mi = int(m[1]), int(m[2] or 0)
    if h == 24 and mi == 0:
        return 0
    if h > 23 or mi > 59:
        raise ValueError("الوقت غلط")
    return h * 60 + mi


def fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_date(text: str) -> str:
    t = norm(text).strip()
    datetime.strptime(t, "%Y-%m-%d")  # يرمي ValueError لو غلط
    return t


# ------------------------------------------------------------------ البوستات
def boost_matches(b, dt: datetime) -> bool:
    today = dt.date().isoformat()
    if b["date_from"] and today < b["date_from"]:
        return False
    if b["date_to"] and today > b["date_to"]:
        return False
    days = [int(x) for x in b["days"].split(",") if x != ""]
    mins = dt.hour * 60 + dt.minute
    s, e = b["start_min"], b["end_min"]
    wd = dt.weekday()
    if s == e:                       # يوم كامل
        return not days or wd in days
    if s < e:                        # نفس اليوم
        return s <= mins < e and (not days or wd in days)
    if mins >= s:                    # بيعدّي نص الليل: الجزء الأول
        return not days or wd in days
    if mins < e:                     # الجزء التاني (بدأ إمبارح)
        return not days or ((wd - 1) % 7) in days
    return False


def current_multiplier(gid: int, now_ts: Optional[float] = None) -> tuple[float, list[str]]:
    """أعلى بوست شغال دلوقتي (مش بيتضرب في بعض)."""
    now_ts = now_ts or time.time()
    dt = datetime.fromtimestamp(now_ts, get_tz(gid))
    best, names = 1.0, []
    if cfg_float(gid, "boost_manual_until", 0) > now_ts:
        m = cfg_float(gid, "boost_manual_mult", 1.0)
        if m > best:
            best, names = m, ["بوست يدوي"]
    for b in db.execute("SELECT * FROM boosts WHERE guild_id=? AND active=1", (gid,)):
        if boost_matches(b, dt) and b["multiplier"] > best:
            best, names = b["multiplier"], [b["name"]]
    return best, names


def create_boost(gid: int, name: str, mult: float, days: str = "all", start: str = "00:00",
                 end: str = "00:00", date_from: Optional[str] = None, date_to: Optional[str] = None) -> int:
    if mult <= 0:
        raise ValueError("المضاعف لازم يكون أكبر من صفر")
    d = parse_days(days)
    s, e = parse_hhmm(start), parse_hhmm(end)
    df = parse_date(date_from) if date_from else None
    dtt = parse_date(date_to) if date_to else None
    cur = db.execute(
        "INSERT INTO boosts(guild_id,name,multiplier,days,start_min,end_min,date_from,date_to) "
        "VALUES (?,?,?,?,?,?,?,?)", (gid, name[:60], mult, d, s, e, df, dtt))
    db.commit()
    return cur.lastrowid


def boost_lines(gid: int) -> list[str]:
    lines = []
    for b in db.execute("SELECT * FROM boosts WHERE guild_id=? ORDER BY id", (gid,)):
        when = "طول اليوم" if b["start_min"] == b["end_min"] else f"{fmt_min(b['start_min'])}–{fmt_min(b['end_min'])}"
        dates = f" | {b['date_from'] or '…'} → {b['date_to'] or '…'}" if (b["date_from"] or b["date_to"]) else ""
        state = "🟢" if b["active"] else "⏸️"
        lines.append(f"{state} `#{b['id']}` **{b['name']}** ×{b['multiplier']:g} — {days_text(b['days'])} {when}{dates}")
    return lines


def boost_embed(gid: int) -> discord.Embed:
    lines = boost_lines(gid)
    mult, names = current_multiplier(gid)
    now = f"×{mult:g} ({names[0]})" if names else "مفيش (×1)"
    e = emb("⚡ البوستات", "\n".join(lines) or "مفيش بوستات متضافة.", color=C_OK)
    e.add_field(name="الشغال دلوقتي", value=now)
    e.set_footer(text=f"التوقيت: {cfg_get(gid, 'tz', 'Africa/Cairo')} • لو أكتر من بوست اشتغلوا، بياخد الأعلى")
    return e


boost_group = app_commands.Group(name="boost", description="نقاط مضاعفة في أوقات/أيام معينة", guild_only=True)


@boost_group.command(name="add", description="ضيف بوست (مضاعف نقاط) في أوقات معينة")
@app_commands.describe(
    name="اسم البوست", multiplier="المضاعف (2 = ضعف النقاط)",
    start="بداية الوقت 18:00 (سيبه فاضي مع end = طول اليوم)", end="نهاية الوقت 23:00",
    days="الأيام: all أو fri,sat أو الجمعة,السبت", date_from="من تاريخ 2026-11-20 (اختياري)",
    date_to="لحد تاريخ 2026-11-25 (اختياري)")
@app_commands.check(manager_check)
async def boost_add(interaction: discord.Interaction, name: str,
                    multiplier: app_commands.Range[float, 0.1, 20.0], start: str = "00:00", end: str = "00:00",
                    days: str = "all", date_from: Optional[str] = None, date_to: Optional[str] = None):
    try:
        bid = create_boost(interaction.guild_id, name, multiplier, days, start, end, date_from, date_to)
    except ValueError as e:
        return await say(interaction, f"❌ {e}")
    await interaction.response.send_message(f"⚡ اتضاف بوست `#{bid}` **{name}** ×{multiplier:g}")
    await glog(interaction.guild, "settings", f"⚡ {interaction.user.mention} ضاف بوست #{bid} {name} ×{multiplier:g}")


@boost_group.command(name="list", description="اعرض البوستات")
@app_commands.check(manager_check)
async def boost_list(interaction: discord.Interaction):
    await say(interaction, embed=boost_embed(interaction.guild_id))


@boost_group.command(name="remove", description="احذف بوست")
@app_commands.describe(boost_id="رقم البوست")
@app_commands.check(manager_check)
async def boost_remove(interaction: discord.Interaction, boost_id: int):
    cur = db.execute("DELETE FROM boosts WHERE id=? AND guild_id=?", (boost_id, interaction.guild_id))
    db.commit()
    await say(interaction, "🗑️ اتحذف." if cur.rowcount else "مفيش بوست بالرقم ده.")


@boost_group.command(name="toggle", description="وقف/شغّل بوست من غير ما تحذفه")
@app_commands.check(manager_check)
async def boost_toggle(interaction: discord.Interaction, boost_id: int):
    cur = db.execute("UPDATE boosts SET active=1-active WHERE id=? AND guild_id=?", (boost_id, interaction.guild_id))
    db.commit()
    await say(interaction, "🔁 اتغيرت حالته." if cur.rowcount else "مفيش بوست بالرقم ده.")


def start_manual_boost(gid: int, mult: float, minutes: int) -> None:
    cfg_set(gid, "boost_manual_mult", mult)
    cfg_set(gid, "boost_manual_until", time.time() + minutes * 60)


@boost_group.command(name="now", description="بوست فوري لمدة معينة")
@app_commands.describe(multiplier="المضاعف", minutes="المدة بالدقايق")
@app_commands.check(manager_check)
async def boost_now(interaction: discord.Interaction, multiplier: app_commands.Range[float, 0.1, 20.0],
                    minutes: app_commands.Range[int, 1, 10080]):
    start_manual_boost(interaction.guild_id, multiplier, minutes)
    await interaction.response.send_message(f"⚡ بوست ×{multiplier:g} شغال لمدة **{minutes}** دقيقة.")
    await glog(interaction.guild, "settings", f"⚡ {interaction.user.mention} شغّل بوست فوري ×{multiplier:g} لـ {minutes} د")


@boost_group.command(name="end_now", description="وقّف البوست الفوري")
@app_commands.check(manager_check)
async def boost_end_now(interaction: discord.Interaction):
    cfg_set(interaction.guild_id, "boost_manual_until", None)
    await say(interaction, "⏹️ اتوقف البوست الفوري.")


bot.tree.add_command(boost_group)

# ------------------------------------------------------------------ معدلات حسب الفويس/الرول
def load_rates(gid: int):
    ch = {r["channel_id"]: r["rate"] for r in db.execute(
        "SELECT channel_id, rate FROM channel_rates WHERE guild_id=?", (gid,))}
    ro = {r["role_id"]: r["rate"] for r in db.execute(
        "SELECT role_id, rate FROM role_rates WHERE guild_id=?", (gid,))}
    return ch, ro


def rate_for(member: discord.Member, ch, ch_rates: dict, role_rates: dict, base: float) -> float:
    """الأولوية: معدل الفويس (أو الكاتيجوري) ثم معدل الرول (الأعلى) ثم المعدل العام."""
    r = ch_rates.get(ch.id)
    if r is None and getattr(ch, "category_id", None):
        r = ch_rates.get(ch.category_id)
    if r is not None:
        return r
    best = None
    for role in member.roles:
        v = role_rates.get(role.id)
        if v is not None and (best is None or v > best):
            best = v
    return best if best is not None else base


def set_channel_rate(guild: discord.Guild, text: str, rate: float) -> str:
    ok, bad = classify_ids(guild, text)
    for cid, _ in ok:
        db.execute(
            "INSERT INTO channel_rates(guild_id,channel_id,rate) VALUES (?,?,?) "
            "ON CONFLICT(guild_id,channel_id) DO UPDATE SET rate=excluded.rate", (guild.id, cid, rate))
    db.commit()
    parts = []
    if ok:
        parts.append(f"✅ {fmt_num(rate)} نقطة/ساعة في: " + "، ".join(chan_label(c) for _, c in ok))
    if bad:
        parts.append("❌ مش فويس/كاتيجوري: " + "، ".join(str(b) for b in bad))
    return "\n".join(parts) or "حدد فويس."


def rates_lines(guild: discord.Guild) -> list[str]:
    ch, ro = load_rates(guild.id)
    lines = []
    for cid, r in ch.items():
        c = guild.get_channel(cid)
        lines.append(f"🔊 {chan_label(c) if c else cid} → **{fmt_num(r)}**")
    for rid, r in ro.items():
        role = guild.get_role(rid)
        lines.append(f"🏷️ {role.mention if role else rid} → **{fmt_num(r)}**")
    return lines


def rates_embed(guild: discord.Guild) -> discord.Embed:
    from core import get_settings
    base = get_settings(guild.id)["rate"]
    e = emb("🎚️ المعدلات", "\n".join(rates_lines(guild)) or "مفيش معدلات خاصة.")
    e.add_field(name="المعدل العام", value=f"{fmt_num(base)} نقطة/ساعة")
    e.set_footer(text="الأولوية: الفويس ← الرول ← العام")
    return e


rates_group = app_commands.Group(name="rates", description="معدلات خاصة حسب الفويس أو الرول", guild_only=True)
ChannelArg = Union[discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel]


@rates_group.command(name="set_channel", description="معدل خاص لفويس أو كاتيجوري")
@app_commands.describe(rate="نقاط/ساعة", channel="الفويس أو الكاتيجوري", link_or_id="أو لينك/ID")
@app_commands.check(manager_check)
async def rates_set_channel(interaction: discord.Interaction, rate: app_commands.Range[float, 0.0, 100000.0],
                            channel: Optional[ChannelArg] = None, link_or_id: Optional[str] = None):
    text = f"{channel.id if channel else ''} {link_or_id or ''}"
    msg = set_channel_rate(interaction.guild, text, rate)
    await interaction.response.send_message(msg)
    await glog(interaction.guild, "settings", f"🎚️ {interaction.user.mention}: {msg}")


@rates_group.command(name="clear_channel", description="شيل المعدل الخاص بفويس")
@app_commands.check(manager_check)
async def rates_clear_channel(interaction: discord.Interaction, channel: Optional[ChannelArg] = None,
                              link_or_id: Optional[str] = None):
    ok, _ = classify_ids(interaction.guild, f"{channel.id if channel else ''} {link_or_id or ''}")
    n = 0
    for cid, _c in ok:
        n += db.execute("DELETE FROM channel_rates WHERE guild_id=? AND channel_id=?", (interaction.guild_id, cid)).rowcount
    db.commit()
    await say(interaction, f"🗑️ اتشال {n} معدل خاص.")


@rates_group.command(name="set_role", description="معدل خاص لرول")
@app_commands.describe(role="الرول", rate="نقاط/ساعة")
@app_commands.check(manager_check)
async def rates_set_role(interaction: discord.Interaction, role: discord.Role,
                         rate: app_commands.Range[float, 0.0, 100000.0]):
    db.execute("INSERT INTO role_rates(guild_id,role_id,rate) VALUES (?,?,?) "
               "ON CONFLICT(guild_id,role_id) DO UPDATE SET rate=excluded.rate", (interaction.guild_id, role.id, rate))
    db.commit()
    await interaction.response.send_message(f"✅ {role.mention} → **{fmt_num(rate)}** نقطة/ساعة",
                                            allowed_mentions=discord.AllowedMentions.none())
    await glog(interaction.guild, "settings", f"🎚️ {interaction.user.mention} ضبط معدل {role.mention} = {fmt_num(rate)}")


@rates_group.command(name="clear_role", description="شيل المعدل الخاص بالرول")
@app_commands.check(manager_check)
async def rates_clear_role(interaction: discord.Interaction, role: discord.Role):
    cur = db.execute("DELETE FROM role_rates WHERE guild_id=? AND role_id=?", (interaction.guild_id, role.id))
    db.commit()
    await say(interaction, "🗑️ اتشال." if cur.rowcount else "الرول ده ملوش معدل خاص.")


@rates_group.command(name="list", description="اعرض كل المعدلات")
@app_commands.check(manager_check)
async def rates_list(interaction: discord.Interaction):
    await say(interaction, embed=rates_embed(interaction.guild))


bot.tree.add_command(rates_group)

# ------------------------------------------------------------------ الأشخاص المحسوبين
def load_people(gid: int):
    mode = cfg_get(gid, "people_mode", "everyone")
    ppl: dict[int, dict] = {}
    for r in db.execute("SELECT user_id, blocked FROM people WHERE guild_id=?", (gid,)):
        ppl[r["user_id"]] = {"blocked": bool(r["blocked"]), "channels": set()}
    for r in db.execute("SELECT user_id, channel_id FROM people_channels WHERE guild_id=?", (gid,)):
        if r["user_id"] in ppl:
            ppl[r["user_id"]]["channels"].add(r["channel_id"])
    return mode, ppl


def member_counts(m: discord.Member, ch, mode: str, ppl: dict) -> bool:
    p = ppl.get(m.id)
    if p and p["blocked"]:
        return False
    if mode == "whitelist" and not p:
        return False
    if p and p["channels"]:
        if ch.id not in p["channels"] and not (getattr(ch, "category_id", None) in p["channels"]):
            return False
    return True


def set_person(guild: discord.Guild, uid: int, channels_text: str = "", blocked: bool = False) -> str:
    ok, bad = classify_ids(guild, channels_text) if channels_text.strip() else ([], [])
    db.execute("INSERT INTO people(guild_id,user_id,blocked) VALUES (?,?,?) "
               "ON CONFLICT(guild_id,user_id) DO UPDATE SET blocked=excluded.blocked", (guild.id, uid, int(blocked)))
    db.execute("DELETE FROM people_channels WHERE guild_id=? AND user_id=?", (guild.id, uid))
    for cid, _ in ok:
        db.execute("INSERT OR IGNORE INTO people_channels VALUES (?,?,?)", (guild.id, uid, cid))
    db.commit()
    where = "، ".join(chan_label(c) for _, c in ok) if ok else "كل الفويسات المتتبعة"
    msg = f"✅ <@{uid}> بيتحسب في: {where}"
    if bad:
        msg += "\n❌ تجاهلت (مش فويس): " + "، ".join(str(b) for b in bad)
    return msg


def people_embed(guild: discord.Guild) -> discord.Embed:
    mode, ppl = load_people(guild.id)
    lines = []
    for uid, p in ppl.items():
        if p["blocked"]:
            lines.append(f"⛔ <@{uid}> — مستبعد")
        elif p["channels"]:
            names = "، ".join(chan_label(guild.get_channel(c)) if guild.get_channel(c) else str(c) for c in p["channels"])
            lines.append(f"✅ <@{uid}> — في: {names}")
        else:
            lines.append(f"✅ <@{uid}> — كل الفويسات")
    mode_txt = "قايمة بيضاء (المضافين بس بيتحسبوا)" if mode == "whitelist" else "الكل بيتحسب (ماعدا المستبعدين)"
    e = emb("👥 الأشخاص", "\n".join(lines) or "مفيش أشخاص متضافين.")
    e.add_field(name="الوضع", value=mode_txt)
    return e


people_group = app_commands.Group(name="people", description="مين بيتحسبله نقاط وفي أنهي فويسات", guild_only=True)


@people_group.command(name="mode", description="الكل بيتحسب ولا المضافين بس")
@app_commands.choices(mode=[app_commands.Choice(name="الكل بيتحسب", value="everyone"),
                            app_commands.Choice(name="المضافين بس (قايمة بيضاء)", value="whitelist")])
@app_commands.check(manager_check)
async def people_mode(interaction: discord.Interaction, mode: str):
    cfg_set(interaction.guild_id, "people_mode", mode)
    await interaction.response.send_message("👥 " + ("هيتحسب المضافين بس." if mode == "whitelist" else "الكل هيتحسب."))
    await glog(interaction.guild, "settings", f"👥 {interaction.user.mention} غيّر وضع الأشخاص لـ {mode}")


@people_group.command(name="add", description="ضيف شخص (وحدد الفويسات اللي يتحسبله فيها)")
@app_commands.describe(user="العضو", channels="لينكات/IDs فويسات (سيبه فاضي = كل الفويسات)")
@app_commands.check(manager_check)
async def people_add(interaction: discord.Interaction, user: discord.Member, channels: str = ""):
    msg = set_person(interaction.guild, user.id, channels)
    await interaction.response.send_message(msg)
    await glog(interaction.guild, "settings", f"👥 {interaction.user.mention} ضاف/عدّل {user.mention}")


@people_group.command(name="block", description="استبعد شخص من الحساب")
@app_commands.check(manager_check)
async def people_block(interaction: discord.Interaction, user: discord.Member):
    set_person(interaction.guild, user.id, "", blocked=True)
    await interaction.response.send_message(f"⛔ {user.mention} اتستبعد من الحساب.")


@people_group.command(name="remove", description="شيل شخص من القايمة (يرجع للوضع العام)")
@app_commands.check(manager_check)
async def people_remove(interaction: discord.Interaction, user: discord.Member):
    db.execute("DELETE FROM people WHERE guild_id=? AND user_id=?", (interaction.guild_id, user.id))
    db.execute("DELETE FROM people_channels WHERE guild_id=? AND user_id=?", (interaction.guild_id, user.id))
    db.commit()
    await say(interaction, f"🗑️ اتشال {user.mention} من القايمة.", allowed_mentions=discord.AllowedMentions.none())


@people_group.command(name="list", description="اعرض الأشخاص والوضع الحالي")
@app_commands.check(manager_check)
async def people_list(interaction: discord.Interaction):
    await say(interaction, embed=people_embed(interaction.guild))


bot.tree.add_command(people_group)
