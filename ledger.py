"""النقاط والفلوس: الرصيد، المشتريات، المدفوع والمتبقي، التقارير، وقفل الفترة"""
import csv
import io
import time
from datetime import datetime

import discord

from core import C_MAIN, cfg_int, cfg_set, db, emb, fmt_dur, fmt_num, get_settings


def period_no(gid: int) -> int:
    return cfg_int(gid, "period", 1)


def ensure_user(gid: int, uid: int) -> None:
    db.execute("INSERT OR IGNORE INTO users(guild_id, user_id) VALUES (?, ?)", (gid, uid))


def credit(gid: int, uid: int, seconds: float, points: float) -> None:
    ensure_user(gid, uid)
    db.execute(
        "UPDATE users SET seconds=seconds+?, earned=earned+? WHERE guild_id=? AND user_id=?",
        (seconds, points, gid, uid),
    )


def adjust_points(gid: int, uid: int, amount: float, by_id: int, reason: str) -> None:
    ensure_user(gid, uid)
    db.execute("UPDATE users SET adjust=adjust+? WHERE guild_id=? AND user_id=?", (amount, gid, uid))
    db.execute(
        "INSERT INTO adjustments(guild_id,user_id,by_id,amount,reason,ts) VALUES (?,?,?,?,?,?)",
        (gid, uid, by_id, amount, reason, time.time()),
    )
    db.commit()


def spend_points(gid: int, uid: int, amount: float) -> None:
    ensure_user(gid, uid)
    db.execute("UPDATE users SET spent=spent+? WHERE guild_id=? AND user_id=?", (amount, gid, uid))
    db.commit()


def refund_points(gid: int, uid: int, amount: float) -> None:
    ensure_user(gid, uid)
    db.execute("UPDATE users SET spent=MAX(0, spent-?) WHERE guild_id=? AND user_id=?", (amount, gid, uid))
    db.commit()


def paid_this_period(gid: int, uid: int) -> float:
    r = db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM payouts WHERE guild_id=? AND user_id=? AND period=?",
        (gid, uid, period_no(gid)),
    ).fetchone()
    return r["s"]


def get_carry(gid: int, uid: int) -> float:
    r = db.execute("SELECT carry FROM balances WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()
    return r["carry"] if r else 0.0


def get_user(gid: int, uid: int) -> dict:
    r = db.execute("SELECT * FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()
    seconds, earned, adjust, spent = (r["seconds"], r["earned"], r["adjust"], r["spent"]) if r else (0.0,) * 4
    total = earned + adjust
    avail = total - spent
    pv = get_settings(gid)["point_value"]
    value = avail * pv
    paid = paid_this_period(gid, uid)
    carry = get_carry(gid, uid)
    return {
        "seconds": seconds, "earned": earned, "adjust": adjust, "spent": spent,
        "total": total, "avail": avail, "value": value, "paid": paid, "carry": carry,
        "remaining": carry + value - paid,
    }


def add_payout(gid: int, uid: int, by_id: int, amount: float, note: str) -> int:
    cur = db.execute(
        "INSERT INTO payouts(guild_id,user_id,by_id,amount,note,period,ts) VALUES (?,?,?,?,?,?,?)",
        (gid, uid, by_id, amount, note, period_no(gid), time.time()),
    )
    db.commit()
    return cur.lastrowid


def leaderboard(gid: int) -> list:
    return db.execute(
        "SELECT user_id, seconds, earned, adjust, earned+adjust AS total "
        "FROM users WHERE guild_id=? ORDER BY seconds DESC",
        (gid,),
    ).fetchall()


# ------------------------------------------------------------------ التقارير وقفل الفترة
def report_rows(gid: int) -> list[dict]:
    ids = {r["user_id"] for r in db.execute("SELECT user_id FROM users WHERE guild_id=?", (gid,))}
    ids |= {r["user_id"] for r in db.execute(
        "SELECT user_id FROM balances WHERE guild_id=? AND ABS(carry)>0.004", (gid,))}
    ids |= {r["user_id"] for r in db.execute(
        "SELECT user_id FROM payouts WHERE guild_id=? AND period=?", (gid, period_no(gid)))}
    rows = []
    for uid in ids:
        d = get_user(gid, uid)
        d["user_id"] = uid
        rows.append(d)
    rows.sort(key=lambda d: d["total"], reverse=True)
    return rows


def csv_from_rows(guild: discord.Guild, rows: list[dict]) -> discord.File:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "hours", "voice_points", "manual_adjust", "spent_points",
                "available_points", "money_value", "paid", "previous_balance", "remaining"])
    for r in rows:
        m = guild.get_member(r["user_id"])
        w.writerow([
            r["user_id"], m.display_name if m else "left server", round(r["seconds"] / 3600, 2),
            round(r["earned"], 2), round(r["adjust"], 2), round(r["spent"], 2), round(r["avail"], 2),
            round(r["value"], 2), round(r["paid"], 2), round(r["carry"], 2), round(r["remaining"], 2),
        ])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # utf-8-sig عشان الإكسيل يقرا العربي
    return discord.File(data, filename=f"report-{datetime.now():%Y-%m-%d}.csv")


