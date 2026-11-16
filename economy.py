"""النقاط والمدفوعات والتقارير (يدوي وتلقائي) وقفل الفترة"""
import time
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from core import (C_MAIN, C_OK, SETUP_HOOKS, bot, cfg_get, cfg_int, cfg_set, db, emb, fmt_dur, fmt_num, get_settings,
                  get_tz, glog, manager_check, say)
from ledger import (add_payout, adjust_points, apply_close, csv_from_rows, get_user, leaderboard, period_no,
                    report_embed, report_rows, user_embed)
from voice import tick_guild


# ------------------------------------------------------------------ منطق مشترك
def do_adjust(gid: int, actor_id: int, uid: int, amount: float, reason: str, mode: str) -> tuple[float, float]:
    cur = get_user(gid, uid)["total"]
    if mode == "add":
        delta = amount
    elif mode == "remove":
        delta = -amount
    else:  # set
        delta = amount - cur
    adjust_points(gid, uid, delta, actor_id, reason if mode != "set" else f"set: {reason}")
    return delta, get_user(gid, uid)["total"]


def adjust_message(mode: str, mention: str, amount: float, new_total: float) -> str:
    if mode == "add":
        return f"➕ اتضاف **{fmt_num(amount)}** نقطة لـ {mention}. الإجمالي: **{fmt_num(new_total)}**"
    if mode == "remove":
        return f"➖ اتسحب **{fmt_num(amount)}** نقطة من {mention}. الإجمالي: **{fmt_num(new_total)}**"
    return f"🎯 إجمالي {mention} بقى **{fmt_num(new_total)}** نقطة."


def do_pay(gid: int, actor_id: int, uid: int, amount: float, note: str) -> tuple[int, dict]:
    pid = add_payout(gid, uid, actor_id, amount, note)
    return pid, get_user(gid, uid)


def top_embed(guild: discord.Guild) -> Optional[discord.Embed]:
    board = leaderboard(guild.id)[:10]
    if not board:
        return None
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{r['user_id']}> — **{fmt_dur(r['seconds'])}**"
        for i, r in enumerate(board)
    ]
    return emb("🏆 المتصدرين", "\n".join(lines), color=0xF1C40F)


def pay_summary_embed(guild: discord.Guild) -> discord.Embed:
    rows = [r for r in report_rows(guild.id) if abs(r["remaining"]) > 0.004 or r["paid"]]
    rows.sort(key=lambda r: r["remaining"], reverse=True)
    pv = get_settings(guild.id)["point_value"]
    lines = [f"<@{r['user_id']}> — الإجمالي {fmt_num(r['value'] + r['carry'])} | مدفوع {fmt_num(r['paid'])} | "
             f"**متبقي {fmt_num(r['remaining'])}**" for r in rows[:25]]
    e = emb("💵 المدفوع والمتبقي", "\n".join(lines) or "مفيش مستحقات حالياً.", color=C_OK)
    e.add_field(name="إجمالي المتبقي", value=fmt_num(sum(r["remaining"] for r in rows)))
    e.add_field(name="إجمالي المدفوع", value=fmt_num(sum(r["paid"] for r in rows)))
    if pv <= 0:
        e.set_footer(text="⚠️ قيمة النقطة = 0 — استخدم /settings money")
    return e


def do_close(guild: discord.Guild):
    """بيقفل الفترة ويرجع (ملف CSV, rows, embed)."""
    if get_settings(guild.id)["session_active"]:
        tick_guild(guild, time.time())
    rows = report_rows(guild.id)
    file = csv_from_rows(guild, rows)
    embed = report_embed(guild, rows, f"🔒 تقرير الفترة #{period_no(guild.id)}")
    apply_close(guild, rows)
    return file, rows, embed


# ------------------------------------------------------------------ النقاط
points_group = app_commands.Group(name="points", description="تعديل نقاط الأعضاء", guild_only=True)


async def _adjust_cmd(interaction, user, amount, reason, mode):
    delta, new = do_adjust(interaction.guild_id, interaction.user.id, user.id, amount, reason, mode)
    await interaction.response.send_message(adjust_message(mode, user.mention, abs(delta) if mode != "set" else amount, new))
    await glog(interaction.guild, "points",
               f"{'➕' if delta >= 0 else '➖'} {interaction.user.mention} → {user.mention}: {delta:+,.2f} (الإجمالي {fmt_num(new)}) — {reason}")


