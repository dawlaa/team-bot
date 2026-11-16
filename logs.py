"""نظام اللوجات الشامل: روم لكل قسم + تسجيل أحداث الفويس والرسائل والأعضاء والسيرفر"""
import time
from typing import Optional

import discord
from discord import app_commands

from core import (C_BAD, C_GRAY, C_OK, C_WARN, LOG_CATS, MESSAGE_CONTENT, bot, cfg_get, cfg_set, emb, fmt_dur,
                  get_settings, glog, manager_check, say)

VOICE_JOINED: dict[tuple[int, int], float] = {}  # (guild, user) -> وقت الدخول (للمدة في اللوج)


def logs_embed(guild: discord.Guild) -> discord.Embed:
    default = get_settings(guild.id)["log_channel_id"]
    lines = []
    for key, label in LOG_CATS.items():
        cid = cfg_get(guild.id, f"log:{key}")
        off = cfg_get(guild.id, f"logoff:{key}") == "1"
        where = f"<#{cid}>" if cid else (f"<#{default}> (افتراضي)" if default else "—")
        lines.append(f"{'⏸️' if off else '🟢'} **{label}** (`{key}`) → {where}")
    return emb("📝 نظام اللوجات", "\n".join(lines))


def set_log_channel(gid: int, cat: str, channel_id: Optional[int]) -> None:
    cfg_set(gid, f"log:{cat}", channel_id)


logs_group = app_commands.Group(name="logs", description="نظام اللوجات", guild_only=True)
CAT_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in LOG_CATS.items()]


@logs_group.command(name="set", description="حدد روم لوج قسم معين")
@app_commands.choices(category=CAT_CHOICES)
@app_commands.check(manager_check)
async def logs_set(interaction: discord.Interaction, category: str, channel: discord.TextChannel):
    set_log_channel(interaction.guild_id, category, channel.id)
    await say(interaction, f"✅ لوج **{LOG_CATS[category]}** هيتبعت في {channel.mention}.")


@logs_group.command(name="set_all", description="كل الأقسام في روم واحد")
@app_commands.check(manager_check)
async def logs_set_all(interaction: discord.Interaction, channel: discord.TextChannel):
    for k in LOG_CATS:
        set_log_channel(interaction.guild_id, k, channel.id)
    await say(interaction, f"✅ كل اللوجات هتتبعت في {channel.mention}.")


@logs_group.command(name="clear", description="شيل روم لوج قسم (يرجع للافتراضي)")
@app_commands.choices(category=CAT_CHOICES)
@app_commands.check(manager_check)
async def logs_clear(interaction: discord.Interaction, category: str):
    set_log_channel(interaction.guild_id, category, None)
    await say(interaction, f"🗑️ اتمسح روم لوج **{LOG_CATS[category]}**.")


@logs_group.command(name="toggle", description="شغّل/وقّف لوج قسم")
@app_commands.choices(category=CAT_CHOICES)
@app_commands.check(manager_check)
async def logs_toggle(interaction: discord.Interaction, category: str, enabled: bool):
    cfg_set(interaction.guild_id, f"logoff:{category}", None if enabled else "1")
    await say(interaction, f"{'🟢 اشتغل' if enabled else '⏸️ اتوقف'} لوج **{LOG_CATS[category]}**.")


@logs_group.command(name="list", description="اعرض إعدادات اللوجات")
@app_commands.check(manager_check)
async def logs_list(interaction: discord.Interaction):
    await say(interaction, embed=logs_embed(interaction.guild))


bot.tree.add_command(logs_group)


# ------------------------------------------------------------------ تسجيل الأحداث
def _is_ticket(ch) -> bool:
    return bool(ch and getattr(ch, "name", "").startswith("ticket-"))


@bot.listen("on_ready")
async def _seed_voice():
    now = time.time()
    for g in bot.guilds:
        for vc in [*g.voice_channels, *g.stage_channels]:
            for m in vc.members:
                if not m.bot:
                    VOICE_JOINED.setdefault((g.id, m.id), now)


