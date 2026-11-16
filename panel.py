"""لوحة التحكم الإمبيد: قائمة أقسام + أزرار + مودالات لكل حاجة"""
import re
import time

import discord
from discord import app_commands

from core import (C_MAIN, PARA, SHORT, Form, LOG_CATS, SETUP_HOOKS, bot, cfg_get, cfg_int, db, emb, fmt_dur, fmt_num,
                  get_settings, glog, is_manager, manager_check, member_from_text, parse_ids, say, set_settings, to_float,
                  to_int)
from ledger import csv_from_rows, get_user, report_embed, report_rows, user_embed
from voice import (add_tracked, apply_rate, channels_lines, do_start, do_stop, live_count, remove_tracked, status_embed,
                   tick_guild)
from rates import (boost_embed, create_boost, people_embed, rates_embed, set_channel_rate, set_person,
                   start_manual_boost)
from economy import (adjust_message, do_adjust, do_close, do_pay, pay_summary_embed, top_embed)
from shop import create_item, items_embed, orders_embed, post_shop_panel
from tickets import post_ticket_panel, tickets_embed
from warns import do_warn, warns_embed
from embeds import send_custom_embed
from logs import logs_embed, set_log_channel

SECTIONS = {
    "home": ("🏠 الرئيسية", "نظرة سريعة + تحكم في بدء/إيقاف الحساب."),
    "voice": ("🔊 الفويسات والحساب", "إضافة/حذف فويسات، المعدل العام، الحد الأدنى للأشخاص، والأشخاص المحسوبين."),
    "points": ("➕ النقاط", "إضافة وسحب وضبط نقاط الأعضاء، وعرض حساب أي عضو."),
    "money": ("💵 الفلوس والفترات", "قيمة النقطة، تسجيل المدفوع، المتبقي، وقفل الفترة."),
    "rates": ("⚡ البوستات والمعدلات", "نقاط مضاعفة بالوقت/الأيام + معدلات خاصة للفويسات والرولات."),
    "shop": ("🛒 المتجر", "منتجات النقاط والطلبات."),
    "team": ("👮 التيم", "الورنات والتيكيتات."),
    "tools": ("🧰 اللوجات والإمبيد", "روم لوج لكل قسم، وإرسال إمبيد."),
}

# أزرار كل قسم: (action, label, style)
P, S, G, D = (discord.ButtonStyle.primary, discord.ButtonStyle.secondary, discord.ButtonStyle.success,
              discord.ButtonStyle.danger)
SECTION_BUTTONS = {
    "home": [("start", "ستارت", G), ("stop", "ستوب", D), ("refresh", "تحديث", S), ("report", "تقرير", P), ("top", "المتصدرين", S)],
    "voice": [("ch_add", "إضافة فويس", G), ("ch_remove", "حذف فويس", D), ("ch_list", "الفويسات", S),
              ("set_rate", "المعدل العام", P), ("set_min", "أقل عدد", S), ("people_add", "إضافة شخص", P),
              ("people_list", "الأشخاص", S)],
    "points": [("pts_add", "إضافة نقاط", G), ("pts_remove", "سحب نقاط", D), ("pts_set", "ضبط إجمالي", P),
               ("user_view", "حساب عضو", S), ("top", "المتصدرين", S)],
    "money": [("set_money", "قيمة النقطة", P), ("pay_add", "تسجيل دفع", G), ("pay_summary", "المدفوع/المتبقي", S),
              ("report", "تقرير CSV", S), ("period_close", "قفل الفترة", D)],
    "rates": [("boost_add", "بوست جديد", G), ("boost_list", "البوستات", S), ("boost_now", "بوست فوري", P),
              ("rate_chan", "معدل فويس", P), ("rates_list", "المعدلات", S)],
    "shop": [("shop_add", "منتج جديد", G), ("shop_list", "المنتجات", S), ("shop_orders", "الطلبات", P),
             ("shop_panel", "نشر المتجر", P)],
    "team": [("warn_add", "ورن", D), ("warn_list", "ورنات عضو", S), ("tk_panel", "نشر التيكيتات", P), ("tk_list", "التيكيتات", S)],
    "tools": [("logs_list", "اللوجات", S), ("logs_set", "ضبط لوج", P), ("embed_send", "إرسال إمبيد", G)],
}


