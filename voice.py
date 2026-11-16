"""حساب وقت الفويس: ستارت/ستوب، الفويسات المتتبعة، الإعدادات العامة"""
import time
from typing import Optional, Union
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from core import (C_GRAY, C_OK, SETUP_HOOKS, bot, cfg_get, cfg_set, chan_label, classify_ids, db, emb, fmt_dur,
                  fmt_num, get_settings, glog, manager_check, say, set_settings, tracked_ids)
from ledger import credit, period_no
from rates import (current_multiplier, load_people, load_rates, member_counts, rate_for)

TICK_SECONDS = 30   # كل كام ثانية البوت يحسب
MAX_GAP = 120       # أقصى وقت يتحسب في دورة واحدة (لو البوت وقع ماتحسبش وقت الوقوع)


# ------------------------------------------------------------------ المنطق
def voice_channels_for(guild: discord.Guild, ids: list[int]) -> list:
    out: dict[int, discord.abc.GuildChannel] = {}
    for cid in ids:
        ch = guild.get_channel(cid)
        if ch is None:
            continue
        if isinstance(ch, discord.CategoryChannel):
            for v in [*ch.voice_channels, *ch.stage_channels]:
                out[v.id] = v
        elif isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            out[ch.id] = ch
    if guild.afk_channel:
        out.pop(guild.afk_channel.id, None)
    return list(out.values())


def eligible_members(ch, s) -> list:
    res = []
    for m in ch.members:
        if m.bot or m.voice is None:
            continue
        vs = m.voice
        if s["ignore_deaf"] and (vs.self_deaf or vs.deaf):
            continue
        if s["ignore_mute"] and (vs.self_mute or vs.mute):
            continue
        res.append(m)
    return res


def live_count(guild: discord.Guild) -> int:
    """كام شخص بيتحسبله دلوقتي."""
    s = get_settings(guild.id)
    mode, ppl = load_people(guild.id)
    n = 0
    for ch in voice_channels_for(guild, tracked_ids(guild.id)):
        members = eligible_members(ch, s)
        if len(members) >= s["min_members"]:
            n += sum(1 for m in members if member_counts(m, ch, mode, ppl))
    return n


def tick_guild(guild: discord.Guild, now: float) -> None:
    s = get_settings(guild.id)
    if not s["session_active"]:
        return
    last = s["last_tick"] or now
    elapsed = max(0.0, min(now - last, MAX_GAP))
    if elapsed > 0:
        mult, _ = current_multiplier(guild.id, now)
        ch_rates, role_rates = load_rates(guild.id)
        mode, ppl = load_people(guild.id)
        for ch in voice_channels_for(guild, tracked_ids(guild.id)):
            members = eligible_members(ch, s)
            if len(members) < s["min_members"]:
                continue
            for m in members:
                if not member_counts(m, ch, mode, ppl):
                    continue
                rate = rate_for(m, ch, ch_rates, role_rates, s["rate"])
                credit(guild.id, m.id, elapsed, elapsed / 3600 * rate * mult)
    db.execute("UPDATE settings SET last_tick=? WHERE guild_id=?", (now, guild.id))
    db.commit()


@tasks.loop(seconds=TICK_SECONDS)
async def accrue():
    now = time.time()
    for guild in bot.guilds:
        try:
            tick_guild(guild, now)
        except Exception as e:  # ما نوقفش اللوب بسبب سيرفر واحد
            print("tick error:", guild.id, repr(e))


@accrue.before_loop
async def _wait():
    await bot.wait_until_ready()


SETUP_HOOKS.append(lambda b: accrue.start())


def do_start(guild: discord.Guild) -> tuple[bool, str]:
    s = get_settings(guild.id)
    if s["session_active"]:
        return False, "الحساب شغال بالفعل ✅"
    now = time.time()
    set_settings(guild.id, session_active=1, session_started=now, last_tick=now)
    txt = f"▶️ بدأ الحساب. المعدل: **{fmt_num(s['rate'])}** نقطة/ساعة."
    if not tracked_ids(guild.id):
        txt += "\n⚠️ مفيش فويسات متضافة لسه، استخدم `/channels add`."
    return True, txt


def do_stop(guild: discord.Guild) -> tuple[bool, str]:
    s = get_settings(guild.id)
    if not s["session_active"]:
        return False, "الحساب واقف أصلاً."
    tick_guild(guild, time.time())  # آخر حساب لحد اللحظة دي
    dur = time.time() - (s["session_started"] or time.time())
    set_settings(guild.id, session_active=0, session_started=None, last_tick=None)
    return True, f"⏹️ وقف الحساب. مدة الجلسة: **{fmt_dur(dur)}**."


def apply_rate(guild: discord.Guild, value: float) -> str:
    if get_settings(guild.id)["session_active"]:
        tick_guild(guild, time.time())  # اقفل الحساب القديم بالمعدل القديم
    set_settings(guild.id, rate=value)
    return f"⚙️ المعدل بقى **{fmt_num(value)}** نقطة/ساعة (من دلوقتي)."


