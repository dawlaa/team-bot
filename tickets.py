"""نظام التيكيتات: بانل، فتح بمودال، استلام، إضافة/إزالة أشخاص، إغلاق مع ترانسكريبت"""
import asyncio
import io
import time
from typing import Optional

import discord
from discord import app_commands

from core import (C_BAD, C_OK, C_WARN, MESSAGE_CONTENT, PARA, SETUP_HOOKS, Form, bot, cfg_get, cfg_int, cfg_set, db, emb,
                  glog, is_manager, manager_check, notify_user, say)


def get_ticket(channel_id: int):
    return db.execute("SELECT * FROM tickets WHERE channel_id=?", (channel_id,)).fetchone()


def is_staff(member, gid: int) -> bool:
    if is_manager(member, gid):
        return True
    rid = cfg_int(gid, "tk_staff_role", 0)
    return bool(rid and isinstance(member, discord.Member) and any(r.id == rid for r in member.roles))


async def make_transcript(channel: discord.TextChannel, t) -> bytes:
    lines = [f"Ticket #{t['id']} | opened by {t['user_id']} | topic: {t['topic']}", "=" * 60]
    if not MESSAGE_CONTENT:
        lines.append("(ملاحظة: المحتوى مش بيظهر لرسائل الأعضاء — فعّل MESSAGE_CONTENT=1 و Message Content Intent)")
    async for m in channel.history(limit=None, oldest_first=True):
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S")
        body = m.content or ""
        if m.embeds and not body:
            body = "[embed] " + " | ".join(filter(None, (e.title or e.description for e in m.embeds)))[:300]
        lines.append(f"[{ts}] {m.author} ({m.author.id}): {body}")
        for a in m.attachments:
            lines.append(f"    📎 {a.url}")
    return "\n".join(lines).encode("utf-8")


def tfile(data: bytes, tid: int) -> discord.File:
    return discord.File(io.BytesIO(data), filename=f"ticket-{tid:04d}.txt")


async def close_ticket(channel: discord.TextChannel, closer: discord.Member, reason: str) -> None:
    t = get_ticket(channel.id)
    if not t or t["status"] != "open":
        return
    db.execute("UPDATE tickets SET status='closed', closed=?, closed_by=?, close_reason=? WHERE id=?",
               (time.time(), closer.id, reason, t["id"]))
    db.commit()
    guild = channel.guild
    data = await make_transcript(channel, t)
    e = emb(f"🔒 تيكيت #{t['id']} اتقفل", color=C_BAD)
    e.add_field(name="صاحب التيكيت", value=f"<@{t['user_id']}>")
    e.add_field(name="اتقفل بواسطة", value=closer.mention)
    e.add_field(name="الموضوع", value=(t["topic"] or "—")[:200], inline=False)
    e.add_field(name="السبب", value=reason or "—", inline=False)
    if t["claimed_by"]:
        e.add_field(name="المستلم", value=f"<@{t['claimed_by']}>")
    await glog(guild, "tickets", embed=e, file=tfile(data, t["id"]))
    await notify_user(t["user_id"], embed=e, file=tfile(data, t["id"]))
    try:
        await channel.send("🔒 التيكيت هيتقفل بعد 5 ثواني...")
        await asyncio.sleep(5)
        await channel.delete(reason=f"Ticket #{t['id']} closed by {closer}")
    except discord.HTTPException as ex:
        print("ticket delete error:", repr(ex))