def section_of(message) -> str:
    try:
        key = (message.embeds[0].footer.text or "").split("|")[-1].strip()
        return key if key in SECTIONS else "home"
    except Exception:
        return "home"


def panel_embed(guild: discord.Guild, section: str) -> discord.Embed:
    title, desc = SECTIONS[section]
    e = emb(f"لوحة التحكم — {title}", desc, color=C_MAIN)
    if section == "home":
        s = get_settings(guild.id)
        g = guild.id
        pending = db.execute("SELECT COUNT(*) c FROM orders WHERE guild_id=? AND status='pending'", (g,)).fetchone()["c"]
        tk = db.execute("SELECT COUNT(*) c FROM tickets WHERE guild_id=? AND status='open'", (g,)).fetchone()["c"]
        from rates import current_multiplier
        mult, names = current_multiplier(g)
        e.add_field(name="الحالة", value="🟢 شغال" if s["session_active"] else "⚪ واقف")
        e.add_field(name="المعدل", value=f"{fmt_num(s['rate'])}/س")
        e.add_field(name="البوست", value=f"×{mult:g}" if names else "—")
        e.add_field(name="بيتحسب دلوقتي", value=str(live_count(guild)))
        e.add_field(name="طلبات معلقة", value=str(pending))
        e.add_field(name="تيكيتات مفتوحة", value=str(tk))
    e.set_footer(text=f"لوحة التحكم | {section}")
    return e


class PanelBtn(discord.ui.DynamicItem[discord.ui.Button], template=r"pnb:(?P<action>[a-z_]+)"):
    def __init__(self, action: str, label: str = "", style=discord.ButtonStyle.secondary, row=None):
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=f"pnb:{action}", row=row))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction):
        if not is_manager(interaction.user, interaction.guild_id):
            return await say(interaction, "❌ للمسؤولين بس.")
        h = HANDLERS.get(self.action)
        if h is None:
            return await say(interaction, "الزرار ده مش متاح.")
        await h(interaction)

    async def on_error(self, interaction, error):
        print("panel btn error:", self.action, repr(error))
        await say(interaction, "⚠️ حصل خطأ.")


class PanelSection(discord.ui.DynamicItem[discord.ui.Select], template=r"pns:section"):
    def __init__(self, current: str = "home"):
        opts = [discord.SelectOption(label=t.split(" ", 1)[1], value=k, emoji=t.split(" ", 1)[0], default=(k == current))
                for k, (t, _) in SECTIONS.items()]
        super().__init__(discord.ui.Select(custom_id="pns:section", placeholder="اختار القسم", options=opts, row=0))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        if not is_manager(interaction.user, interaction.guild_id):
            return await say(interaction, "❌ للمسؤولين بس.")
        vals = (interaction.data or {}).get("values") or ["home"]
        sec = vals[0] if vals[0] in SECTIONS else "home"
        await interaction.response.edit_message(embed=panel_embed(interaction.guild, sec), view=panel_view(sec))

    async def on_error(self, interaction, error):
        print("panel section error:", repr(error))
        await say(interaction, "⚠️ حصل خطأ.")


