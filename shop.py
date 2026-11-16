"""المتجر: منتجات بنقاط، طلبات، تسليم/استرجاع"""
import time
from typing import Optional

import discord
from discord import app_commands

from core import (C_BAD, C_OK, C_WARN, SETUP_HOOKS, bot, cfg_get, cfg_int, cfg_set, db, emb, fmt_num, get_settings,
                  glog, is_manager, manager_check, notify_user, parse_ids, say)
from ledger import get_user, refund_points, spend_points


# ------------------------------------------------------------------ منطق
def stock_text(stock) -> str:
    return "∞" if stock is None else str(stock)


def create_item(gid: int, name: str, price: float, desc: str = "", stock: Optional[int] = None,
                role_id: Optional[int] = None) -> int:
    cur = db.execute(
        "INSERT INTO shop_items(guild_id,name,description,price,stock,role_id) VALUES (?,?,?,?,?,?)",
        (gid, name[:80], desc[:200], price, stock, role_id))
    db.commit()
    return cur.lastrowid


def get_item(gid: int, item_id: int):
    return db.execute("SELECT * FROM shop_items WHERE id=? AND guild_id=?", (item_id, gid)).fetchone()


def active_items(gid: int) -> list:
    return db.execute("SELECT * FROM shop_items WHERE guild_id=? AND active=1 ORDER BY price", (gid,)).fetchall()


def do_purchase(guild: discord.Guild, member: discord.Member, item_id: int):
    """بيرجع (ok, رسالة, order_id)."""
    item = get_item(guild.id, item_id)
    if not item or not item["active"]:
        return False, "❌ المنتج ده مش متاح.", 0
    if item["stock"] is not None and item["stock"] <= 0:
        return False, "❌ المنتج ده خلص.", 0
    avail = get_user(guild.id, member.id)["avail"]
    if avail + 1e-9 < item["price"]:
        return False, f"❌ رصيدك **{fmt_num(avail)}** ومحتاج **{fmt_num(item['price'])}**.", 0
    spend_points(guild.id, member.id, item["price"])
    if item["stock"] is not None:
        db.execute("UPDATE shop_items SET stock=stock-1 WHERE id=?", (item_id,))
    cur = db.execute(
        "INSERT INTO orders(guild_id,user_id,item_id,item_name,price,ts) VALUES (?,?,?,?,?,?)",
        (guild.id, member.id, item_id, item["name"], item["price"], time.time()))
    db.commit()
    left = get_user(guild.id, member.id)["avail"]
    return True, f"✅ اشتريت **{item['name']}** بـ {fmt_num(item['price'])} نقطة. رصيدك دلوقتي **{fmt_num(left)}**.", cur.lastrowid


def process_order(guild: discord.Guild, oid: int, action: str, actor_id: int):
    """action = done | refund. بيرجع (ok, رسالة, order)."""
    o = db.execute("SELECT * FROM orders WHERE id=? AND guild_id=?", (oid, guild.id)).fetchone()
    if not o:
        return False, "الطلب مش موجود.", None
    if o["status"] != "pending":
        return False, f"الطلب ده اتعامل معاه قبل كده ({o['status']}).", o
    if action == "done":
        db.execute("UPDATE orders SET status='done', handled_by=? WHERE id=?", (actor_id, oid))
    else:
        db.execute("UPDATE orders SET status='refunded', handled_by=? WHERE id=?", (actor_id, oid))
        refund_points(guild.id, o["user_id"], o["price"])
        if o["item_id"]:
            db.execute("UPDATE shop_items SET stock=stock+1 WHERE id=? AND stock IS NOT NULL", (o["item_id"],))
    db.commit()
    return True, "اتسلم ✅" if action == "done" else "اتلغى واتسترجعت النقاط ↩️", o


def order_embed(o, status: Optional[str] = None) -> discord.Embed:
    color = {"pending": C_WARN, "done": C_OK, "refunded": C_BAD}.get(status or o["status"], C_WARN)
    e = emb(f"🛒 طلب #{o['id']}", color=color)
    e.add_field(name="العضو", value=f"<@{o['user_id']}>")
    e.add_field(name="المنتج", value=o["item_name"])
    e.add_field(name="السعر", value=f"{fmt_num(o['price'])} نقطة")
    return e


