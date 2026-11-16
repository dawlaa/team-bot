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
    bot.run(TOKEN)