def panel_view(section: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(PanelSection(section))
    for i, (action, label, style) in enumerate(SECTION_BUTTONS[section]):
        v.add_item(PanelBtn(action, label, style, row=1 + i // 5))
    return v


SETUP_HOOKS.append(lambda b: b.add_dynamic_items(PanelBtn, PanelSection))


async def refresh(interaction: discord.Interaction) -> None:
    sec = section_of(interaction.message)
    await interaction.response.edit_message(embed=panel_embed(interaction.guild, sec), view=panel_view(sec))


# ------------------------------------------------------------------ الأزرار (Handlers)
async def h_start(i):
    ok, txt = do_start(i.guild)
    await refresh(i)
    await i.followup.send(txt, ephemeral=True)
    if ok:
        await glog(i.guild, "settings", f"▶️ {i.user.mention} بدأ الحساب (لوحة التحكم).")


async def h_stop(i):
    ok, txt = do_stop(i.guild)
    await refresh(i)
    await i.followup.send(txt, ephemeral=True)
    if ok:
        await glog(i.guild, "settings", f"⏹️ {i.user.mention} وقّف الحساب (لوحة التحكم).")


async def h_refresh(i):
    await refresh(i)


async def h_report(i):
    if get_settings(i.guild_id)["session_active"]:
        tick_guild(i.guild, time.time())
    rows = report_rows(i.guild_id)
    await say(i, embed=report_embed(i.guild, rows), file=csv_from_rows(i.guild, rows))


async def h_top(i):
    e = top_embed(i.guild)
    await say(i, embed=e) if e else await say(i, "لسه مفيش بيانات.")


def form_btn(title, fields, submit):
    async def handler(i):
        await i.response.send_modal(Form(title, fields, submit))
    return handler


async def _need_member(inter, text):
    m = await member_from_text(inter.guild, text)
    if not m:
        await say(inter, "❌ مش لاقي العضو (حط منشن أو ID).")
    return m


def _num(inter_text):
    try:
        return to_float(inter_text)
    except ValueError:
        return None


# --- الفويسات
async def _ch_add(inter, v):
    msg = add_tracked(inter.guild, v["ids"])
    await say(inter, msg)
    if msg.startswith("✅"):
        await glog(inter.guild, "settings", f"➕ {inter.user.mention}\n{msg}")


async def _ch_remove(inter, v):
    await say(inter, remove_tracked(inter.guild, v["ids"]))


async def h_ch_list(i):
    await say(i, "\n".join(channels_lines(i.guild)) or "مفيش فويسات متضافة.")


async def _set_rate(inter, v):
    n = _num(v["rate"])
    if n is None or n < 0:
        return await say(inter, "❌ الرقم غلط.")
    await say(inter, apply_rate(inter.guild, n))
    await glog(inter.guild, "settings", f"⚙️ {inter.user.mention} غيّر المعدل لـ {fmt_num(n)}/ساعة")


async def _set_min(inter, v):
    try:
        n = max(1, min(99, to_int(v["n"])))
    except ValueError:
        return await say(inter, "❌ الرقم غلط.")
    set_settings(inter.guild_id, min_members=n)
    await say(inter, f"👥 الوقت هيتحسب بس لو في الروم **{n}** أو أكتر.")


async def _people_add(inter, v):
    m = await _need_member(inter, v["user"])
    if m:
        await say(inter, set_person(inter.guild, m.id, v["channels"]))


async def h_people_list(i):
    await say(i, embed=people_embed(i.guild))


# --- النقاط
def pts_handler(mode, title):
    async def submit(inter, v):
        m = await _need_member(inter, v["user"])
        n = _num(v["amount"])
        if not m:
            return
        if n is None or n < 0 or (n == 0 and mode != "set"):
            return await say(inter, "❌ الرقم غلط.")
        delta, new = do_adjust(inter.guild_id, inter.user.id, m.id, n, v["reason"] or "—", mode)
        await say(inter, adjust_message(mode, m.mention, abs(delta) if mode != "set" else n, new))
        await glog(inter.guild, "points", f"{'➕' if delta >= 0 else '➖'} {inter.user.mention} → {m.mention}: {delta:+,.2f} "
                                          f"(الإجمالي {fmt_num(new)}) — {v['reason'] or '—'}")

    return form_btn(title, [
        ("user", "العضو (منشن أو ID)", dict(max_length=60)),
        ("amount", "عدد النقاط" if mode != "set" else "الإجمالي الجديد", dict(max_length=15)),
        ("reason", "السبب (اختياري)", dict(required=False, max_length=100)),
    ], submit)


async def _user_view(inter, v):
    m = await _need_member(inter, v["user"])
    if m:
        await say(inter, embed=user_embed(inter.guild, m))


# --- الفلوس
async def _set_money(inter, v):
    n = _num(v["value"])
    if n is None or n < 0:
        return await say(inter, "❌ الرقم غلط.")
    set_settings(inter.guild_id, point_value=n)
    await say(inter, f"💰 قيمة النقطة بقت **{fmt_num(n)}**.", ephemeral=False)
    await glog(inter.guild, "settings", f"💰 {inter.user.mention} ضبط قيمة النقطة = {fmt_num(n)}")


async def _pay_add(inter, v):
    m = await _need_member(inter, v["user"])
    n = _num(v["amount"])
    if not m:
        return
    if n is None or n <= 0:
        return await say(inter, "❌ المبلغ غلط.")
    pid, u = do_pay(inter.guild_id, inter.user.id, m.id, n, v["note"] or "—")
    await say(inter, f"💵 اتسجل دفع `#{pid}` **{fmt_num(n)}** لـ {m.mention}. المتبقي: **{fmt_num(u['remaining'])}**", ephemeral=False)
    await glog(inter.guild, "pay", f"💵 {inter.user.mention} دفع {fmt_num(n)} لـ {m.mention} (#{pid}) — {v['note'] or '—'}")


async def h_pay_summary(i):
    if get_settings(i.guild_id)["session_active"]:
        tick_guild(i.guild, time.time())
    await say(i, embed=pay_summary_embed(i.guild))


class ConfirmClosePeriod(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="أيوه، اقفل الفترة وصفّر", style=discord.ButtonStyle.danger, emoji="🔒")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        file, rows, embed = do_close(interaction.guild)
        await interaction.response.edit_message(content="🔒 اتقفلت الفترة. التقرير تحت 👇", view=None)
        await interaction.followup.send(embed=embed, file=file)
        await glog(interaction.guild, "settings", f"🔒 {interaction.user.mention} قفل الفترة وصفّر النقاط (لوحة التحكم).")

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="اتلغى.", view=None)


async def h_period_close(i):
    await i.response.send_message("⚠️ ده هيصفّر نقاط كل الأعضاء ويبعت التقرير، والمتبقي من الفلوس بيترحّل للفترة الجاية. متأكد؟",
                                  view=ConfirmClosePeriod(), ephemeral=True)


# --- البوستات والمعدلات
async def _boost_add(inter, v):
    try:
        mult = to_float(v["mult"])
        hours = v["hours"].replace("→", "-").replace("–", "-")
        start, end = (re.split(r"\s*-\s*", hours, maxsplit=1) + ["00:00"])[:2] if hours else ("00:00", "00:00")
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", v["dates"].translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
        bid = create_boost(inter.guild_id, v["name"] or "بوست", mult, v["days"] or "all", start, end,
                           dates[0] if dates else None, dates[1] if len(dates) > 1 else None)
    except ValueError as ex:
        return await say(inter, f"❌ {ex}")
    await say(inter, f"⚡ اتضاف بوست `#{bid}` **{v['name']}** ×{mult:g}", ephemeral=False)
    await glog(inter.guild, "settings", f"⚡ {inter.user.mention} ضاف بوست #{bid} {v['name']} ×{mult:g}")


async def h_boost_list(i):
    await say(i, embed=boost_embed(i.guild_id))


async def _boost_now(inter, v):
    try:
        mult, minutes = to_float(v["mult"]), to_int(v["minutes"])
        if mult <= 0 or minutes <= 0:
            raise ValueError
    except ValueError:
        return await say(inter, "❌ القيم غلط.")
    start_manual_boost(inter.guild_id, mult, minutes)
    await say(inter, f"⚡ بوست ×{mult:g} شغال لمدة **{minutes}** دقيقة.", ephemeral=False)
    await glog(inter.guild, "settings", f"⚡ {inter.user.mention} شغّل بوست فوري ×{mult:g} لـ {minutes} د")


async def _rate_chan(inter, v):
    n = _num(v["rate"])
    if n is None or n < 0:
        return await say(inter, "❌ الرقم غلط.")
    await say(inter, set_channel_rate(inter.guild, v["ids"], n))


async def h_rates_list(i):
    await say(i, embed=rates_embed(i.guild))


# --- المتجر
async def _shop_add(inter, v):
    try:
        price = to_float(v["price"])
        stock = to_int(v["stock"]) if v["stock"] else None
        if price <= 0 or (stock is not None and stock < 0):
            raise ValueError
    except ValueError:
        return await say(inter, "❌ السعر أو المخزون غلط.")
    ids = parse_ids(v["role"]) if v["role"] else []
    iid = create_item(inter.guild_id, v["name"], price, v["desc"], stock, ids[0] if ids else None)
    await say(inter, f"✅ اتضاف المنتج `#{iid}` **{v['name']}** بسعر {fmt_num(price)} نقطة.", ephemeral=False)
    await glog(inter.guild, "shop", f"➕ {inter.user.mention} ضاف منتج #{iid} {v['name']} ({fmt_num(price)})")


async def h_shop_list(i):
    await say(i, embed=items_embed(i.guild_id))


async def h_shop_orders(i):
    await say(i, embed=orders_embed(i.guild_id))


async def _shop_panel(inter, v):
    ids = parse_ids(v["channel"])
    ch = inter.guild.get_channel(ids[0]) if ids else None
    if not isinstance(ch, discord.TextChannel):
        return await say(inter, "❌ مش لاقي الروم.")
    await post_shop_panel(ch)
    await say(inter, f"✅ اتنشر في {ch.mention}.")


# --- التيم
async def _warn_add(inter, v):
    m = await _need_member(inter, v["user"])
    if not m:
        return
    pen = None
    if v["penalty"]:
        pen = _num(v["penalty"])
        if pen is None or pen < 0:
            return await say(inter, "❌ الخصم غلط.")
    await inter.response.defer(ephemeral=True)
    await inter.followup.send(await do_warn(inter.guild, inter.user, m, v["reason"] or "—", pen), ephemeral=True)


async def _warn_list(inter, v):
    m = await _need_member(inter, v["user"])
    if m:
        await say(inter, embed=warns_embed(inter.guild_id, m))


async def _tk_panel(inter, v):
    ids = parse_ids(v["channel"])
    ch = inter.guild.get_channel(ids[0]) if ids else None
    if not isinstance(ch, discord.TextChannel):
        return await say(inter, "❌ مش لاقي الروم.")
    await post_ticket_panel(ch)
    await say(inter, f"✅ اتنشر في {ch.mention}.")


async def h_tk_list(i):
    await say(i, embed=tickets_embed(i.guild_id))


# --- الأدوات
async def h_logs_list(i):
    await say(i, embed=logs_embed(i.guild))


async def _logs_set(inter, v):
    key = v["cat"].strip().lower()
    rev = {label: k for k, label in LOG_CATS.items()}
    key = rev.get(v["cat"].strip(), key)
    if key not in LOG_CATS and key != "all":
        return await say(inter, "❌ القسم غلط. الأقسام: " + "، ".join(f"{k} ({l})" for k, l in LOG_CATS.items()) + " أو all")
    ids = parse_ids(v["channel"])
    ch = inter.guild.get_channel(ids[0]) if ids else None
    if not isinstance(ch, discord.TextChannel):
        return await say(inter, "❌ مش لاقي الروم.")
    for k in (LOG_CATS if key == "all" else [key]):
        set_log_channel(inter.guild_id, k, ch.id)
    await say(inter, f"✅ لوج **{'الكل' if key == 'all' else LOG_CATS[key]}** → {ch.mention}")


async def _embed_send(inter, v):
    await say(inter, await send_custom_embed(inter.guild, v["channel"], v))


HANDLERS = {
    "start": h_start, "stop": h_stop, "refresh": h_refresh, "report": h_report, "top": h_top,
    "ch_add": form_btn("إضافة فويس", [("ids", "لينك/ID فويس أو كاتيجوري (ممكن أكتر من واحد)", dict(style=PARA, max_length=1000))], _ch_add),
    "ch_remove": form_btn("حذف فويس", [("ids", "لينك/ID", dict(style=PARA, max_length=1000))], _ch_remove),
    "ch_list": h_ch_list,
    "set_rate": form_btn("المعدل العام", [("rate", "نقاط في الساعة", dict(max_length=10))], _set_rate),
    "set_min": form_btn("أقل عدد في الروم", [("n", "العدد (1 = حتى لو لوحده)", dict(max_length=3))], _set_min),
    "people_add": form_btn("إضافة شخص", [
        ("user", "العضو (منشن أو ID)", dict(max_length=60)),
        ("channels", "فويسات محددة (اختياري — فاضي = كلها)", dict(style=PARA, required=False, max_length=800))], _people_add),
    "people_list": h_people_list,
    "pts_add": pts_handler("add", "إضافة نقاط"), "pts_remove": pts_handler("remove", "سحب نقاط"),
    "pts_set": pts_handler("set", "ضبط إجمالي النقاط"),
    "user_view": form_btn("حساب عضو", [("user", "العضو (منشن أو ID)", dict(max_length=60))], _user_view),
    "set_money": form_btn("قيمة النقطة", [("value", "قيمة النقطة الواحدة بالفلوس", dict(max_length=12))], _set_money),
    "pay_add": form_btn("تسجيل دفع", [
        ("user", "العضو (منشن أو ID)", dict(max_length=60)), ("amount", "المبلغ المدفوع", dict(max_length=12)),
        ("note", "ملاحظة (اختياري)", dict(required=False, max_length=100))], _pay_add),
    "pay_summary": h_pay_summary, "period_close": h_period_close,
    "boost_add": form_btn("بوست جديد", [
        ("name", "الاسم", dict(max_length=60)), ("mult", "المضاعف (2 = ضعف)", dict(max_length=6)),
        ("hours", "الساعات 18:00-23:00 (فاضي = طول اليوم)", dict(required=False, max_length=20)),
        ("days", "الأيام: all أو الجمعة,السبت", dict(required=False, max_length=60)),
        ("dates", "التواريخ 2026-11-20 2026-11-25 (اختياري)", dict(required=False, max_length=40))], _boost_add),
    "boost_list": h_boost_list,
    "boost_now": form_btn("بوست فوري", [("mult", "المضاعف", dict(max_length=6)), ("minutes", "المدة بالدقايق", dict(max_length=6))], _boost_now),
    "rate_chan": form_btn("معدل خاص لفويس", [
        ("ids", "لينك/ID فويس أو كاتيجوري", dict(style=PARA, max_length=500)), ("rate", "نقاط/ساعة", dict(max_length=10))], _rate_chan),
    "rates_list": h_rates_list,
    "shop_add": form_btn("منتج جديد", [
        ("name", "اسم المنتج", dict(max_length=80)), ("price", "السعر بالنقاط", dict(max_length=12)),
        ("desc", "وصف (اختياري)", dict(required=False, max_length=200)),
        ("stock", "المخزون (فاضي = مفتوح)", dict(required=False, max_length=7)),
        ("role", "رول يتدّي أوتوماتيك (اختياري)", dict(required=False, max_length=40))], _shop_add),
    "shop_list": h_shop_list, "shop_orders": h_shop_orders,
    "shop_panel": form_btn("نشر المتجر", [("channel", "الروم (منشن أو ID)", dict(max_length=60))], _shop_panel),
    "warn_add": form_btn("ورن جديد", [
        ("user", "العضو (منشن أو ID)", dict(max_length=60)), ("reason", "السبب", dict(style=PARA, max_length=300)),
        ("penalty", "خصم نقاط (اختياري)", dict(required=False, max_length=10))], _warn_add),
    "warn_list": form_btn("ورنات عضو", [("user", "العضو (منشن أو ID)", dict(max_length=60))], _warn_list),
    "tk_panel": form_btn("نشر التيكيتات", [("channel", "الروم (منشن أو ID)", dict(max_length=60))], _tk_panel),
    "tk_list": h_tk_list,
    "logs_list": h_logs_list,
    "logs_set": form_btn("ضبط لوج", [
        ("cat", "القسم", dict(max_length=20, placeholder="voice/points/pay/shop/tickets/warns/settings/members/messages/server/all")),
        ("channel", "الروم (منشن أو ID)", dict(max_length=60))], _logs_set),
    "embed_send": form_btn("إرسال إمبيد", [
        ("channel", "الروم (منشن أو ID)", dict(max_length=60)),
        ("title", "العنوان", dict(required=False, max_length=256)),
        ("description", "الوصف", dict(style=PARA, required=False, max_length=4000)),
        ("color", "اللون hex (اختياري)", dict(required=False, max_length=7)),
        ("image", "رابط صورة (اختياري)", dict(required=False, max_length=500))], _embed_send),
}


@bot.tree.command(name="panel", description="لوحة التحكم الكاملة (إمبيد بأزرار وقوائم)")
@app_commands.guild_only()
@app_commands.describe(private="اعرضها ليك بس بدل ما تتنشر في الروم")
@app_commands.check(manager_check)
async def panel_cmd(interaction: discord.Interaction, private: bool = False):
    await interaction.response.send_message(embed=panel_embed(interaction.guild, "home"), view=panel_view("home"),
                                            ephemeral=private)