def order_view(oid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(ShopBtn("done", oid))
    v.add_item(ShopBtn("refund", oid))
    return v


async def post_order(guild: discord.Guild, member: discord.Member, oid: int) -> None:
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    item = get_item(guild.id, o["item_id"]) if o["item_id"] else None
    # منتج بيدّي رول: بيتسلم أوتوماتيك
    if item and item["role_id"]:
        role = guild.get_role(item["role_id"])
        try:
            if role:
                await member.add_roles(role, reason=f"شراء من المتجر (طلب #{oid})")
            db.execute("UPDATE orders SET status='done', handled_by=? WHERE id=?", (bot.user.id, oid))
            db.commit()
            await glog(guild, "shop", f"🛒 {member.mention} اشترى **{o['item_name']}** (طلب #{oid}) — اتسلم أوتوماتيك (رول).")
            return
        except discord.HTTPException:
            pass  # مفيش صلاحية رولات → يتحول لطلب يدوي
    cid = cfg_int(guild.id, "shop_orders_channel", 0) or int(
        cfg_get(guild.id, "log:shop") or get_settings(guild.id)["log_channel_id"] or 0)
    ch = guild.get_channel(cid) if cid else None
    if ch is None:
        await glog(guild, "shop", f"🛒 طلب جديد #{oid}: {member.mention} اشترى **{o['item_name']}** — حدد روم الطلبات بـ /shop orders_channel")
        return
    msg = await ch.send(embed=order_embed(o), view=order_view(oid), allowed_mentions=discord.AllowedMentions.none())
    db.execute("UPDATE orders SET message_id=?, channel_id=? WHERE id=?", (msg.id, ch.id, oid))
    db.commit()
    await glog(guild, "shop", f"🛒 {member.mention} اشترى **{o['item_name']}** بـ {fmt_num(o['price'])} (طلب #{oid})")


def shop_embed(guild: discord.Guild, member: discord.Member) -> discord.Embed:
    items = active_items(guild.id)
    bal = get_user(guild.id, member.id)["avail"]
    lines = [f"**{i['name']}** — {fmt_num(i['price'])} نقطة" + (f" • متبقي {i['stock']}" if i["stock"] is not None else "")
             + (f"\n└ {i['description']}" if i["description"] else "") for i in items]
    e = emb("🛒 المتجر", "\n".join(lines) or "المتجر فاضي دلوقتي.", color=C_OK)
    e.add_field(name="رصيدك", value=f"**{fmt_num(bal)}** نقطة")
    return e


class ShopSelect(discord.ui.Select):
    def __init__(self, items):
        opts = [discord.SelectOption(label=i["name"][:100], description=f"{fmt_num(i['price'])} نقطة"[:100], value=str(i["id"]))
                for i in items[:25]]
        super().__init__(placeholder="اختار المنتج...", options=opts)

    async def callback(self, interaction: discord.Interaction):
        item = get_item(interaction.guild_id, int(self.values[0]))
        if not item or not item["active"]:
            return await interaction.response.edit_message(content="المنتج مش متاح.", embed=None, view=None)
        e = emb("تأكيد الشراء", f"**{item['name']}**\n{item['description'] or ''}\n\nالسعر: **{fmt_num(item['price'])}** نقطة", color=C_WARN)
        await interaction.response.edit_message(embed=e, view=BuyConfirm(item["id"]))


class ShopView(discord.ui.View):
    def __init__(self, items):
        super().__init__(timeout=180)
        if items:
            self.add_item(ShopSelect(items))


class BuyConfirm(discord.ui.View):
    def __init__(self, item_id: int):
        super().__init__(timeout=60)
        self.item_id = item_id

    @discord.ui.button(label="تأكيد الشراء", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, msg, oid = do_purchase(interaction.guild, interaction.user, self.item_id)
        await interaction.response.edit_message(content=msg, embed=None, view=None)
        if ok:
            await post_order(interaction.guild, interaction.user, oid)

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="اتلغى.", embed=None, view=None)


async def open_shop(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=shop_embed(interaction.guild, interaction.user),
        view=ShopView(active_items(interaction.guild_id)), ephemeral=True)


class ShopBtn(discord.ui.DynamicItem[discord.ui.Button], template=r"shop:(?P<action>open|done|refund)(?::(?P<oid>\d+))?"):
    STYLES = {
        "open": ("افتح المتجر", discord.ButtonStyle.primary, "🛒"),
        "done": ("تم التسليم", discord.ButtonStyle.success, "✅"),
        "refund": ("رفض واسترجاع", discord.ButtonStyle.danger, "↩️"),
    }

    def __init__(self, action: str, oid: int = 0):
        label, style, emoji = self.STYLES[action]
        cid = f"shop:{action}" + (f":{oid}" if oid else "")
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=cid))
        self.action, self.oid = action, oid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["oid"] or 0))

    async def callback(self, interaction: discord.Interaction):
        if self.action == "open":
            return await open_shop(interaction)
        if not is_manager(interaction.user, interaction.guild_id):
            return await say(interaction, "❌ للمسؤولين بس.")
        ok, msg, o = process_order(interaction.guild, self.oid, self.action, interaction.user.id)
        if not ok:
            return await say(interaction, msg)
        status = "done" if self.action == "done" else "refunded"
        e = order_embed(o, status)
        e.add_field(name="الحالة", value=f"{msg} بواسطة {interaction.user.mention}", inline=False)
        await interaction.response.edit_message(embed=e, view=None)
        word = "اتسلّم" if self.action == "done" else "اتلغى واتسترجعت نقاطه"
        await notify_user(o["user_id"], content=f"🛒 طلبك **{o['item_name']}** (#{o['id']}) {word}.")
        await glog(interaction.guild, "shop", f"{'✅' if self.action == 'done' else '↩️'} {interaction.user.mention} "
                                              f"{'سلّم' if self.action == 'done' else 'استرجع'} طلب #{o['id']} ({o['item_name']}) لـ <@{o['user_id']}>")

    async def on_error(self, interaction, error):
        print("shop btn error:", repr(error))
        await say(interaction, "⚠️ حصل خطأ.")