async def create_ticket(interaction: discord.Interaction, topic: str) -> None:
    guild, member = interaction.guild, interaction.user
    category = guild.get_channel(cfg_int(guild.id, "tk_category", 0))
    if not isinstance(category, discord.CategoryChannel):
        return await say(interaction, "⚠️ نظام التيكيتات مش متظبط لسه، كلّم الإدارة (`/ticket setup`).")
    max_open = cfg_int(guild.id, "tk_max", 1)
    mine = db.execute("SELECT * FROM tickets WHERE guild_id=? AND user_id=? AND status='open'",
                      (guild.id, member.id)).fetchall()
    if len(mine) >= max_open:
        ch = guild.get_channel(mine[0]["channel_id"])
        return await say(interaction, f"عندك تيكيت مفتوح بالفعل: {ch.mention if ch else '#' + str(mine[0]['id'])}")
    await interaction.response.defer(ephemeral=True)
    cur = db.execute("INSERT INTO tickets(guild_id,user_id,topic,created) VALUES (?,?,?,?)",
                     (guild.id, member.id, topic, time.time()))
    db.commit()
    tid = cur.lastrowid
    perms = dict(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(**perms),
        guild.me: discord.PermissionOverwrite(**perms, manage_channels=True, manage_messages=True),
    }
    staff = guild.get_role(cfg_int(guild.id, "tk_staff_role", 0))
    if staff:
        overwrites[staff] = discord.PermissionOverwrite(**perms, manage_messages=True)
    try:
        channel = await guild.create_text_channel(
            f"ticket-{tid:04d}", category=category, overwrites=overwrites,
            topic=f"Ticket #{tid} | {member} | {topic[:100]}")
    except discord.HTTPException as ex:
        db.execute("DELETE FROM tickets WHERE id=?", (tid,))
        db.commit()
        return await interaction.followup.send(f"❌ معرفتش أعمل الروم: {ex}", ephemeral=True)
    db.execute("UPDATE tickets SET channel_id=? WHERE id=?", (channel.id, tid))
    db.commit()
    e = emb(f"🎫 تيكيت #{tid}", color=C_OK)
    e.add_field(name="صاحب التيكيت", value=member.mention)
    e.add_field(name="الموضوع", value=topic[:500] or "—", inline=False)
    e.set_footer(text="اكتب مشكلتك وفريق الدعم هيرد عليك قريب.")
    view = discord.ui.View(timeout=None)
    view.add_item(TicketBtn("claim"))
    view.add_item(TicketBtn("close"))
    await channel.send(content=f"{member.mention} {staff.mention if staff else ''}", embed=e, view=view,
                       allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    await interaction.followup.send(f"✅ اتفتح تيكيتك: {channel.mention}", ephemeral=True)
    await glog(guild, "tickets", f"🎫 {member.mention} فتح تيكيت #{tid} ({channel.mention}) — {topic[:150]}")


class ConfirmClose(discord.ui.View):
    def __init__(self, reason: str = ""):
        super().__init__(timeout=60)
        self.reason = reason

    @discord.ui.button(label="تأكيد الإغلاق", style=discord.ButtonStyle.danger, emoji="🔒")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="⏳ جاري الإغلاق...", view=None)
        await close_ticket(interaction.channel, interaction.user, self.reason)

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="اتلغى.", view=None)


async def do_claim(interaction: discord.Interaction) -> None:
    t = get_ticket(interaction.channel_id)
    if not t or t["status"] != "open":
        return await say(interaction, "ده مش تيكيت مفتوح.")
    if not is_staff(interaction.user, interaction.guild_id):
        return await say(interaction, "❌ للفريق بس.", ephemeral=True)
    if t["claimed_by"]:
        return await say(interaction, f"التيكيت مستلمه <@{t['claimed_by']}> بالفعل.")
    db.execute("UPDATE tickets SET claimed_by=? WHERE id=?", (interaction.user.id, t["id"]))
    db.commit()
    await interaction.response.send_message(f"✋ {interaction.user.mention} استلم التيكيت.")
    await glog(interaction.guild, "tickets", f"✋ {interaction.user.mention} استلم تيكيت #{t['id']}")


class TicketBtn(discord.ui.DynamicItem[discord.ui.Button], template=r"tkt:(?P<action>open|close|claim)"):
    STYLES = {
        "open": ("افتح تيكيت", discord.ButtonStyle.primary, "🎫"),
        "close": ("إغلاق", discord.ButtonStyle.danger, "🔒"),
        "claim": ("استلام", discord.ButtonStyle.success, "✋"),
    }

    def __init__(self, action: str):
        label, style, emoji = self.STYLES[action]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"tkt:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction):
        if self.action == "open":
            async def submit(inter, v):
                await create_ticket(inter, v["topic"])
            return await interaction.response.send_modal(Form("فتح تيكيت", [
                ("topic", "الموضوع", dict(style=PARA, placeholder="اكتب مشكلتك أو طلبك باختصار", max_length=400))], submit))
        if self.action == "claim":
            return await do_claim(interaction)
        t = get_ticket(interaction.channel_id)
        if not t or t["status"] != "open":
            return await say(interaction, "ده مش تيكيت مفتوح.")
        if interaction.user.id != t["user_id"] and not is_staff(interaction.user, interaction.guild_id):
            return await say(interaction, "❌ مش مسموحلك تقفل التيكيت ده.", ephemeral=True)
        await interaction.response.send_message("متأكد إنك عايز تقفل التيكيت؟", view=ConfirmClose(), ephemeral=True)

    async def on_error(self, interaction, error):
        print("ticket btn error:", repr(error))
        await say(interaction, "⚠️ حصل خطأ.")


SETUP_HOOKS.append(lambda b: b.add_dynamic_items(TicketBtn))


async def post_ticket_panel(channel, title: str = "🎫 الدعم والتيكيتات",
                            desc: str = "محتاج مساعدة؟ دوس على الزرار وافتح تيكيت وهيتواصل معاك الفريق.") -> None:
    v = discord.ui.View(timeout=None)
    v.add_item(TicketBtn("open"))
    await channel.send(embed=emb(title, desc, color=C_OK), view=v)