def apply_close(guild: discord.Guild, rows: list[dict]) -> None:
    """بيرحّل المتبقي (فلوس) للفترة الجاية، ويصفّر النقاط والساعات."""
    gid = guild.id
    for r in rows:
        carry = round(r["remaining"], 4)
        if abs(carry) < 0.005:
            db.execute("DELETE FROM balances WHERE guild_id=? AND user_id=?", (gid, r["user_id"]))
        else:
            db.execute(
                "INSERT INTO balances(guild_id,user_id,carry) VALUES (?,?,?) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET carry=excluded.carry",
                (gid, r["user_id"], carry),
            )
    db.execute("DELETE FROM users WHERE guild_id=?", (gid,))
    db.commit()
    cfg_set(gid, "period", period_no(gid) + 1)


def report_embed(guild: discord.Guild, rows: list[dict], title: str = "📄 تقرير الفترة") -> discord.Embed:
    e = emb(title, color=C_MAIN)
    e.add_field(name="الأعضاء", value=str(len(rows)))
    e.add_field(name="إجمالي الوقت", value=fmt_dur(sum(r["seconds"] for r in rows)))
    e.add_field(name="إجمالي النقاط", value=fmt_num(sum(r["total"] for r in rows)))
    pv = get_settings(guild.id)["point_value"]
    if pv > 0 or any(r["paid"] or r["carry"] for r in rows):
        e.add_field(name="القيمة المالية", value=fmt_num(sum(r["value"] for r in rows)))
        e.add_field(name="المدفوع", value=fmt_num(sum(r["paid"] for r in rows)))
        e.add_field(name="المتبقي", value=fmt_num(sum(r["remaining"] for r in rows)))
    top = [f"`{i}.` <@{r['user_id']}> — **{fmt_num(r['total'])}** نقطة ({fmt_dur(r['seconds'])})"
           for i, r in enumerate(rows[:10], 1)]
    if top:
        e.add_field(name="🏆 الأعلى", value="\n".join(top), inline=False)
    return e


def user_embed(guild: discord.Guild, member) -> discord.Embed:
    s = get_settings(guild.id)
    u = get_user(guild.id, member.id)
    board = leaderboard(guild.id)
    rank = next((i for i, r in enumerate(board, 1) if r["user_id"] == member.id), None)
    e = emb(f"📈 {member.display_name}")
    e.add_field(name="الوقت", value=fmt_dur(u["seconds"]))
    e.add_field(name="نقاط الفويس", value=fmt_num(u["earned"]))
    e.add_field(name="تعديلات يدوية", value=f"{u['adjust']:+,.2f}")
    if u["spent"]:
        e.add_field(name="اتصرف في المتجر", value=fmt_num(u["spent"]))
    e.add_field(name="الرصيد المتاح", value=f"**{fmt_num(u['avail'])}**")
    if rank:
        e.add_field(name="الترتيب", value=f"#{rank}")
    if s["point_value"] > 0 or u["paid"] or u["carry"]:
        e.add_field(name="المقابل المادي", value=fmt_num(u["value"]))
        if u["carry"]:
            e.add_field(name="رصيد سابق", value=f"{u['carry']:+,.2f}")
        e.add_field(name="المدفوع", value=fmt_num(u["paid"]))
        e.add_field(name="المتبقي", value=f"**{fmt_num(u['remaining'])}**")
    return e