SETUP_HOOKS.append(lambda b: b.add_dynamic_items(ShopBtn))


async def post_shop_panel(channel: discord.abc.Messageable) -> None:
    e = emb("🛒 متجر النقاط", "بدّل نقاطك بمنتجات! دوس على الزرار وشوف المتاح ورصيدك.", color=C_OK)
    v = discord.ui.View(timeout=None)
    v.add_item(ShopBtn("open"))
    await channel.send(embed=e, view=v)


def items_embed(gid: int) -> discord.Embed:
    rows = db.execute("SELECT * FROM shop_items WHERE guild_id=? ORDER BY id", (gid,)).fetchall()
    lines = [f"{'🟢' if r['active'] else '⏸️'} `#{r['id']}` **{r['name']}** — {fmt_num(r['price'])} نقطة • مخزون {stock_text(r['stock'])}"
             + (f" • 🏷️ <@&{r['role_id']}>" if r["role_id"] else "") for r in rows]
    return emb("🛒 منتجات المتجر", "\n".join(lines) or "مفيش منتجات.")


def orders_embed(gid: int, status: str = "pending") -> discord.Embed:
    rows = db.execute("SELECT * FROM orders WHERE guild_id=? AND status=? ORDER BY id DESC LIMIT 20", (gid, status)).fetchall()
    lines = [f"`#{r['id']}` <@{r['user_id']}> — **{r['item_name']}** ({fmt_num(r['price'])}) <t:{int(r['ts'])}:R>" for r in rows]
    return emb(f"📦 الطلبات ({status})", "\n".join(lines) or "مفيش طلبات.")


# ------------------------------------------------------------------ الأوامر
@bot.tree.command(name="store", description="افتح المتجر وبدّل نقاطك")
@app_commands.guild_only()
async def store_cmd(interaction: discord.Interaction):
    await open_shop(interaction)


@bot.tree.command(name="myorders", description="آخر طلباتك من المتجر")
@app_commands.guild_only()
async def myorders_cmd(interaction: discord.Interaction):
    rows = db.execute("SELECT * FROM orders WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
                      (interaction.guild_id, interaction.user.id)).fetchall()
    lines = [f"`#{r['id']}` **{r['item_name']}** — {fmt_num(r['price'])} — {r['status']}" for r in rows]
    await say(interaction, embed=emb("📦 طلباتك", "\n".join(lines) or "مفيش طلبات لسه."))


shop_group = app_commands.Group(name="shop", description="إدارة المتجر", guild_only=True)


@shop_group.command(name="add", description="ضيف منتج")
@app_commands.describe(name="اسم المنتج", price="السعر بالنقاط", description="وصف", stock="المخزون (سيبه فاضي = مفتوح)",
                       role="رول يتدّي تلقائيًا عند الشراء (اختياري)")
