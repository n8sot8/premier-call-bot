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

# Weekly schedule (0=Mon .. 6=Sun)
WEEKLY_DAY = int(os.getenv("WEEKLY_DAY", "6"))       # default: Sunday
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "20"))    # default: 20:00
WEEKLY_MINUTE = int(os.getenv("WEEKLY_MINUTE", "0"))

# DB (Flyで永続化したいなら /data/bot.db を推奨)
DB_PATH = os.getenv("DB_PATH", "bot.db")

# =========================
# Discord client / commands
# =========================
# ボタン/セレクト + スラコマだけなら privileged intents不要
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
            kind TEXT NOT NULL,      -- 'tenko' or 'weekly'
            created_at TEXT NOT NULL
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY(message_id, user_id)
        );
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_votes (
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day INTEGER NOT NULL,    -- 0=Mon..6=Sun
            PRIMARY KEY(message_id, user_id, day)
        );
        """)

def now_jst():
    return datetime.datetime.now(JST)

# =========================
# Helpers (format)
# =========================
JP_DAYS = ["月", "火", "水", "木", "金", "土", "日"]

def format_tenko(message_id: int) -> str:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM attendance WHERE message_id=? ORDER BY user_id ASC",
            (message_id,)
        ).fetchall()
    uids = [r[0] for r in rows]
    mentions = " ".join([f"<@{uid}>" for uid in uids]) if uids else "まだ誰も押してない"
    if len(mentions) > 1500:
        mentions = mentions[:1500] + " …"
    return f"🕔 **点呼（参加）**\n参加: **{len(uids)}**\n{mentions}"

def format_weekly_table(message_id: int) -> str:
    with db() as conn:
        rows = conn.execute(
            "SELECT day, user_id FROM weekly_votes WHERE message_id=? ORDER BY day ASC",
            (message_id,)
        ).fetchall()

    by_day = {d: [] for d in range(7)}
    for day, uid in rows:
        by_day[int(day)].append(int(uid))

    counts = {d: len(by_day[d]) for d in range(7)}
    best = max(counts.values()) if counts else 0
    best_days = [d for d, c in counts.items() if c == best and best > 0]

    lines = []
    lines.append("📅 **出席できそうな曜日（集計）**")
    if best_days:
        lines.append("✅ **最多:** " + " / ".join([f"{JP_DAYS[d]}({counts[d]})" for d in best_days]))
    else:
        lines.append("✅ **最多:** まだ投票なし")
    lines.append("")
    lines.append("| 曜日 | 人数 | メンバー |")
    lines.append("|---|---:|---|")
    for d in range(7):
        members = " ".join([f"<@{uid}>" for uid in by_day[d]]) or "-"
        if len(members) > 1200:
            members = members[:1200] + " …"
        lines.append(f"| {JP_DAYS[d]} | {counts[d]} | {members} |")

    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n…（長いので省略）"
    return text

def guild_obj():
    return discord.Object(id=GUILD_ID) if GUILD_ID else None

# =========================
# Views
# =========================
class TenkoView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ 参加/取消", style=discord.ButtonStyle.success, custom_id="tenko_toggle")
    async def toggle(self, interaction: discord.Interaction, _button: discord.ui.Button):
        mid = interaction.message.id
        uid = interaction.user.id

        with db() as conn:
            exists = conn.execute(
                "SELECT 1 FROM attendance WHERE message_id=? AND user_id=?",
                (mid, uid)
            ).fetchone()

            if exists:
                conn.execute("DELETE FROM attendance WHERE message_id=? AND user_id=?", (mid, uid))
                msg = "取消したよ"
            else:
                conn.execute("INSERT OR IGNORE INTO attendance(message_id,user_id) VALUES(?,?)", (mid, uid))
                msg = "参加にしたよ"

        await interaction.response.defer(thinking=False)
        await interaction.message.edit(content=format_tenko(mid), view=TenkoView())
        await interaction.followup.send(msg, ephemeral=True)

class WeeklySelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=f"{JP_DAYS[i]}曜日", value=str(i))
            for i in range(7)
        ]
        super().__init__(
            placeholder="出席できそうな曜日を選んでね（複数OK）",
            min_values=0,
            max_values=7,
            options=options,
            custom_id="weekly_select"
        )

    async def callback(self, interaction: discord.Interaction):
        mid = interaction.message.id
        uid = interaction.user.id
        chosen = {int(v) for v in self.values}

        with db() as conn:
            # いったん全削除して入れ直し（簡単&安全）
            conn.execute("DELETE FROM weekly_votes WHERE message_id=? AND user_id=?", (mid, uid))
            for d in chosen:
                conn.execute(
                    "INSERT OR IGNORE INTO weekly_votes(message_id,user_id,day) VALUES(?,?,?)",
                    (mid, uid, d)
                )

        await interaction.response.defer(thinking=False)
        await interaction.message.edit(content=format_weekly_table(mid), view=WeeklyView())
        await interaction.followup.send("更新したよ！", ephemeral=True)

class WeeklyView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(WeeklySelect())

# =========================
# Post helpers
# =========================
async def post_tenko(channel: discord.abc.Messageable) -> int:
    view = TenkoView()
    msg = await channel.send("🕔 点呼を始めるよ！✅で参加してね", view=view)
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(message_id,kind,created_at) VALUES(?,?,?)",
            (msg.id, "tenko", now_jst().isoformat())
        )
    await msg.edit(content=format_tenko(msg.id), view=view)
    return msg.id

async def post_weekly(channel: discord.abc.Messageable) -> int:
    view = WeeklyView()
    msg = await channel.send("📅 曜日アンケを始めるよ（複数選択OK）", view=view)
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(message_id,kind,created_at) VALUES(?,?,?)",
            (msg.id, "weekly", now_jst().isoformat())
        )
    await msg.edit(content=format_weekly_table(msg.id), view=view)
    return msg.id

def next_weekly_run() -> datetime.datetime:
    now = now_jst()
    # 今週のターゲット時刻
    days_ahead = (WEEKLY_DAY - now.weekday()) % 7
    target = now.replace(hour=WEEKLY_HOUR, minute=WEEKLY_MINUTE, second=0, microsecond=0) + datetime.timedelta(days=days_ahead)
    if target <= now:
        target += datetime.timedelta(days=7)
    return target

# =========================
# Scheduled tasks
# =========================
@tasks.loop(minutes=1)
async def daily_rollcall_loop():
    # 毎日 17:00 に CHANNEL_ID へ点呼を投げる
    if not CHANNEL_ID:
        return

    now = now_jst()
    if now.hour == 17 and now.minute == 0:
        channel = await client.fetch_channel(CHANNEL_ID)
        await post_tenko(channel)

@tasks.loop(seconds=5)
async def weekly_poll_loop():
    # 起動後は自分でsleepして週1で実行（無駄に回さない）
    weekly_poll_loop.stop()

    if not CHANNEL_ID:
        print("[weekly] CHANNEL_ID not set; weekly poll disabled")
        return

    channel = await client.fetch_channel(CHANNEL_ID)

    while True:
        target = next_weekly_run()
        wait = (target - now_jst()).total_seconds()
        print(f"[weekly] next run at {target.isoformat()} (in {wait:.0f}s)")
        await asyncio.sleep(max(1, wait))
        await post_weekly(channel)

# =========================
# Slash commands
# =========================
@tree.command(name="tenko", description="点呼を開始（✅で参加）", guild=guild_obj())
async def tenko_cmd(interaction: discord.Interaction):
    mid = await post_tenko(interaction.channel)
    await interaction.response.send_message(f"点呼出した！(message_id={mid})", ephemeral=True)

@tree.command(name="yobi", description="週の出席できそうな曜日アンケを開始", guild=guild_obj())
async def yobi_cmd(interaction: discord.Interaction):
    mid = await post_weekly(interaction.channel)
    await interaction.response.send_message(f"曜日アンケ出した！(message_id={mid})", ephemeral=True)

@tree.command(name="shukei", description="集計メッセージを更新する（message_id指定も可）", guild=guild_obj())
@app_commands.describe(message_id="更新したいメッセージID（空なら直近の投票/点呼）")
async def shukei_cmd(interaction: discord.Interaction, message_id: str = ""):
    mid = int(message_id) if message_id.strip() else None

    if not mid:
        with db() as conn:
            row = conn.execute(
                "SELECT message_id, kind FROM meta ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return await interaction.response.send_message("集計対象が見つからない…", ephemeral=True)
        mid, kind = int(row[0]), row[1]
    else:
        with db() as conn:
            row = conn.execute("SELECT kind FROM meta WHERE message_id=?", (mid,)).fetchone()
        kind = row[0] if row else "weekly"

    try:
        msg = await interaction.channel.fetch_message(mid)
    except Exception:
        return await interaction.response.send_message("そのmessage_id、このチャンネルで取れなかった…", ephemeral=True)

    if kind == "tenko":
        await msg.edit(content=format_tenko(mid), view=TenkoView())
    else:
        await msg.edit(content=format_weekly_table(mid), view=WeeklyView())

    await interaction.response.send_message("更新した！", ephemeral=True)

# =========================
# Lifecycle
# =========================
@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")

    # 永続ボタン/セレクト（再起動しても効く）
    client.add_view(TenkoView())
    client.add_view(WeeklyView())

    # スラコマ同期（GUILD_IDあり＝即反映）
    if GUILD_ID:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print("Synced commands to guild")
    else:
        await tree.sync()
        print("Synced commands globally (may take time)")

    if not daily_rollcall_loop.is_running():
        daily_rollcall_loop.start()

    if not weekly_poll_loop.is_running():
        weekly_poll_loop.start()

def wait_env():
    # Flyの反映遅延対策（最大10秒待つ）
    for _ in range(10):
        if os.getenv("DISCORD_TOKEN"):
            return
        time.sleep(1)

if __name__ == "__main__":
    wait_env()
    init_db()

    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN がないよ（Fly Secrets を確認して）")
    if not CHANNEL_ID:
        print("CHANNEL_ID がないので自動投稿は無効（/tenko や /yobi は使える）")

    client.run(TOKEN)

@client.event
async def on_ready():
    print(f"READY: {client.user}")

    # スラッシュコマンドを「このサーバー」に同期（即反映）
    if GUILD_ID:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print("Synced commands to guild")
    else:
        await tree.sync()
        print("Synced commands globally")
