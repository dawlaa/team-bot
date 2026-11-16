"""نظام الورنات للتيم: إنذارات مع خصم نقاط اختياري وحد أقصى وتايم أوت تلقائي"""
import time
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands

from core import (C_BAD, C_WARN, bot, cfg_float, cfg_int, cfg_set, db, emb, fmt_num, glog, manager_check,
                  notify_user, say)
from ledger import adjust_points


def active_warns(gid: int, uid: int) -> list:
    expire = cfg_int(gid, "warn_expire_days", 0)
    rows = db.execute("SELECT * FROM warns WHERE guild_id=? AND user_id=? AND active=1 ORDER BY ts", (gid, uid)).fetchall()
    if expire > 0:
        cutoff = time.time() - expire * 86400
        rows = [r for r in rows if r["ts"] >= cutoff]
    return rows


async def do_warn(guild: discord.Guild, by: discord.Member, member: discord.Member, reason: str,
                  penalty: Optional[float] = None) -> str:
    gid = guild.id
    pen = cfg_float(gid, "warn_default_penalty", 0.0) if penalty is None else penalty
    cur = db.execute("INSERT INTO warns(guild_id,user_id,by_id,reason,penalty,ts) VALUES (?,?,?,?,?,?)",
                     (gid, member.id, by.id, reason, pen, time.time()))
    db.commit()
    wid = cur.lastrowid
    if pen > 0:
        adjust_points(gid, member.id, -pen, by.id, f"ورن #{wid}: {reason}")
    count = len(active_warns(gid, member.id))
    limit = cfg_int(gid, "warn_limit", 3)
    e = emb(f"⚠️ ورن #{wid}", color=C_WARN)
    e.add_field(name="العضو", value=member.mention)
    e.add_field(name="بواسطة", value=by.mention)
    e.add_field(name="الورنات الفعالة", value=f"{count}/{limit}")
    e.add_field(name="السبب", value=reason or "—", inline=False)
    if pen > 0:
        e.add_field(name="خصم نقاط", value=fmt_num(pen))
    await glog(guild, "warns", embed=e)
    dm = emb(f"⚠️ جالك ورن في {guild.name}", reason or "—", color=C_WARN)
    dm.add_field(name="الورنات الفعالة", value=f"{count}/{limit}")
    if pen > 0:
        dm.add_field(name="اتخصم منك", value=f"{fmt_num(pen)} نقطة")
    await notify_user(member.id, embed=dm)
    msg = f"⚠️ اتسجل ورن `#{wid}` على {member.mention} ({count}/{limit})" + (f" وخصم {fmt_num(pen)} نقطة" if pen > 0 else "")
    if count >= limit:
        msg += f"\n🚨 وصل للحد الأقصى ({limit})!"
        minutes = cfg_int(gid, "warn_timeout_min", 0)
        extra = ""
        if minutes > 0:
            try:
                await member.timeout(timedelta(minutes=minutes), reason=f"وصل {count} ورنات")
                extra = f" واتعمله تايم أوت {minutes} دقيقة."
            except discord.HTTPException:
                extra = " (معرفتش أعمل تايم أوت — راجع صلاحياتي)."
        msg += extra
        await glog(guild, "warns", f"🚨 {member.mention} وصل **{count}** ورنات (الحد {limit}).{extra}")
    return msg


async def revoke_warn(guild: discord.Guild, wid: int, by_id: int) -> str:
    w = db.execute("SELECT * FROM warns WHERE id=? AND guild_id=?", (wid, guild.id)).fetchone()
    if not w or not w["active"]:
        return "❌ مفيش ورن فعال بالرقم ده."
    db.execute("UPDATE warns SET active=0 WHERE id=?", (wid,))
    db.commit()
    if w["penalty"] > 0:
        adjust_points(guild.id, w["user_id"], w["penalty"], by_id, f"إلغاء ورن #{wid}")
    await glog(guild, "warns", f"↩️ <@{by_id}> لغى ورن #{wid} على <@{w['user_id']}>"
                               + (f" واترجّع {fmt_num(w['penalty'])} نقطة" if w["penalty"] > 0 else ""))
    return f"↩️ اتلغى ورن `#{wid}`" + (f" واترجّع {fmt_num(w['penalty'])} نقطة." if w["penalty"] > 0 else ".")