@app_commands.check(manager_check)
async def shop_add(interaction: discord.Interaction, name: str, price: app_commands.Range[float, 0.01, 100000000.0],
                   description: str = "", stock: Optional[app_commands.Range[int, 0, 1000000]] = None,
                   role: Optional[discord.Role] = None):
    iid = create_item(interaction.guild_id, name, price, description, stock, role.id if role else None)
    await interaction.response.send_message(f"✅ اتضاف المنتج `#{iid}` **{name}** بسعر {fmt_num(price)} نقطة.")
    await glog(interaction.guild, "shop", f"➕ {interaction.user.mention} ضاف منتج #{iid} {name} ({fmt_num(price)})")


@shop_group.command(name="edit", description="عدّل منتج")
@app_commands.describe(item_id="رقم المنتج", active="تفعيل/إيقاف")
@app_commands.check(manager_check)
async def shop_edit(interaction: discord.Interaction, item_id: int, name: Optional[str] = None,
                    price: Optional[app_commands.Range[float, 0.01, 100000000.0]] = None,
                    description: Optional[str] = None, stock: Optional[app_commands.Range[int, 0, 1000000]] = None,
                    active: Optional[bool] = None):
    item = get_item(interaction.guild_id, item_id)
    if not item:
        return await say(interaction, "مفيش منتج بالرقم ده.")
    db.execute("UPDATE shop_items SET name=?, price=?, description=?, stock=?, active=? WHERE id=?", (
        name or item["name"], price if price is not None else item["price"],
        description if description is not None else item["description"],
        stock if stock is not None else item["stock"], int(active) if active is not None else item["active"], item_id))
    db.commit()
    await say(interaction, f"✏️ اتعدّل المنتج `#{item_id}`.")


@shop_group.command(name="remove", description="احذف منتج")
@app_commands.check(manager_check)
async def shop_remove(interaction: discord.Interaction, item_id: int):
    cur = db.execute("DELETE FROM shop_items WHERE id=? AND guild_id=?", (item_id, interaction.guild_id))
    db.commit()
    await say(interaction, "🗑️ اتحذف." if cur.rowcount else "مفيش منتج بالرقم ده.")


@shop_group.command(name="list", description="كل المنتجات (للإدارة)")
@app_commands.check(manager_check)
async def shop_list(interaction: discord.Interaction):
    await say(interaction, embed=items_embed(interaction.guild_id))


@shop_group.command(name="orders", description="الطلبات المعلقة")
@app_commands.choices(status=[app_commands.Choice(name="معلقة", value="pending"), app_commands.Choice(name="اتسلمت", value="done"),
                              app_commands.Choice(name="اتسترجعت", value="refunded")])
@app_commands.check(manager_check)
async def shop_orders(interaction: discord.Interaction, status: str = "pending"):
    await say(interaction, embed=orders_embed(interaction.guild_id, status))


async def _process_cmd(interaction, order_id, action):
    ok, msg, o = process_order(interaction.guild, order_id, action, interaction.user.id)
    await say(interaction, f"{'✅' if ok else '❌'} {msg}")
    if ok:
        await notify_user(o["user_id"], content=f"🛒 طلبك **{o['item_name']}** (#{o['id']}) "
                                                f"{'اتسلّم' if action == 'done' else 'اتلغى واتسترجعت نقاطه'}.")
        await glog(interaction.guild, "shop", f"{interaction.user.mention} → طلب #{order_id}: {msg}")


@shop_group.command(name="done", description="علّم طلب إنه اتسلم")
@app_commands.check(manager_check)
async def shop_done(interaction: discord.Interaction, order_id: int):
    await _process_cmd(interaction, order_id, "done")


@shop_group.command(name="refund", description="ارفض طلب وارجّع النقاط")
@app_commands.check(manager_check)
async def shop_refund(interaction: discord.Interaction, order_id: int):
    await _process_cmd(interaction, order_id, "refund")


@shop_group.command(name="orders_channel", description="الروم اللي بتوصل فيه الطلبات الجديدة")
@app_commands.check(manager_check)
async def shop_orders_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg_set(interaction.guild_id, "shop_orders_channel", channel.id)
    await say(interaction, f"📦 الطلبات هتوصل في {channel.mention}.")


@shop_group.command(name="panel", description="انشر بانل المتجر (زرار) في روم")
@app_commands.check(manager_check)
async def shop_panel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    ch = channel or interaction.channel
    await post_shop_panel(ch)
    await say(interaction, f"✅ اتنشر في {ch.mention}.")


bot.tree.add_command(shop_group)
