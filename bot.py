import os
import time
import asyncio
import sqlite3
import datetime
import pytz

import discord
from discord import app_commands
from discord.ext import tasks
from discord.ui import View, Select

# =========================
# Env
# =========================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0")) or None
TZ = os.getenv("TZ", "Asia/Tokyo")

MENTION_ROLE_ID = int(os.getenv("MENTION_ROLE_ID", "0")) or None

DAILY_TENKO_HOUR = int(os.getenv("DAILY_TENKO_HOUR", "17"))
DAILY_TENKO_MINUTE = int(os.getenv("DAILY_TENKO_MINUTE", "0"))

WEEKLY_POST_DAY = int(os.getenv("WEEKLY_DAY", "6"))
WEEKLY_POST_HOUR = int(os.getenv("WEEKLY_HOUR", "20"))
WEEKLY_POST_MINUTE = int(os.getenv("WEEKLY_MINUTE", "0"))

# 締切（デフォルト：1時間）
TENKO_CLOSE_MIN = int(os.getenv("TENKO_CLOSE_MIN", "60"))
WEEKLY_CLOSE_HOURS = int(os.getenv("WEEKLY_CLOSE_HOURS", "1"))
REMIND_BEFORE_MIN = int(os.getenv("REMIND_BEFORE_MIN", "10"))

DB_PATH = os.getenv("DB_PATH", "/data/bot.db")

# =========================
# Discord
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
JST = pytz.timezone(TZ)

# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            message_id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            close_at TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            reminded INTEGER NOT NULL DEFAULT 0
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,      -- 'yes' or 'no'
            PRIMARY KEY(message_id, user_id)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_votes (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day INTEGER NOT NULL,
            PRIMARY KEY(message_id, user_id, day)
        );
        """)

def now_jst():
    return datetime.datetime.now(JST)

def iso(dt):
    return dt.isoformat() if dt else None

def parse_iso(s):
    return datetime.datetime.fromisoformat(s) if s else None

# =========================
# Helpers
# =========================
JP_DAYS = ["月","火","水","木","金","土","日"]

def mention_prefix():
    return f"<@&{MENTION_ROLE_ID}>\n" if MENTION_ROLE_ID else ""

def guild_obj():
    return discord.Object(id=GUILD_ID) if GUILD_ID else None

def safe_mentions(uids, limit_chars=1800):
    if not uids:
        return "（なし）"
    s = " ".join(f"<@{uid}>" for uid in uids)
    return s if len(s) <= limit_chars else s[:limit_chars] + " …"

# =========================
# Embeds
# =========================
def tenko_embed(message_id, closed=False, close_at=None):
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, status FROM attendance WHERE message_id=? ORDER BY user_id",
            (message_id,)
        ).fetchall()

    yes = [int(uid) for uid, s in rows if s == "yes"]
    no  = [int(uid) for uid, s in rows if s == "no"]

    e = discord.Embed(
        title="🕔 点呼",
        description="下のボタンで **参加 / 不参加** を選んでね"
    )
    e.add_field(name=f"✅ 参加（{len(yes)}人）", value=safe_mentions(yes), inline=False)
    e.add_field(name=f"❌ 不参加（{len(no)}人）", value=safe_mentions(no), inline=False)

    if close_at:
        e.add_field(name="締切", value=f"{close_at.strftime('%Y/%m/%d %H:%M')}（{TZ}）", inline=False)

    e.set_footer(text="締切済み（操作不可）" if closed else "どちらかを選択してね")
    return e

def weekly_counts(message_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT day, user_id FROM weekly_votes WHERE message_id=?",
            (message_id,)
        ).fetchall()
    by_day = {d: [] for d in range(7)}
    for d, uid in rows:
        by_day[int(d)].append(int(uid))
    counts = {d: len(by_day[d]) for d in range(7)}
    best = max(counts.values()) if counts else 0
    best_days = [d for d,c in counts.items() if c == best and best > 0]
    return by_day, counts, best_days

def next_occurrence_of_weekday(target_weekday, hour, minute):
    now = now_jst()
    days = (target_weekday - now.weekday()) % 7
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + datetime.timedelta(days=days)
    if t <= now:
        t += datetime.timedelta(days=7)
    return t

def weekly_embed(message_id, closed=False, close_at=None):
    by_day, counts, best_days = weekly_counts(message_id)

    if best_days:
        best_text = " / ".join(f"{JP_DAYS[d]}({counts[d]})" for d in best_days)
        next_dt = next_occurrence_of_weekday(best_days[0], WEEKLY_POST_HOUR, WEEKLY_POST_MINUTE)
        next_text = f"{next_dt.strftime('%Y/%m/%d')}（{JP_DAYS[next_dt.weekday()]}） {next_dt.strftime('%H:%M')}（{TZ}）"
    else:
        best_text = "まだ投票なし"
        next_text = "未定"

    e = discord.Embed(
        title="📅 出席できそうな曜日（アンケ）",
        description="複数選択OK"
    )
    e.add_field(name="✅ 最多", value=best_text, inline=False)
    e.add_field(name="🗓 次回候補", value=next_text, inline=False)

    lines = []
    for d in range(7):
        lines.append(f"**{JP_DAYS[d]}：{counts[d]}人**\n{safe_mentions(by_day[d], 700)}")
    e.add_field(name="集計", value="\n\n".join(lines), inline=False)

    if close_at:
        e.add_field(name="締切", value=f"{close_at.strftime('%Y/%m/%d %H:%M')}（{TZ}）", inline=False)

    e.set_footer(text="締切済み（操作不可）" if closed else "選択後に閉じてOK")
    return e

# =========================
# Views
# =========================
class TenkoView(View):
    def __init__(self, disabled=False):
        super().__init__(timeout=None)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = disabled

    async def set_status(self, interaction, status):
        mid = interaction.message.id
        uid = interaction.user.id

        with db() as conn:
            row = conn.execute("SELECT status FROM meta WHERE message_id=?", (mid,)).fetchone()
        if row and row[0] == "closed":
            return await interaction.response.send_message("この点呼は締切済み！", ephemeral=True)

        await interaction.response.defer(thinking=False)

        try:
            with db() as conn:
                cur = conn.execute(
                    "UPDATE attendance SET status=? WHERE message_id=? AND user_id=?",
                    (status, mid, uid)
                )
                if cur.rowcount == 0:
                    conn.execute(
                        "INSERT INTO attendance(message_id,user_id,status) VALUES(?,?,?)",
                        (mid, uid, status)
                    )
                row2 = conn.execute("SELECT close_at FROM meta WHERE message_id=?", (mid,)).fetchone()
                close_at = parse_iso(row2[0]) if row2 else None

            await interaction.message.edit(
                embed=tenko_embed(mid, closed=False, close_at=close_at),
                view=TenkoView(False)
            )
            await interaction.followup.send(
                "参加にしたよ！" if status == "yes" else "不参加にしたよ！",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"エラー: {e}", ephemeral=True)

    @discord.ui.button(label="✅ 参加", style=discord.ButtonStyle.success, custom_id="tenko_yes")
    async def yes(self, interaction, _):
        await self.set_status(interaction, "yes")

    @discord.ui.button(label="❌ 不参加", style=discord.ButtonStyle.danger, custom_id="tenko_no")
    async def no(self, interaction, _):
        await self.set_status(interaction, "no")

class WeeklySelect(Select):
    def __init__(self, disabled=False):
        options = [discord.SelectOption(label=f"{JP_DAYS[i]}曜日", value=str(i)) for i in range(7)]
        super().__init__(
            placeholder="出席できそうな曜日（複数OK）",
            min_values=0, max_values=7,
            options=options,
            custom_id="weekly_select",
            disabled=disabled
        )

    async def callback(self, interaction):
        mid = interaction.message.id
        uid = interaction.user.id

        with db() as conn:
            row = conn.execute("SELECT status, close_at FROM meta WHERE message_id=?", (mid,)).fetchone()
        if row and row[0] == "closed":
            return await interaction.response.send_message("このアンケは締切済み！", ephemeral=True)

        await interaction.response.defer(thinking=False)

        chosen = {int(v) for v in self.values}
        with db() as conn:
            conn.execute("DELETE FROM weekly_votes WHERE message_id=? AND user_id=?", (mid, uid))
            for d in chosen:
                conn.execute(
                    "INSERT OR IGNORE INTO weekly_votes(message_id,user_id,day) VALUES(?,?,?)",
                    (mid, uid, d)
                )
            close_at = parse_iso(conn.execute(
                "SELECT close_at FROM meta WHERE message_id=?", (mid,)
            ).fetchone()[0])

        await interaction.message.edit(
            embed=weekly_embed(mid, False, close_at),
            view=WeeklyView(False)
        )
        await interaction.followup.send("更新したよ！", ephemeral=True)

class WeeklyView(View):
    def __init__(self, disabled=False):
        super().__init__(timeout=None)
        self.add_item(WeeklySelect(disabled))

# =========================
# Posting / Closing
# =========================
async def post_tenko(channel):
    close_at = now_jst() + datetime.timedelta(minutes=TENKO_CLOSE_MIN) if TENKO_CLOSE_MIN > 0 else None
    msg = await channel.send(
        content=mention_prefix() + "🕔 点呼を始めるよ！",
        embed=tenko_embed(0, False, close_at),
        view=TenkoView(False)
    )
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES(?,?,?,?,?,?,?)",
            (msg.id, "tenko", msg.channel.id, iso(now_jst()), iso(close_at), "open", 0)
        )
    await msg.edit(embed=tenko_embed(msg.id, False, close_at), view=TenkoView(False))
    return msg.id

async def post_weekly(channel):
    close_at = now_jst() + datetime.timedelta(hours=WEEKLY_CLOSE_HOURS) if WEEKLY_CLOSE_HOURS > 0 else None
    msg = await channel.send(
        content=mention_prefix() + "📅 曜日アンケを始めるよ",
        embed=weekly_embed(0, False, close_at),
        view=WeeklyView(False)
    )
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta VALUES(?,?,?,?,?,?,?)",
            (msg.id, "weekly", msg.channel.id, iso(now_jst()), iso(close_at), "open", 0)
        )
    await msg.edit(embed=weekly_embed(msg.id, False, close_at), view=WeeklyView(False))
    return msg.id

async def close_poll(message_id):
    with db() as conn:
        row = conn.execute(
            "SELECT kind, channel_id, close_at, status FROM meta WHERE message_id=?",
            (message_id,)
        ).fetchone()
    if not row or row[3] == "closed":
        return

    kind, channel_id, close_at_s, _ = row
    close_at = parse_iso(close_at_s)

    channel = await client.fetch_channel(int(channel_id))
    msg = await channel.fetch_message(int(message_id))

    if kind == "tenko":
        await msg.edit(embed=tenko_embed(message_id, True, close_at), view=TenkoView(True))
        await channel.send("✅ 点呼を締め切ったよ！")
    else:
        await msg.edit(embed=weekly_embed(message_id, True, close_at), view=WeeklyView(True))

    with db() as conn:
        conn.execute("UPDATE meta SET status='closed' WHERE message_id=?", (message_id,))

# =========================
# Loops
# =========================
@tasks.loop(minutes=1)
async def daily_rollcall_loop():
    if not CHANNEL_ID:
        return
    now = now_jst()
    if now.hour == DAILY_TENKO_HOUR and now.minute == DAILY_TENKO_MINUTE:
        await post_tenko(await client.fetch_channel(CHANNEL_ID))

@tasks.loop(seconds=20)
async def closer_loop():
    now = now_jst()
    with db() as conn:
        rows = conn.execute(
            "SELECT message_id, channel_id, close_at, reminded FROM meta WHERE status='open' AND close_at IS NOT NULL"
        ).fetchall()
    for mid, cid, close_at_s, reminded in rows:
        close_at = parse_iso(close_at_s)
        if REMIND_BEFORE_MIN > 0 and not reminded:
            if close_at - datetime.timedelta(minutes=REMIND_BEFORE_MIN) <= now < close_at:
                await (await client.fetch_channel(cid)).send("⏰ もうすぐ締切！")
                with db() as conn:
                    conn.execute("UPDATE meta SET reminded=1 WHERE message_id=?", (mid,))
        if now >= close_at:
            await close_poll(mid)

# =========================
# Slash commands
# =========================
@tree.command(name="tenko", description="点呼を開始", guild=guild_obj())
async def tenko_cmd(interaction):
    mid = await post_tenko(interaction.channel)
    await interaction.response.send_message(f"点呼出した！（{mid}）", ephemeral=True)

@tree.command(name="yobi", description="曜日アンケ開始", guild=guild_obj())
async def yobi_cmd(interaction):
    mid = await post_weekly(interaction.channel)
    await interaction.response.send_message(f"アンケ出した！（{mid}）", ephemeral=True)

# =========================
# Lifecycle
# =========================
@client.event
async def on_ready():
    print(f"READY: {client.user}")
    client.add_view(TenkoView(False))
    client.add_view(WeeklyView(False))
    if GUILD_ID:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
    else:
        await tree.sync()
    if not daily_rollcall_loop.is_running():
        daily_rollcall_loop.start()
    if not closer_loop.is_running():
        closer_loop.start()

def wait_env():
    for _ in range(10):
        if os.getenv("DISCORD_TOKEN"):
            return
        time.sleep(1)

if __name__ == "__main__":
    wait_env()
    init_db()
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN がありません")
    client.run(TOKEN)