def warns_embed(gid: int, member) -> discord.Embed:
    rows = active_warns(gid, member.id)
    limit = cfg_int(gid, "warn_limit", 3)
    lines = [f"`#{r['id']}` <t:{int(r['ts'])}:d> — {r['reason'] or '—'}"
             + (f" (-{fmt_num(r['penalty'])} نقطة)" if r["penalty"] > 0 else "") + f" — <@{r['by_id']}>" for r in rows]
    e = emb(f"⚠️ ورنات {member.display_name}", "\n".join(lines) or "مفيش ورنات فعالة ✅", color=C_BAD if rows else C_WARN)
    e.add_field(name="العدد", value=f"{len(rows)}/{limit}")
    return e


warn_group = app_commands.Group(name="warn", description="نظام الورنات للتيم", guild_only=True)


@warn_group.command(name="add", description="اديّ ورن لعضو")
@app_commands.describe(user="العضو", reason="السبب", penalty="نقاط تتخصم (سيبه فاضي = الافتراضي)")
@app_commands.check(manager_check)
async def warn_add(interaction: discord.Interaction, user: discord.Member, reason: str,
                   penalty: Optional[app_commands.Range[float, 0.0, 1000000.0]] = None):
    if user.bot:
        return await say(interaction, "مينفعش تدي ورن لبوت.")
    await interaction.response.defer()
    msg = await do_warn(interaction.guild, interaction.user, user, reason, penalty)
    await interaction.followup.send(msg)


@warn_group.command(name="list", description="ورنات عضو")
@app_commands.check(manager_check)
async def warn_list(interaction: discord.Interaction, user: discord.Member):
    await say(interaction, embed=warns_embed(interaction.guild_id, user))


@warn_group.command(name="remove", description="الغي ورن (وبيرجّع النقاط المخصومة)")
@app_commands.check(manager_check)
async def warn_remove(interaction: discord.Interaction, warn_id: int):
    await interaction.response.send_message(await revoke_warn(interaction.guild, warn_id, interaction.user.id))


@warn_group.command(name="clear", description="امسح كل ورنات عضو")
@app_commands.check(manager_check)
async def warn_clear(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer()
    out = []
    for w in active_warns(interaction.guild_id, user.id):
        out.append(await revoke_warn(interaction.guild, w["id"], interaction.user.id))
    await interaction.followup.send(f"🧹 اتمسحت {len(out)} ورن من {user.mention}.")


@warn_group.command(name="config", description="إعدادات الورنات")
@app_commands.describe(limit="الحد الأقصى قبل التنبيه", timeout_minutes="تايم أوت تلقائي عند الحد (0 = بدون)",
                       default_penalty="خصم نقاط افتراضي لكل ورن", expire_days="الورن بيتحسب لكام يوم (0 = دايمًا)")
@app_commands.check(manager_check)
async def warn_config(interaction: discord.Interaction, limit: Optional[app_commands.Range[int, 1, 50]] = None,
                      timeout_minutes: Optional[app_commands.Range[int, 0, 40320]] = None,
                      default_penalty: Optional[app_commands.Range[float, 0.0, 1000000.0]] = None,
                      expire_days: Optional[app_commands.Range[int, 0, 3650]] = None):
    g = interaction.guild_id
    for key, val in (("warn_limit", limit), ("warn_timeout_min", timeout_minutes),
                     ("warn_default_penalty", default_penalty), ("warn_expire_days", expire_days)):
        if val is not None:
            cfg_set(g, key, val)
    await say(interaction, (f"⚙️ **إعدادات الورنات**\nالحد: {cfg_int(g, 'warn_limit', 3)} • تايم أوت: {cfg_int(g, 'warn_timeout_min', 0)} د • "
                            f"خصم افتراضي: {fmt_num(cfg_float(g, 'warn_default_penalty', 0.0))} • صلاحية الورن: "
                            f"{cfg_int(g, 'warn_expire_days', 0) or 'دايمًا'} يوم"), ephemeral=True)


bot.tree.add_command(warn_group)


@bot.tree.command(name="mywarns", description="شوف ورناتك")
@app_commands.guild_only()
async def mywarns_cmd(interaction: discord.Interaction):
    await say(interaction, embed=warns_embed(interaction.guild_id, interaction.user), ephemeral=True)