@points_group.command(name="add", description="ضيف نقاط لعضو")
@app_commands.describe(user="العضو", amount="عدد النقاط", reason="السبب (اختياري)")
@app_commands.check(manager_check)
async def p_add(interaction: discord.Interaction, user: discord.Member,
                amount: app_commands.Range[float, 0.01, 1000000.0], reason: str = "—"):
    await _adjust_cmd(interaction, user, amount, reason, "add")


@points_group.command(name="remove", description="اسحب نقاط من عضو")
@app_commands.describe(user="العضو", amount="عدد النقاط", reason="السبب (اختياري)")
@app_commands.check(manager_check)
async def p_remove(interaction: discord.Interaction, user: discord.Member,
                   amount: app_commands.Range[float, 0.01, 1000000.0], reason: str = "—"):
    await _adjust_cmd(interaction, user, amount, reason, "remove")


@points_group.command(name="set", description="اضبط إجمالي نقاط عضو على رقم معين")
@app_commands.describe(user="العضو", amount="الإجمالي الجديد", reason="السبب (اختياري)")
@app_commands.check(manager_check)
async def p_set(interaction: discord.Interaction, user: discord.Member,
                amount: app_commands.Range[float, 0.0, 1000000.0], reason: str = "—"):
    await _adjust_cmd(interaction, user, amount, reason, "set")


@points_group.command(name="history", description="سجل التعديلات اليدوية على عضو")
@app_commands.describe(user="العضو")
@app_commands.check(manager_check)
async def p_history(interaction: discord.Interaction, user: discord.Member):
    rows = db.execute(
        "SELECT * FROM adjustments WHERE guild_id=? AND user_id=? ORDER BY ts DESC LIMIT 15",
        (interaction.guild_id, user.id)).fetchall()
    if not rows:
        return await say(interaction, "مفيش تعديلات على العضو ده.")
    lines = [f"<t:{int(r['ts'])}:d> | {r['amount']:+,.2f} | <@{r['by_id']}> | {r['reason']}" for r in rows]
    await say(interaction, f"**آخر تعديلات {user.display_name}:**\n" + "\n".join(lines))


bot.tree.add_command(points_group)

# ------------------------------------------------------------------ المدفوعات
pay_group = app_commands.Group(name="pay", description="تتبع المدفوع والمتبقي لكل عضو", guild_only=True)


@pay_group.command(name="add", description="سجّل مبلغ اتدفع لعضو")
@app_commands.describe(user="العضو", amount="المبلغ المدفوع", note="ملاحظة (اختياري)")
@app_commands.check(manager_check)
async def pay_add(interaction: discord.Interaction, user: discord.Member,
                  amount: app_commands.Range[float, 0.01, 100000000.0], note: str = "—"):
    pid, u = do_pay(interaction.guild_id, interaction.user.id, user.id, amount, note)
    await interaction.response.send_message(
        f"💵 اتسجل دفع `#{pid}` **{fmt_num(amount)}** لـ {user.mention}. المتبقي: **{fmt_num(u['remaining'])}**")
    await glog(interaction.guild, "pay",
               f"💵 {interaction.user.mention} دفع {fmt_num(amount)} لـ {user.mention} (#{pid}) — {note}")


@pay_group.command(name="undo", description="الغي عملية دفع بالرقم")
@app_commands.check(manager_check)
async def pay_undo(interaction: discord.Interaction, payment_id: int):
    r = db.execute("SELECT * FROM payouts WHERE id=? AND guild_id=?", (payment_id, interaction.guild_id)).fetchone()
    if not r:
        return await say(interaction, "مفيش عملية دفع بالرقم ده.")
    db.execute("DELETE FROM payouts WHERE id=?", (payment_id,))
    db.commit()
    await interaction.response.send_message(f"↩️ اتلغى دفع `#{payment_id}` ({fmt_num(r['amount'])}) لـ <@{r['user_id']}>.")
    await glog(interaction.guild, "pay", f"↩️ {interaction.user.mention} لغى دفع #{payment_id} ({fmt_num(r['amount'])}) لـ <@{r['user_id']}>")