def add_tracked(guild: discord.Guild, text: str) -> str:
    ok, bad = classify_ids(guild, text)
    for cid, _ in ok:
        db.execute("INSERT OR IGNORE INTO tracked_channels VALUES (?, ?)", (guild.id, cid))
    db.commit()
    parts = []
    if ok:
        parts.append("✅ اتضاف: " + "، ".join(chan_label(c) for _, c in ok))
    if bad:
        parts.append("❌ مش فويس/كاتيجوري في السيرفر ده: " + "، ".join(str(b) for b in bad))
    return "\n".join(parts) or "حدد فويس أو حط لينك/ID."


def remove_tracked(guild: discord.Guild, text: str) -> str:
    from core import parse_ids
    n = 0
    for cid in parse_ids(text):
        n += db.execute("DELETE FROM tracked_channels WHERE guild_id=? AND channel_id=?", (guild.id, cid)).rowcount
    db.commit()
    return f"🗑️ اتشال {n} من الفويسات المتتبعة."


def channels_lines(guild: discord.Guild) -> list[str]:
    lines = []
    for cid in tracked_ids(guild.id):
        ch = guild.get_channel(cid)
        if ch is None:
            lines.append(f"❓ `{cid}` (محذوف أو البوت مش شايفه)")
        elif isinstance(ch, discord.CategoryChannel):
            lines.append(f"📁 **{ch.name}** (الفويسات اللي جواها: {len(ch.voice_channels) + len(ch.stage_channels)})")
        else:
            lines.append(f"🔊 {ch.mention}")
    return lines


def status_embed(guild: discord.Guild) -> discord.Embed:
    s = get_settings(guild.id)
    chans = voice_channels_for(guild, tracked_ids(guild.id))
    mult, names = current_multiplier(guild.id)
    e = emb("📊 حالة البوت", color=C_OK if s["session_active"] else C_GRAY)
    e.add_field(name="الحالة", value=f"شغال منذ <t:{int(s['session_started'])}:R>" if s["session_active"] else "واقف")
    e.add_field(name="المعدل", value=f"{fmt_num(s['rate'])} نقطة/ساعة")
    e.add_field(name="البوست الحالي", value=f"×{mult:g} ({names[0]})" if names else "مفيش")
    e.add_field(name="قيمة النقطة", value=fmt_num(s["point_value"]))
    e.add_field(name="فويسات متتبعة", value=str(len(chans)))
    e.add_field(name="ناس بتتحسب دلوقتي", value=str(live_count(guild)))
    e.add_field(name="أقل عدد في الروم", value=str(s["min_members"]))
    e.add_field(name="رقم الفترة", value=str(period_no(guild.id)))
    return e


# ------------------------------------------------------------------ الأوامر
@bot.tree.command(name="start", description="ابدأ حساب النقاط من دلوقتي")
@app_commands.guild_only()
@app_commands.check(manager_check)
async def start_cmd(interaction: discord.Interaction):
    ok, txt = do_start(interaction.guild)
    await say(interaction, txt, ephemeral=not ok)
    if ok:
        await glog(interaction.guild, "settings", f"▶️ {interaction.user.mention} بدأ الحساب.")


@bot.tree.command(name="stop", description="وقّف حساب النقاط")
@app_commands.guild_only()
@app_commands.check(manager_check)
async def stop_cmd(interaction: discord.Interaction):
    ok, txt = do_stop(interaction.guild)
    await say(interaction, txt, ephemeral=not ok)
    if ok:
        await glog(interaction.guild, "settings", f"⏹️ {interaction.user.mention} وقّف الحساب.")


@bot.tree.command(name="status", description="حالة البوت والإعدادات الحالية")
@app_commands.guild_only()
@app_commands.check(manager_check)
async def status_cmd(interaction: discord.Interaction):
    await say(interaction, embed=status_embed(interaction.guild))


channels_group = app_commands.Group(name="channels", description="إدارة الفويسات المتتبعة", guild_only=True)
ChannelArg = Union[discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel]


def _text(channel, link_or_id) -> str:
    return f"{channel.id if channel else ''} {link_or_id or ''}"


@channels_group.command(name="add", description="ضيف فويس (أو كاتيجوري كاملة) للحساب")
@app_commands.describe(channel="اختار الفويس أو الكاتيجوري", link_or_id="أو حط لينك/ID (ممكن أكتر من واحد)")
@app_commands.check(manager_check)
async def ch_add(interaction: discord.Interaction, channel: Optional[ChannelArg] = None,
                 link_or_id: Optional[str] = None):
    msg = add_tracked(interaction.guild, _text(channel, link_or_id))
    await interaction.response.send_message(msg)
    if msg.startswith("✅"):
        await glog(interaction.guild, "settings", f"➕ {interaction.user.mention}\n{msg}")


