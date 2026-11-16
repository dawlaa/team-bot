"""نظام الإمبيد المتكامل: إنشاء/تعديل/حقول/JSON"""
import json
from typing import Optional

import discord
from discord import app_commands

from core import (C_MAIN, PARA, SHORT, Form, bot, glog, manager_check, parse_ids, say)


def parse_color(text: str, default: int = C_MAIN) -> int:
    t = (text or "").strip().lstrip("#")
    if not t:
        return default
    return int(t, 16)


def build_embed(v: dict) -> discord.Embed:
    e = discord.Embed(title=v.get("title") or None, description=v.get("description") or None,
                      color=parse_color(v.get("color", "")))
    if v.get("image"):
        e.set_image(url=v["image"])
    if v.get("footer"):
        e.set_footer(text=v["footer"])
    return e


async def send_custom_embed(guild: discord.Guild, channel_text: str, v: dict) -> str:
    ids = parse_ids(channel_text)
    ch = guild.get_channel(ids[0]) if ids else None
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return "❌ مش لاقي روم الكتابة."
    if not (v.get("title") or v.get("description")):
        return "❌ لازم عنوان أو وصف."
    try:
        await ch.send(embed=build_embed(v))
    except ValueError:
        return "❌ اللون لازم يبقى hex زي #FF0000."
    except discord.HTTPException as ex:
        return f"❌ فشل الإرسال: {ex}"
    return f"✅ اتبعت في {ch.mention}."


async def fetch_bot_message(guild: discord.Guild, link: str) -> Optional[discord.Message]:
    ids = parse_ids(link)
    if len(ids) < 2:
        return None
    ch = guild.get_channel(ids[-2])
    if ch is None or not hasattr(ch, "fetch_message"):
        return None
    try:
        msg = await ch.fetch_message(ids[-1])
    except discord.HTTPException:
        return None
    return msg if msg.author.id == bot.user.id and msg.embeds else None


embed_group = app_commands.Group(name="embed", description="أدوات الإمبيد", guild_only=True)


@embed_group.command(name="send", description="اعمل إمبيد جديد وابعته في روم")
@app_commands.describe(channel="الروم")
@app_commands.check(manager_check)
async def embed_send(interaction: discord.Interaction, channel: discord.TextChannel):
    async def submit(inter, v):
        msg = await send_custom_embed(inter.guild, str(channel.id), v)
        await say(inter, msg)
        if msg.startswith("✅"):
            await glog(inter.guild, "settings", f"🖼️ {inter.user.mention} بعت إمبيد في {channel.mention}")

    await interaction.response.send_modal(Form("إمبيد جديد", [
        ("title", "العنوان", dict(required=False, max_length=256)),
        ("description", "الوصف", dict(style=PARA, required=False, max_length=4000)),
        ("color", "اللون (hex مثل #5865F2)", dict(required=False, max_length=7, placeholder="#5865F2")),
        ("image", "رابط صورة (اختياري)", dict(required=False, max_length=500)),
        ("footer", "الفوتر (اختياري)", dict(required=False, max_length=200)),
    ], submit))


@embed_group.command(name="edit", description="عدّل إمبيد بعته البوت (بلينك الرسالة)")
@app_commands.describe(message_link="لينك الرسالة")
@app_commands.check(manager_check)
async def embed_edit(interaction: discord.Interaction, message_link: str):
    msg = await fetch_bot_message(interaction.guild, message_link)
    if not msg:
        return await say(interaction, "❌ مش لاقي رسالة إمبيد تخص البوت بالرابط ده.")
    e = msg.embeds[0]

    async def submit(inter, v):
        try:
            e.title = v["title"] or None
            e.description = v["description"] or None
            e.color = parse_color(v["color"], e.color.value if e.color else C_MAIN)
        except ValueError:
            return await say(inter, "❌ اللون لازم يبقى hex زي #FF0000.")
        if v["image"]:
            e.set_image(url=v["image"])
        else:
            e.set_image(url=None)
        if v["footer"]:
            e.set_footer(text=v["footer"])
        else:
            e.remove_footer()
        await msg.edit(embed=e)
        await say(inter, "✅ اتعدّل.")

    color_hex = f"#{e.color.value:06X}" if e.color else ""
    await interaction.response.send_modal(Form("تعديل إمبيد", [
        ("title", "العنوان", dict(required=False, max_length=256, default=e.title or "")),
        ("description", "الوصف", dict(style=PARA, required=False, max_length=4000, default=(e.description or "")[:4000])),
        ("color", "اللون (hex)", dict(required=False, max_length=7, default=color_hex)),
        ("image", "رابط صورة", dict(required=False, max_length=500, default=(e.image.url if e.image else "") or "")),
        ("footer", "الفوتر", dict(required=False, max_length=200, default=(e.footer.text if e.footer else "") or "")),
    ], submit))


@embed_group.command(name="field_add", description="ضيف حقل لإمبيد")
@app_commands.describe(message_link="لينك الرسالة", name="اسم الحقل", value="المحتوى", inline="جنب بعض؟")
@app_commands.check(manager_check)
async def embed_field_add(interaction: discord.Interaction, message_link: str, name: str, value: str, inline: bool = False):
    msg = await fetch_bot_message(interaction.guild, message_link)
    if not msg:
        return await say(interaction, "❌ مش لاقي رسالة إمبيد تخص البوت بالرابط ده.")
    e = msg.embeds[0]
    if len(e.fields) >= 25:
        return await say(interaction, "❌ الإمبيد وصل للحد (25 حقل).")
    e.add_field(name=name[:256], value=value[:1024], inline=inline)
    await msg.edit(embed=e)
    await say(interaction, f"✅ اتضاف الحقل (رقم {len(e.fields)}).")


@embed_group.command(name="field_remove", description="شيل حقل من إمبيد بالرقم")
@app_commands.describe(message_link="لينك الرسالة", index="رقم الحقل (1 = الأول)")
@app_commands.check(manager_check)
async def embed_field_remove(interaction: discord.Interaction, message_link: str, index: app_commands.Range[int, 1, 25]):
    msg = await fetch_bot_message(interaction.guild, message_link)
    if not msg:
        return await say(interaction, "❌ مش لاقي رسالة إمبيد تخص البوت بالرابط ده.")
    e = msg.embeds[0]
    if index > len(e.fields):
        return await say(interaction, f"❌ الإمبيد فيه {len(e.fields)} حقل بس.")
    e.remove_field(index - 1)
    await msg.edit(embed=e)
    await say(interaction, "🗑️ اتشال الحقل.")


@embed_group.command(name="json", description="ابعت إمبيد من JSON جاهز (متقدم)")
@app_commands.describe(channel="الروم")
@app_commands.check(manager_check)
async def embed_json(interaction: discord.Interaction, channel: discord.TextChannel):
    async def submit(inter, v):
        try:
            data = json.loads(v["json"])
            items = data["embeds"] if isinstance(data, dict) and "embeds" in data else [data]
            embeds = [discord.Embed.from_dict(d) for d in items[:10]]
            content = data.get("content") if isinstance(data, dict) else None
            await channel.send(content=content, embeds=embeds)
        except (ValueError, KeyError, TypeError, discord.HTTPException) as ex:
            return await say(inter, f"❌ JSON غلط أو مرفوض: {ex}")
        await say(inter, f"✅ اتبعت في {channel.mention}.")

    await interaction.response.send_modal(Form("إمبيد من JSON", [
        ("json", "JSON", dict(style=PARA, max_length=4000, placeholder='{"title":"...","description":"...","color":5793266}'))], submit))


bot.tree.add_command(embed_group)
