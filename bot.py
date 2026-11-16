from core import db
import discord
"""نقطة التشغيل: python bot.py

الملفات:
  core.py     الأساس (قاعدة البيانات، البوت، اللوج، أدوات)
  ledger.py   النقاط والفلوس والمدفوع والمتبقي والتقارير
  rates.py    البوستات والمعدلات حسب الفويس/الرول والأشخاص المحسوبين
  voice.py    ستارت/ستوب وحساب الفويس والإعدادات
  economy.py  أوامر النقاط والمدفوعات والتقارير (يدوي + تلقائي)
  shop.py     المتجر والطلبات
  tickets.py  التيكيتات
  warns.py    الورنات
  embeds.py   أدوات الإمبيد
  logs.py     اللوجات الشاملة
  panel.py    لوحة التحكم الإمبيد
"""
from core import TOKEN, bot

# الاستيراد بيسجّل الأوامر
import rates      # noqa: F401
import voice      # noqa: F401
import economy    # noqa: F401
import shop       # noqa: F401
import tickets    # noqa: F401
import warns      # noqa: F401
import embeds     # noqa: F401
import logs       # noqa: F401
import panel      # noqa: F401

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("حط DISCORD_TOKEN في ملف .env")
    

# === نظام لوجات أوامر البوت ===
@discord.app_commands.command(name="cmdlog", description="[إدارة] تحديد روم لوجات أوامر البوت")
@discord.app_commands.default_permissions(administrator=True)
@discord.app_commands.describe(channel="الروم اللي هيتبعت فيها لوج كل أوامر البوت (سيبها فاضية لإيقاف اللوج)")
async def cmdlog_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not interaction.guild_id:
        return
    if channel:
        db.execute("INSERT OR REPLACE INTO config(guild_id, key, value) VALUES (?, 'cmd_log_channel', ?)", (interaction.guild_id, str(channel.id)))
        db.commit()
        await interaction.response.send_message(f"✅ تم تفعيل لوجات أوامر البوت في {channel.mention}.", ephemeral=True)
    else:
        db.execute("DELETE FROM config WHERE guild_id=? AND key='cmd_log_channel'", (interaction.guild_id,))
        db.commit()
        await interaction.response.send_message("🔕 تم إيقاف لوجات أوامر البوت.", ephemeral=True)

if not bot.tree.get_command(cmdlog_cmd.name):
    bot.tree.add_command(cmdlog_cmd)

@bot.listen("on_app_command_completion")
async def _log_all_slash_commands(interaction: discord.Interaction, command):
    if not interaction.guild_id or not interaction.guild:
        return
    row = db.execute("SELECT value FROM config WHERE guild_id=? AND key='cmd_log_channel'", (interaction.guild_id,)).fetchone()
    if not row or not row[0]:
        return
    ch = interaction.guild.get_channel(int(row[0]))
    if not ch:
        return
    opts = []
    for opt in interaction.data.get("options", []):
        if opt.get("type") in (1, 2):
            for sub in opt.get("options", []):
                opts.append(f"`{sub.get('name')}`: {sub.get('value')}")
        else:
            opts.append(f"`{opt.get('name')}`: {opt.get('value')}")
    emb = discord.Embed(
        title=f"📜 تنفيذ أمر: /{command.qualified_name}",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    emb.add_field(name="العضو", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
    emb.add_field(name="الروم", value=interaction.channel.mention if interaction.channel else "DM", inline=True)
    if opts:
        emb.add_field(name="الخيارات", value=" | ".join(opts)[:1000], inline=False)
    emb.set_footer(text="Dev by DAWLA")
    try:
        await ch.send(embed=emb)
    except Exception:
        pass

bot.run(TOKEN)