@pay_group.command(name="history", description="سجل المدفوعات لعضو")
@app_commands.check(manager_check)
async def pay_history(interaction: discord.Interaction, user: discord.Member):
    rows = db.execute("SELECT * FROM payouts WHERE guild_id=? AND user_id=? ORDER BY ts DESC LIMIT 15",
                      (interaction.guild_id, user.id)).fetchall()
    if not rows:
        return await say(interaction, "مفيش مدفوعات للعضو ده.")
    lines = [f"`#{r['id']}` <t:{int(r['ts'])}:d> | **{fmt_num(r['amount'])}** | <@{r['by_id']}> | {r['note']}" for r in rows]
    await say(interaction, f"**مدفوعات {user.display_name}:**\n" + "\n".join(lines))


@pay_group.command(name="summary", description="المدفوع والمتبقي لكل الأعضاء")
@app_commands.check(manager_check)
async def pay_summary(interaction: discord.Interaction):
    if get_settings(interaction.guild_id)["session_active"]:
        tick_guild(interaction.guild, time.time())
    await say(interaction, embed=pay_summary_embed(interaction.guild))


bot.tree.add_command(pay_group)


# ------------------------------------------------------------------ للأعضاء والتقارير
@bot.tree.command(name="me", description="شوف نقاطك ووقتك ومستحقاتك")
@app_commands.guild_only()
async def me_cmd(interaction: discord.Interaction):
    await say(interaction, embed=user_embed(interaction.guild, interaction.user), ephemeral=True)


@bot.tree.command(name="user", description="شوف نقاط عضو معين")
@app_commands.guild_only()
@app_commands.check(manager_check)
async def user_cmd(interaction: discord.Interaction, member: discord.Member):
    await say(interaction, embed=user_embed(interaction.guild, member))


@bot.tree.command(name="top", description="لوحة المتصدرين")
@app_commands.guild_only()
async def top_cmd(interaction: discord.Interaction):
    e = top_embed(interaction.guild)
    if not e:
        return await say(interaction, "لسه مفيش بيانات.")
    await interaction.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="report", description="تقرير CSV بكل الأعضاء (ساعات ونقاط وفلوس ومتبقي)")
@app_commands.guild_only()
@app_commands.check(manager_check)
async def report_cmd(interaction: discord.Interaction):
    if get_settings(interaction.guild_id)["session_active"]:
        tick_guild(interaction.guild, time.time())
    rows = report_rows(interaction.guild_id)
    await say(interaction, embed=report_embed(interaction.guild, rows), file=csv_from_rows(interaction.guild, rows))


period_group = app_commands.Group(name="period", description="إدارة الفترات (أسبوع/شهر)", guild_only=True)


@period_group.command(name="close", description="اقفل الفترة: يبعت التقرير ويصفّر النقاط ويرحّل المتبقي")
@app_commands.describe(confirm="لازم تكون True عشان التصفير يتنفذ")
@app_commands.check(manager_check)
async def period_close(interaction: discord.Interaction, confirm: bool = False):
    if not confirm:
        return await say(interaction, "⚠️ ده هيصفّر نقاط وساعات كل الأعضاء بعد ما يبعت التقرير، والمتبقي من الفلوس "
                                      "بيتنقل للفترة الجاية. اكتب `confirm: True` للتأكيد.")
    file, rows, embed = do_close(interaction.guild)
    await interaction.response.send_message("🔒 اتقفلت الفترة واتصفّر الحساب. ده تقرير الفترة:", embed=embed, file=file)
    await glog(interaction.guild, "settings", f"🔒 {interaction.user.mention} قفل الفترة وصفّر النقاط.")


bot.tree.add_command(period_group)