def tickets_embed(gid: int) -> discord.Embed:
    rows = db.execute("SELECT * FROM tickets WHERE guild_id=? AND status='open' ORDER BY id", (gid,)).fetchall()
    lines = [f"`#{r['id']}` <#{r['channel_id']}> — <@{r['user_id']}>" + (f" • ✋ <@{r['claimed_by']}>" if r["claimed_by"] else "")
             + f" • {(r['topic'] or '')[:50]}" for r in rows]
    return emb("🎫 التيكيتات المفتوحة", "\n".join(lines) or "مفيش تيكيتات مفتوحة.")


# ------------------------------------------------------------------ الأوامر
ticket_group = app_commands.Group(name="ticket", description="نظام التيكيتات", guild_only=True)


@ticket_group.command(name="setup", description="إعداد نظام التيكيتات")
@app_commands.describe(category="الكاتيجوري اللي هتتفتح فيها التيكيتات", staff_role="رول فريق الدعم",
                       max_open="أقصى عدد تيكيتات مفتوحة للشخص")
@app_commands.check(manager_check)
async def tk_setup(interaction: discord.Interaction, category: discord.CategoryChannel, staff_role: discord.Role,
                   max_open: app_commands.Range[int, 1, 10] = 1):
    g = interaction.guild_id
    cfg_set(g, "tk_category", category.id)
    cfg_set(g, "tk_staff_role", staff_role.id)
    cfg_set(g, "tk_max", max_open)
    await interaction.response.send_message(
        f"✅ التيكيتات هتتفتح في **{category.name}** وفريق الدعم {staff_role.mention} (أقصى {max_open} للشخص).\n"
        "الخطوة الجاية: `/ticket panel` لنشر زرار الفتح. وحدد روم اللوج بـ `/logs set` (قسم التيكيتات) عشان الترانسكريبت.",
        allowed_mentions=discord.AllowedMentions.none())


@ticket_group.command(name="panel", description="انشر بانل فتح التيكيتات")
@app_commands.check(manager_check)
async def tk_panel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                   title: str = "🎫 الدعم والتيكيتات",
                   description: str = "محتاج مساعدة؟ دوس على الزرار وافتح تيكيت وهيتواصل معاك الفريق."):
    ch = channel or interaction.channel
    await post_ticket_panel(ch, title, description)
    await say(interaction, f"✅ اتنشر في {ch.mention}.")


@ticket_group.command(name="close", description="اقفل التيكيت الحالي")
@app_commands.describe(reason="سبب الإغلاق")
async def tk_close(interaction: discord.Interaction, reason: str = ""):
    t = get_ticket(interaction.channel_id)
    if not t or t["status"] != "open":
        return await say(interaction, "الأمر ده بيتكتب جوه روم تيكيت مفتوح.")
    if interaction.user.id != t["user_id"] and not is_staff(interaction.user, interaction.guild_id):
        return await say(interaction, "❌ مش مسموحلك.", ephemeral=True)
    await interaction.response.send_message("متأكد إنك عايز تقفل التيكيت؟", view=ConfirmClose(reason), ephemeral=True)


@ticket_group.command(name="claim", description="استلم التيكيت الحالي")
async def tk_claim(interaction: discord.Interaction):
    await do_claim(interaction)


@ticket_group.command(name="add", description="ضيف شخص للتيكيت")
async def tk_add(interaction: discord.Interaction, user: discord.Member):
    t = get_ticket(interaction.channel_id)
    if not t or t["status"] != "open":
        return await say(interaction, "الأمر ده جوه روم تيكيت مفتوح بس.")
    if not is_staff(interaction.user, interaction.guild_id):
        return await say(interaction, "❌ للفريق بس.", ephemeral=True)
    await interaction.channel.set_permissions(user, view_channel=True, send_messages=True, attach_files=True,
                                              read_message_history=True)
    await interaction.response.send_message(f"➕ اتضاف {user.mention} للتيكيت.")
    await glog(interaction.guild, "tickets", f"➕ {interaction.user.mention} ضاف {user.mention} لتيكيت #{t['id']}")


@ticket_group.command(name="remove", description="شيل شخص من التيكيت")
async def tk_remove(interaction: discord.Interaction, user: discord.Member):
    t = get_ticket(interaction.channel_id)
    if not t or t["status"] != "open":
        return await say(interaction, "الأمر ده جوه روم تيكيت مفتوح بس.")
    if not is_staff(interaction.user, interaction.guild_id):
        return await say(interaction, "❌ للفريق بس.", ephemeral=True)
    if user.id == t["user_id"]:
        return await say(interaction, "مينفعش تشيل صاحب التيكيت.")
    await interaction.channel.set_permissions(user, overwrite=None)
    await interaction.response.send_message(f"➖ اتشال {user.mention} من التيكيت.")
    await glog(interaction.guild, "tickets", f"➖ {interaction.user.mention} شال {user.mention} من تيكيت #{t['id']}")


@ticket_group.command(name="list", description="التيكيتات المفتوحة")
@app_commands.check(manager_check)
async def tk_list(interaction: discord.Interaction):
    await say(interaction, embed=tickets_embed(interaction.guild_id))


bot.tree.add_command(ticket_group)