@channels_group.command(name="remove", description="شيل فويس من الحساب")
@app_commands.describe(channel="اختار الفويس أو الكاتيجوري", link_or_id="أو حط لينك/ID")
@app_commands.check(manager_check)
async def ch_remove(interaction: discord.Interaction, channel: Optional[ChannelArg] = None,
                    link_or_id: Optional[str] = None):
    msg = remove_tracked(interaction.guild, _text(channel, link_or_id))
    await interaction.response.send_message(msg)
    await glog(interaction.guild, "settings", f"➖ {interaction.user.mention}: {msg}")


@channels_group.command(name="list", description="اعرض الفويسات المتتبعة")
@app_commands.check(manager_check)
async def ch_list(interaction: discord.Interaction):
    lines = channels_lines(interaction.guild)
    await say(interaction, "\n".join(lines) or "مفيش فويسات متضافة.")


bot.tree.add_command(channels_group)

settings_group = app_commands.Group(name="settings", description="إعدادات البوت", guild_only=True)


@settings_group.command(name="rate", description="عدد النقاط في الساعة (المعدل العام)")
@app_commands.describe(points_per_hour="نقاط/ساعة")
@app_commands.check(manager_check)
async def s_rate(interaction: discord.Interaction, points_per_hour: app_commands.Range[float, 0.0, 100000.0]):
    await interaction.response.send_message(apply_rate(interaction.guild, points_per_hour))
    await glog(interaction.guild, "settings", f"⚙️ {interaction.user.mention} غيّر المعدل لـ {fmt_num(points_per_hour)}/ساعة")


@settings_group.command(name="money", description="قيمة النقطة الواحدة بالفلوس")
@app_commands.describe(value="قيمة النقطة")
@app_commands.check(manager_check)
async def s_money(interaction: discord.Interaction, value: app_commands.Range[float, 0.0, 1000000.0]):
    set_settings(interaction.guild_id, point_value=value)
    await interaction.response.send_message(f"💰 قيمة النقطة بقت **{fmt_num(value)}**.")
    await glog(interaction.guild, "settings", f"💰 {interaction.user.mention} ضبط قيمة النقطة = {fmt_num(value)}")


@settings_group.command(name="min_members", description="أقل عدد ناس في الروم عشان الوقت يتحسب")
@app_commands.describe(count="1 = يتحسب حتى لو لوحده")
@app_commands.check(manager_check)
async def s_min(interaction: discord.Interaction, count: app_commands.Range[int, 1, 99]):
    set_settings(interaction.guild_id, min_members=count)
    await interaction.response.send_message(f"👥 الوقت هيتحسب بس لو في الروم **{count}** أو أكتر.")


@settings_group.command(name="ignore_deafened", description="مايحسبش اللي عامل ديفن")
@app_commands.check(manager_check)
async def s_deaf(interaction: discord.Interaction, enabled: bool):
    set_settings(interaction.guild_id, ignore_deaf=int(enabled))
    await interaction.response.send_message("🎧 " + ("اللي عامل ديفن مش هيتحسب." if enabled else "الديفن هيتحسب عادي."))


@settings_group.command(name="ignore_muted", description="مايحسبش اللي عامل ميوت")
@app_commands.check(manager_check)
async def s_mute(interaction: discord.Interaction, enabled: bool):
    set_settings(interaction.guild_id, ignore_mute=int(enabled))
    await interaction.response.send_message("🎙️ " + ("اللي عامل ميوت مش هيتحسب." if enabled else "الميوت هيتحسب عادي."))


@settings_group.command(name="log_channel", description="روم اللوج الافتراضي (سيبه فاضي للمسح)")
@app_commands.check(manager_check)
async def s_log(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    set_settings(interaction.guild_id, log_channel_id=channel.id if channel else None)
    await interaction.response.send_message(f"📝 روم اللوج الافتراضي: {channel.mention if channel else 'اتمسح'}")


@settings_group.command(name="manager_role", description="رول يقدر يتحكم في البوت (سيبه فاضي للمسح)")
@app_commands.check(manager_check)
async def s_role(interaction: discord.Interaction, role: Optional[discord.Role] = None):
    set_settings(interaction.guild_id, manager_role_id=role.id if role else None)
    await interaction.response.send_message(
        f"🛡️ رول التحكم: {role.mention if role else 'اتمسح (الأدمن بس)'}",
        allowed_mentions=discord.AllowedMentions.none())


@settings_group.command(name="timezone", description="المنطقة الزمنية للبوستات والتقارير (مثال Africa/Cairo)")
@app_commands.check(manager_check)
async def s_tz(interaction: discord.Interaction, name: str):
    try:
        ZoneInfo(name)
    except Exception:
        return await say(interaction, "❌ اسم المنطقة غلط. مثال: `Africa/Cairo` أو `Asia/Riyadh`.")
    cfg_set(interaction.guild_id, "tz", name)
    await interaction.response.send_message(f"🕒 المنطقة الزمنية: **{name}**")


bot.tree.add_command(settings_group)