@bot.listen("on_voice_state_update")
async def _voice(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    g, key = member.guild, (member.guild.id, member.id)
    if before.channel is None and after.channel is not None:
        VOICE_JOINED[key] = time.time()
        await glog(g, "voice", f"🔊 {member.mention} دخل {after.channel.mention}")
    elif before.channel is not None and after.channel is None:
        t = VOICE_JOINED.pop(key, None)
        dur = f" — قعد {fmt_dur(time.time() - t)}" if t else ""
        await glog(g, "voice", f"🔈 {member.mention} خرج من {before.channel.mention}{dur}")
    elif before.channel != after.channel:
        await glog(g, "voice", f"🔀 {member.mention} اتنقل من {before.channel.mention} إلى {after.channel.mention}")
    elif before.mute != after.mute or before.deaf != after.deaf:
        what = []
        if before.mute != after.mute:
            what.append("ميوت سيرفر" if after.mute else "فك ميوت سيرفر")
        if before.deaf != after.deaf:
            what.append("ديفن سيرفر" if after.deaf else "فك ديفن سيرفر")
        await glog(g, "voice", f"🎙️ {member.mention}: {'، '.join(what)}")


@bot.listen("on_raw_message_delete")
async def _msg_delete(payload: discord.RawMessageDeleteEvent):
    if payload.guild_id is None:
        return
    g = bot.get_guild(payload.guild_id)
    if g is None:
        return
    ch = g.get_channel(payload.channel_id)
    if _is_ticket(ch):
        return
    where = ch.mention if ch else f"`{payload.channel_id}`"
    msg = payload.cached_message
    if msg is None:
        return await glog(g, "messages", embed=emb("🗑️ رسالة اتمسحت", f"في {where} (مش محفوظة عندي)", C_GRAY))
    if msg.author.bot:
        return
    e = emb("🗑️ رسالة اتمسحت", f"من {msg.author.mention} في {where}", C_BAD)
    if msg.content:
        e.add_field(name="المحتوى", value=msg.content[:1000], inline=False)
    elif not MESSAGE_CONTENT:
        e.set_footer(text="المحتوى مش ظاهر — فعّل MESSAGE_CONTENT=1")
    if msg.attachments:
        e.add_field(name="مرفقات", value="\n".join(a.url for a in msg.attachments)[:1000], inline=False)
    await glog(g, "messages", embed=e)


@bot.listen("on_message_edit")
async def _msg_edit(before: discord.Message, after: discord.Message):
    if not MESSAGE_CONTENT or after.guild is None or after.author.bot or before.content == after.content:
        return
    if _is_ticket(after.channel):
        return
    e = emb("✏️ رسالة اتعدلت", f"من {after.author.mention} في {after.channel.mention} — [الرسالة]({after.jump_url})", C_WARN)
    e.add_field(name="قبل", value=(before.content or "—")[:1000], inline=False)
    e.add_field(name="بعد", value=(after.content or "—")[:1000], inline=False)
    await glog(after.guild, "messages", embed=e)


@bot.listen("on_member_join")
async def _join(member: discord.Member):
    e = emb("📥 عضو دخل", f"{member.mention} ({member})", C_OK)
    e.add_field(name="الحساب اتعمل", value=discord.utils.format_dt(member.created_at, "R"))
    await glog(member.guild, "members", embed=e)


@bot.listen("on_member_remove")
async def _leave(member: discord.Member):
    roles = ", ".join(r.mention for r in member.roles if r.name != "@everyone") or "—"
    e = emb("📤 عضو خرج", f"{member.mention} ({member})", C_BAD)
    e.add_field(name="الرولات", value=roles[:1000])
    await glog(member.guild, "members", embed=e)


@bot.listen("on_member_update")
async def _member_update(before: discord.Member, after: discord.Member):
    if after.bot:
        return
    if before.nick != after.nick:
        await glog(after.guild, "members", f"✏️ {after.mention} غيّر الاسم: `{before.nick or before.name}` → `{after.nick or after.name}`")
    added = [r for r in after.roles if r not in before.roles]
    removed = [r for r in before.roles if r not in after.roles]
    if added or removed:
        parts = []
        if added:
            parts.append("➕ " + "، ".join(r.mention for r in added))
        if removed:
            parts.append("➖ " + "، ".join(r.mention for r in removed))
        await glog(after.guild, "members", f"🏷️ رولات {after.mention}: " + " | ".join(parts))


@bot.listen("on_member_ban")
async def _ban(guild: discord.Guild, user: discord.User):
    await glog(guild, "members", f"🔨 {user.mention} ({user}) اتبان.")


@bot.listen("on_member_unban")
async def _unban(guild: discord.Guild, user: discord.User):
    await glog(guild, "members", f"♻️ اتفك البان عن {user.mention} ({user}).")


@bot.listen("on_guild_channel_create")
async def _ch_create(channel):
    if not _is_ticket(channel):
        await glog(channel.guild, "server", f"➕ روم جديد: {channel.mention} (`{channel.name}`)")


@bot.listen("on_guild_channel_delete")
async def _ch_delete(channel):
    if not _is_ticket(channel):
        await glog(channel.guild, "server", f"➖ روم اتحذف: `{channel.name}`")


@bot.listen("on_guild_channel_update")
async def _ch_update(before, after):
    if before.name != after.name and not _is_ticket(after):
        await glog(after.guild, "server", f"✏️ روم اتغير اسمه: `{before.name}` → {after.mention}")


@bot.listen("on_guild_role_create")
async def _role_create(role: discord.Role):
    await glog(role.guild, "server", f"🏷️ رول جديد: {role.mention} (`{role.name}`)")


@bot.listen("on_guild_role_delete")
async def _role_delete(role: discord.Role):
    await glog(role.guild, "server", f"🏷️ رول اتحذف: `{role.name}`")