# ------------------------------------------------------------------ التقرير التلقائي
FREQ_CHOICES = [app_commands.Choice(name="أسبوعي", value="weekly"), app_commands.Choice(name="شهري", value="monthly")]
WEEKDAY_CHOICES = [app_commands.Choice(name=n, value=i) for i, n in enumerate(
    ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"])]


def sched_moment(gid: int, dt: datetime) -> datetime:
    """آخر ميعاد تقرير فات (أو ده اللي حصل دلوقتي)."""
    freq = cfg_get(gid, "ar_freq", "off")
    hour = cfg_int(gid, "ar_hour", 9)
    if freq == "weekly":
        wd = cfg_int(gid, "ar_weekday", 4)
        back = (dt.weekday() - wd) % 7
        s = (dt - timedelta(days=back)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if s > dt:
            s -= timedelta(days=7)
        return s
    day = min(max(cfg_int(gid, "ar_day", 1), 1), 28)
    s = dt.replace(day=day, hour=hour, minute=0, second=0, microsecond=0)
    if s > dt:
        y, m = (s.year, s.month - 1) if s.month > 1 else (s.year - 1, 12)
        s = s.replace(year=y, month=m)
    return s


def sched_key(gid: int) -> str:
    return sched_moment(gid, datetime.now(get_tz(gid))).strftime("%Y-%m-%d %H")


async def send_report(guild: discord.Guild, channel, auto_close: bool, title: str = "📄 التقرير الدوري"):
    if get_settings(guild.id)["session_active"]:
        tick_guild(guild, time.time())
    rows = report_rows(guild.id)
    embed = report_embed(guild, rows, title)
    file = csv_from_rows(guild, rows)
    if auto_close:
        embed.set_footer(text="تم قفل الفترة وتصفير النقاط. المتبقي اترحّل للفترة الجاية.")
        apply_close(guild, rows)
    await channel.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())


@tasks.loop(seconds=60)
async def auto_report():
    for guild in bot.guilds:
        try:
            gid = guild.id
            if cfg_get(gid, "ar_freq", "off") == "off":
                continue
            ch = guild.get_channel(cfg_int(gid, "ar_channel", 0))
            if ch is None:
                continue
            key = sched_key(gid)
            if cfg_get(gid, "ar_last") == key:
                continue
            cfg_set(gid, "ar_last", key)  # الأول عشان مايتكررش
            await send_report(guild, ch, cfg_get(gid, "ar_close", "0") == "1")
            await glog(guild, "settings", f"📄 اتبعت التقرير التلقائي في {ch.mention}")
        except Exception as e:
            print("auto report error:", guild.id, repr(e))


@auto_report.before_loop
async def _wait_ar():
    await bot.wait_until_ready()


@bot.listen("on_ready")
async def _start_ar():
    if not auto_report.is_running(): auto_report.start()

autoreport_group = app_commands.Group(name="autoreport", description="تقرير تلقائي أسبوعي/شهري", guild_only=True)


@autoreport_group.command(name="set", description="فعّل التقرير التلقائي في روم معين")
@app_commands.describe(
    channel="الروم اللي هيتبعت فيه", frequency="أسبوعي ولا شهري", weekday="يوم الأسبوع (للأسبوعي)",
    day="يوم الشهر 1-28 (للشهري)", hour="الساعة 0-23 بتوقيت السيرفر",
    auto_close="يقفل الفترة ويصفّر النقاط بعد التقرير؟")
@app_commands.choices(frequency=FREQ_CHOICES, weekday=WEEKDAY_CHOICES)
@app_commands.check(manager_check)
async def ar_set(interaction: discord.Interaction, channel: discord.TextChannel, frequency: str,
                 weekday: int = 4, day: app_commands.Range[int, 1, 28] = 1,
                 hour: app_commands.Range[int, 0, 23] = 9, auto_close: bool = False):
    g = interaction.guild_id
    for k, v in (("ar_channel", channel.id), ("ar_freq", frequency), ("ar_weekday", weekday),
                 ("ar_day", day), ("ar_hour", hour), ("ar_close", "1" if auto_close else "0")):
        cfg_set(g, k, v)
    cfg_set(g, "ar_last", sched_key(g))  # عشان مايبعتش فورًا على ميعاد فات
    when = (f"كل {WEEKDAY_CHOICES[weekday].name}" if frequency == "weekly" else f"يوم {day} من كل شهر") + f" الساعة {hour:02d}:00"
    await interaction.response.send_message(
        f"📄 التقرير التلقائي شغال في {channel.mention} — {when}"
        f"{' (وبيقفل الفترة تلقائيًا)' if auto_close else ''}.")
    await glog(interaction.guild, "settings", f"📄 {interaction.user.mention} فعّل التقرير التلقائي: {when}")


@autoreport_group.command(name="off", description="إيقاف التقرير التلقائي")
@app_commands.check(manager_check)
async def ar_off(interaction: discord.Interaction):
    cfg_set(interaction.guild_id, "ar_freq", "off")
    await say(interaction, "⏹️ اتوقف التقرير التلقائي.")


@autoreport_group.command(name="status", description="حالة التقرير التلقائي")
@app_commands.check(manager_check)
async def ar_status(interaction: discord.Interaction):
    g = interaction.guild_id
    freq = cfg_get(g, "ar_freq", "off")
    if freq == "off":
        return await say(interaction, "التقرير التلقائي واقف.")
    ch = cfg_int(g, "ar_channel", 0)
    when = (f"كل {WEEKDAY_CHOICES[cfg_int(g, 'ar_weekday', 4)].name}" if freq == "weekly"
            else f"يوم {cfg_int(g, 'ar_day', 1)} من كل شهر") + f" الساعة {cfg_int(g, 'ar_hour', 9):02d}:00"
    await say(interaction, f"📄 شغال في <#{ch}> — {when}. قفل تلقائي: {'أيوه' if cfg_get(g, 'ar_close', '0') == '1' else 'لأ'}.")


@autoreport_group.command(name="now", description="ابعت التقرير في الروم المحدد دلوقتي (من غير تصفير)")
@app_commands.check(manager_check)
async def ar_now(interaction: discord.Interaction):
    ch = interaction.guild.get_channel(cfg_int(interaction.guild_id, "ar_channel", 0))
    if ch is None:
        return await say(interaction, "حدد الروم الأول بـ `/autoreport set`.")
    await send_report(interaction.guild, ch, False)
    await say(interaction, f"✅ اتبعت في {ch.mention}.")


bot.tree.add_command(autoreport_group)


# === أوامر الإدارة الإضافية (مسح الداتا وقائمة البوينتات) ===
class ConfirmResetAllView(discord.ui.View):
    def __init__(self, author_id: int, guild_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.guild_id = guild_id

    @discord.ui.button(label="تأكيد مسح كل الداتا نهائياً", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await say(interaction, "❌ الزرار ده للي كتب الأمر بس.", ephemeral=True)
            return
        for tbl in ("users", "adjustments", "balances", "payouts", "warns", "orders"):
            try:
                db.execute(f"DELETE FROM {tbl} WHERE guild_id=?", (self.guild_id,))
            except Exception:
                pass
        db.commit()
        await interaction.response.edit_message(content="✅ **تم مسح كل داتا الأعضاء والنقاط والساعات والورنات والطلبات في السيرفر بنجاح!**", view=None)

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await say(interaction, "❌ الزرار ده للي كتب الأمر بس.", ephemeral=True)
            return
        await interaction.response.edit_message(content="❎ تم إلغاء عملية المسح.", view=None)


@app_commands.command(name="resetuser", description="[إدارة] مسح كل داتا عضو معين من البوت")
@app_commands.describe(member="العضو المراد مسح بياناته")
@app_commands.default_permissions(administrator=True)
async def resetuser_cmd(interaction: discord.Interaction, member: discord.Member):
    for tbl in ("users", "adjustments", "balances", "payouts", "warns", "orders"):
        try:
            db.execute(f"DELETE FROM {tbl} WHERE guild_id=? AND user_id=?", (interaction.guild_id, member.id))
        except Exception:
            pass
    db.commit()
    await say(interaction, f"✅ تم مسح كل داتا {member.mention} (البوينتات، الساعات، السجل، والتحذيرات) بنجاح.", ephemeral=True)


@app_commands.command(name="resetall", description="[إدارة] مسح كل داتا الأعضاء والبوينتات في السيرفر بالكامل")
@app_commands.default_permissions(administrator=True)
async def resetall_cmd(interaction: discord.Interaction):
    await say(
        interaction,
        "⚠️ **تحذير:** الخطوة دي هتمسح كل البوينتات والساعات والسجلات والورنات والطلبات لكل الأعضاء في السيرفر نهائياً (مع الإبقاء على إعدادات الرومات والمتجر). متأكد؟",
        ephemeral=True,
        view=ConfirmResetAllView(interaction.user.id, interaction.guild_id)
    )


@app_commands.command(name="pointslist", description="[إدارة] عرض قائمة بكل الأعضاء اللي معاهم بوينتات")
@app_commands.default_permissions(administrator=True)
async def pointslist_cmd(interaction: discord.Interaction):
    rows = db.execute("""
        SELECT u.user_id, u.seconds, u.earned, u.adjust, u.spent, COALESCE(b.carry, 0) as carry
        FROM users u
        LEFT JOIN balances b ON u.guild_id = b.guild_id AND u.user_id = b.user_id
        WHERE u.guild_id=?
    """, (interaction.guild_id,)).fetchall()
    items = []
    for r in rows:
        tot = float(r["earned"] or 0) + float(r["adjust"] or 0) + float(r["carry"] or 0)
        avail = max(0.0, tot - float(r["spent"] or 0))
        secs = int(r["seconds"] or 0)
        if avail > 0 or tot > 0 or secs > 0:
            items.append((avail, tot, secs, r["user_id"]))
    items.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    if not items:
        await say(interaction, "📭 مفيش أي عضو معاه بوينتات أو ساعات مسجلة حالياً.")
        return
    pv = float(get_settings(interaction.guild_id)["point_value"] or 0)
    lines = []
    for i, (avail, tot, secs, uid) in enumerate(items[:30], 1):
        money_txt = f" — **{fmt_num(avail * pv)}**" if pv > 0 else ""
        lines.append(f"`{i}.` <@{uid}> — **{fmt_num(avail)}** نقطة (إجمالي: {fmt_num(tot)}) | ⏱️ {fmt_dur(secs)}{money_txt}")
    emb = discord.Embed(
        title=f"📊 قائمة بوينتات الأعضاء ({len(items)} عضو)",
        description=chr(10).join(lines),
        color=discord.Color.gold()
    )
    emb.set_footer(text="Dev by DAWLA")
    await say(interaction, embed=emb)

for _cmd in (resetuser_cmd, resetall_cmd, pointslist_cmd):
    if not bot.tree.get_command(_cmd.name):
        bot.tree.add_command(_cmd)


class ConfirmFactoryResetView(discord.ui.View):
    def __init__(self, author_id: int, guild_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.guild_id = guild_id

    @discord.ui.button(label="تأكيد مسح كل شيء نهائياً (Factory Reset)", style=discord.ButtonStyle.danger, emoji="💣")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await say(interaction, "❌ الزرار ده للي كتب الأمر بس.", ephemeral=True)
            return
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for tbl in tables:
            try:
                cols = [c[1] for c in db.execute(f"PRAGMA table_info({tbl})").fetchall()]
                if "guild_id" in cols:
                    db.execute(f"DELETE FROM {tbl} WHERE guild_id=?", (self.guild_id,))
                elif tbl == "sqlite_sequence":
                    db.execute("DELETE FROM sqlite_sequence")
            except Exception:
                pass
        db.commit()
        await interaction.response.edit_message(
            content="💣 **تم مسح كل داتا البوت بالكامل (النقاط، المنتجات، الطلبات، الورنات، الفويسات، والإعدادات) ورجع كأنه جديد تماماً!**",
            view=None
        )

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await say(interaction, "❌ الزرار ده للي كتب الأمر بس.", ephemeral=True)
            return
        await interaction.response.edit_message(content="❎ تم إلغاء عملية التصفير الشامل.", view=None)


@app_commands.command(name="factoryreset", description="[إدارة] مسح كل داتا البوت بالكامل (منتجات، نقاط، ورنات، إعدادات) كأنه جديد")
@app_commands.default_permissions(administrator=True)
async def factoryreset_cmd(interaction: discord.Interaction):
    await say(
        interaction,
        "💣 **تحذير خطير:** الأمر ده هيمسح **كل حاجة** في البوت (النقاط، الساعات، منتجات المتجر، الطلبات، التحذيرات، الرومات المتتبعة، وكل الإعدادات) وهيرجّع البوت أبيض من الصفر! متأكد؟",
        ephemeral=True,
        view=ConfirmFactoryResetView(interaction.user.id, interaction.guild_id)
    )

if not bot.tree.get_command(factoryreset_cmd.name):
    bot.tree.add_command(factoryreset_cmd)
