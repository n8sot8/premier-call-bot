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

# メンションしたいロール（任意）
MENTION_ROLE_ID = int(os.getenv("MENTION_ROLE_ID", "0")) or None

# 毎日点呼（固定）
DAILY_TENKO_HOUR = int(os.getenv("DAILY_TENKO_HOUR", "17"))
DAILY_TENKO_MINUTE = int(os.getenv("DAILY_TENKO_MINUTE", "0"))

# 週一アンケ投稿の基準（0=Mon..6=Sun）
WEEKLY_POST_DAY = int(os.getenv("WEEKLY_DAY", "6"))       # default: Sunday
WEEKLY_POST_HOUR = int(os.getenv("WEEKLY_HOUR", "12"))    # default: 20:00
WEEKLY_POST_MINUTE = int(os.getenv("WEEKLY_MINUTE", "0"))

# 締切関連
TENKO_CLOSE_MIN = int(os.getenv("TENKO_CLOSE_MIN", "0"))         # 0なら締切なし（任意）
WEEKLY_CLOSE_HOURS = int(os.getenv("WEEKLY_CLOSE_HOURS", "48"))  # 週一アンケは48h後に締切
REMIND_BEFORE_MIN = int(os.getenv("REMIND_BEFORE_MIN", "60"))    # 締切60分前にリマインド（任意）

# DB（Flyで永続化したいなら /data/bot.db 推奨）
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")

# =========================
# Discord client / commands
# =========================
# スラコマ + コンポーネントだけなら privileged intents なくてOK
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
            kind TEXT NOT NULL,              -- 'tenko' or 'weekly'
            channel_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            close_at TEXT,                   -- ISO
            status TEXT NOT NULL DEFAULT 'open',   -- open/closed
            reminded INTEGER NOT NULL DEFAULT 0    -- 0/1
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

def iso(dt: datetime.datetime | None):
    return dt.isoformat() if dt else None

def parse_iso(s: str | None):
    if not s:
        return None
    return datetime.datetime.fromisoformat(s)

# =========================
# Helpers
# =========================
JP_DAYS = ["月", "火", "水", "木", "金", "土", "日"]

def mention_prefix() -> str:
    # ロール指定がある時だけメンション（開始投稿だけ）
    if MENTION_ROLE_ID:
        return f"<@&{MENTION_ROLE_ID}>\n"
    return ""

def guild_obj():
    return discord.Object(id=GUILD_ID) if GUILD_ID else None

def safe_mentions(uids: list[int], limit_chars: int = 1800) -> str:
    if not uids:
        return "（まだ誰も参加してない）"
    s = " ".join([f"<@{uid}>" for uid in uids])
    if len(s) > limit_chars:
        s = s[:limit_chars] + " …"
    return s

# =========================
# Embeds
# =========================
def tenko_embed(message_id: int, closed: bool = False, close_at: datetime.datetime | None = None) -> discord.Embed:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM attendance WHERE message_id=? ORDER BY user_id ASC",
            (message_id,)
        ).fetchall()
    uids = [int(r[0]) for r in rows]
    count = len(uids)

    e = discord.Embed(
        title="🕔 点呼（参加）",
        description=f"✅ボタンで参加/取消できるよ\n\n**参加人数：{count}**\n{safe_mentions(uids)}",
    )
    if close_at:
        e.add_field(name="締切", value=f"{close_at.strftime('%Y/%m/%d %H:%M')}（{TZ}）", inline=False)
    if closed:
        e.set_footer(text="締切済み（操作は無効）")
    else:
        e.set_footer(text="参加する人は✅を押してね")
    return e

def weekly_counts(message_id: int):
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
    return by_day, counts, best_days

def next_occurrence_of_weekday(target_weekday: int, hour: int, minute: int) -> datetime.datetime:
    # target_weekday: 0=Mon..6=Sun
    now = now_jst()
    days_ahead = (target_weekday - now.weekday()) % 7
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + datetime.timedelta(days=days_ahead)
    if t <= now:
        t += datetime.timedelta(days=7)
    return t

def weekly_embed(message_id: int, closed: bool = False, close_at: datetime.datetime | None = None) -> discord.Embed:
    by_day, counts, best_days = weekly_counts(message_id)

    if best_days:
        best_text = " / ".join([f"{JP_DAYS[d]}({counts[d]})" for d in best_days])
        # 1つ目を「次回候補」として採用
        next_dt = next_occurrence_of_weekday(best_days[0], WEEKLY_POST_HOUR, WEEKLY_POST_MINUTE)
        next_text = f"{next_dt.strftime('%Y/%m/%d')}（{JP_DAYS[next_dt.weekday()]}） {next_dt.strftime('%H:%M')}（{TZ}）"
    else:
        best_text = "まだ投票なし"
        next_text = "未定（投票してね）"

    e = discord.Embed(
        title="📅 出席できそうな曜日（アンケ）",
        description="下のセレクトで「出席できそうな曜日」を複数選べるよ",
    )
    e.add_field(name="✅ 最多", value=best_text, inline=False)
    e.add_field(name="🗓 次回候補（最多から自動計算）", value=next_text, inline=False)

    # 表っぽく整形（EmbedはMarkdownテーブルが見づらいのでフィールドで）
    lines = []
    for d in range(7):
        members = safe_mentions(by_day[d], limit_chars=700)
        lines.append(f"**{JP_DAYS[d]}：{counts[d]}人**\n{members}")
    chunk = "\n\n".join(lines)
    if len(chunk) > 3500:
        chunk = chunk[:3500] + "\n…（長いので省略）"
    e.add_field(name="集計", value=chunk, inline=False)

    if close_at:
        e.add_field(name="締切", value=f"{close_at.strftime('%Y/%m/%d %H:%M')}（{TZ}）", inline=False)

    if closed:
        e.set_footer(text="締切済み（操作は無効）")
    else:
        e.set_footer(text="投票したらセレクトを閉じるだけでOK（自動更新）")
    return e

# =========================
# Views
# =========================
class TenkoView(View):
    def __init__(self, disabled: bool = False):
        super().__init__(timeout=None)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = disabled

  async def set_status(self, interaction: discord.Interaction, status: str):
    mid = interaction.message.id
    uid = interaction.user.id

    # まず締切チェック（ここはdefer前にOK）
    with db() as conn:
        row = conn.execute("SELECT status FROM meta WHERE message_id=?", (mid,)).fetchone()
    if row and row[0] == "closed":
        return await interaction.response.send_message("この点呼は締切済み！", ephemeral=True)

    # 先にACK（タイムアウト対策）
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
            view=TenkoView(disabled=False)
        )
        await interaction.followup.send(
            "参加にしたよ！" if status == "yes" else "不参加にしたよ！",
            ephemeral=True
        )
    except Exception as e:
        # これがあると「失敗しました」じゃなく理由が見える
        await interaction.followup.send(f"エラー起きた: {e}", ephemeral=True)

    @discord.ui.button(label="✅ 参加", style=discord.ButtonStyle.success, custom_id="tenko_yes")
    async def yes(self, interaction: discord.Interaction, _):
        await self.set_status(interaction, "yes")

    @discord.ui.button(label="❌ 不参加", style=discord.ButtonStyle.danger, custom_id="tenko_no")
    async def no(self, interaction: discord.Interaction, _):
        await self.set_status(interaction, "no")


class WeeklySelect(Select):
    def __init__(self, disabled: bool = False):
        options = [discord.SelectOption(label=f"{JP_DAYS[i]}曜日", value=str(i)) for i in range(7)]
        super().__init__(
            placeholder="出席できそうな曜日を選んでね（複数OK）",
            min_values=0,
            max_values=7,
            options=options,
            custom_id="weekly_select",
            disabled=disabled
        )

    async def callback(self, interaction: discord.Interaction):
        mid = interaction.message.id
        uid = interaction.user.id

        with db() as conn:
            row = conn.execute("SELECT status, close_at FROM meta WHERE message_id=?", (mid,)).fetchone()
        if row and row[0] == "closed":
            return await interaction.response.send_message("このアンケは締切済み！", ephemeral=True)

        chosen = {int(v) for v in self.values}

        with db() as conn:
            conn.execute("DELETE FROM weekly_votes WHERE message_id=? AND user_id=?", (mid, uid))
            for d in chosen:
                conn.execute(
                    "INSERT OR IGNORE INTO weekly_votes(message_id,user_id,day) VALUES(?,?,?)",
                    (mid, uid, d)
                )
            row2 = conn.execute("SELECT close_at FROM meta WHERE message_id=?", (mid,)).fetchone()
            close_at = parse_iso(row2[0]) if row2 else None

        await interaction.response.defer(thinking=False)
        await interaction.message.edit(embed=weekly_embed(mid, closed=False, close_at=close_at), view=WeeklyView(disabled=False))
        await interaction.followup.send("更新したよ！", ephemeral=True)

class WeeklyView(View):
    def __init__(self, disabled: bool = False):
        super().__init__(timeout=None)
        self.add_item(WeeklySelect(disabled=disabled))

# =========================
# Post helpers
# =========================
async def post_tenko(channel: discord.abc.Messageable) -> int:
    close_at = None
    if TENKO_CLOSE_MIN > 0:
        close_at = now_jst() + datetime.timedelta(minutes=TENKO_CLOSE_MIN)

    msg = await channel.send(
        content=mention_prefix() + "🕔 点呼を始めるよ！",
        embed=tenko_embed(0, closed=False, close_at=close_at),  # message_idは後で差し替え
        view=TenkoView(disabled=False)
    )

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(message_id,kind,channel_id,created_at,close_at,status,reminded) VALUES(?,?,?,?,?,?,?)",
            (msg.id, "tenko", msg.channel.id, iso(now_jst()), iso(close_at), "open", 0)
        )

    await msg.edit(embed=tenko_embed(msg.id, closed=False, close_at=close_at), view=TenkoView(disabled=False))
    return msg.id

async def post_weekly(channel: discord.abc.Messageable) -> int:
    close_at = now_jst() + datetime.timedelta(hours=WEEKLY_CLOSE_HOURS) if WEEKLY_CLOSE_HOURS > 0 else None

    msg = await channel.send(
        content=mention_prefix() + "📅 曜日アンケを始めるよ（複数選択OK）",
        embed=weekly_embed(0, closed=False, close_at=close_at),
        view=WeeklyView(disabled=False)
    )

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(message_id,kind,channel_id,created_at,close_at,status,reminded) VALUES(?,?,?,?,?,?,?)",
            (msg.id, "weekly", msg.channel.id, iso(now_jst()), iso(close_at), "open", 0)
        )

    await msg.edit(embed=weekly_embed(msg.id, closed=False, close_at=close_at), view=WeeklyView(disabled=False))
    return msg.id

def next_weekly_post_time() -> datetime.datetime:
    now = now_jst()
    days_ahead = (WEEKLY_POST_DAY - now.weekday()) % 7
    target = now.replace(hour=WEEKLY_POST_HOUR, minute=WEEKLY_POST_MINUTE, second=0, microsecond=0) + datetime.timedelta(days=days_ahead)
    if target <= now:
        target += datetime.timedelta(days=7)
    return target

async def close_poll(message_id: int):
    with db() as conn:
        row = conn.execute("SELECT kind, channel_id, close_at, status FROM meta WHERE message_id=?", (message_id,)).fetchone()
    if not row:
        return
    kind, channel_id, close_at_s, status = row
    if status == "closed":
        return

    close_at = parse_iso(close_at_s)
    try:
        channel = await client.fetch_channel(int(channel_id))
        msg = await channel.fetch_message(int(message_id))
    except Exception:
        # メッセージ取れない場合も閉じた扱いにする
        with db() as conn:
            conn.execute("UPDATE meta SET status='closed' WHERE message_id=?", (message_id,))
        return

    if kind == "tenko":
        await msg.edit(embed=tenko_embed(message_id, closed=True, close_at=close_at), view=TenkoView(disabled=True))
        await channel.send(f"✅ 点呼を締め切ったよ！（message_id={message_id}）")
    else:
        await msg.edit(embed=weekly_embed(message_id, closed=True, close_at=close_at), view=WeeklyView(disabled=True))

        # 締切結果を別メッセージで出す（見逃しにくい）
        by_day, counts, best_days = weekly_counts(message_id)
        if best_days:
            best_text = " / ".join([f"{JP_DAYS[d]}({counts[d]})" for d in best_days])
            next_dt = next_occurrence_of_weekday(best_days[0], WEEKLY_POST_HOUR, WEEKLY_POST_MINUTE)
            next_text = f"{next_dt.strftime('%Y/%m/%d')}（{JP_DAYS[next_dt.weekday()]}） {next_dt.strftime('%H:%M')}（{TZ}）"
        else:
            best_text = "まだ投票なし"
            next_text = "未定"

        e = discord.Embed(title="📌 週一アンケ結果（締切）")
        e.add_field(name="✅ 最多", value=best_text, inline=False)
        e.add_field(name="🗓 次回候補（最多から）", value=next_text, inline=False)
        await channel.send(embed=e)

    with db() as conn:
        conn.execute("UPDATE meta SET status='closed' WHERE message_id=?", (message_id,))

# =========================
# Scheduled tasks
# =========================
@tasks.loop(minutes=1)
async def daily_rollcall_loop():
    if not CHANNEL_ID:
        return
    now = now_jst()
    if now.hour == DAILY_TENKO_HOUR and now.minute == DAILY_TENKO_MINUTE:
        channel = await client.fetch_channel(CHANNEL_ID)
        await post_tenko(channel)

@tasks.loop(seconds=10)
async def weekly_post_loop():
    # 10秒ごとに「次の投稿時刻」を見て、過ぎたら投げる方式（再起動にも強い）
    if not CHANNEL_ID:
        return

    now = now_jst()
    target = next_weekly_post_time()

    # ちょうど付近でだけ発火（±10秒）
    if abs((now - target).total_seconds()) <= 10:
        channel = await client.fetch_channel(CHANNEL_ID)
        await post_weekly(channel)

@tasks.loop(seconds=20)
async def closer_loop():
    # DB見て締切処理＆リマインドを回す（再起動にも強い）
    now = now_jst()
    with db() as conn:
        rows = conn.execute("""
            SELECT message_id, kind, channel_id, close_at, reminded, status
            FROM meta
            WHERE status='open' AND close_at IS NOT NULL
        """).fetchall()

    for message_id, kind, channel_id, close_at_s, reminded, status in rows:
        close_at = parse_iso(close_at_s)
        if not close_at:
            continue

        # リマインド
        if REMIND_BEFORE_MIN > 0 and int(reminded) == 0:
            remind_at = close_at - datetime.timedelta(minutes=REMIND_BEFORE_MIN)
            if remind_at <= now < close_at:
                try:
                    channel = await client.fetch_channel(int(channel_id))
                    await channel.send(f"⏰ もうすぐ締切！ {REMIND_BEFORE_MIN}分後に締めるよ（message_id={message_id}）")
                except Exception:
                    pass
                with db() as conn:
                    conn.execute("UPDATE meta SET reminded=1 WHERE message_id=?", (int(message_id),))

        # 締切
        if now >= close_at:
            await close_poll(int(message_id))

# =========================
# Slash commands
# =========================
@tree.command(name="tenko", description="点呼を開始（✅で参加）", guild=guild_obj())
async def tenko_cmd(interaction: discord.Interaction):
    mid = await post_tenko(interaction.channel)
    await interaction.response.send_message(f"点呼出した！(message_id={mid})", ephemeral=True)

@tree.command(name="yobi", description="週の出席できそうな曜日アンケを開始（複数選択OK）", guild=guild_obj())
async def yobi_cmd(interaction: discord.Interaction):
    mid = await post_weekly(interaction.channel)
    await interaction.response.send_message(f"曜日アンケ出した！(message_id={mid})", ephemeral=True)

@tree.command(name="shukei", description="集計メッセージを更新する（message_id指定も可）", guild=guild_obj())
@app_commands.describe(message_id="更新したいメッセージID（空なら直近）")
async def shukei_cmd(interaction: discord.Interaction, message_id: str = ""):
    mid = int(message_id) if message_id.strip() else None

    if not mid:
        with db() as conn:
            row = conn.execute(
                "SELECT message_id, kind, close_at, status FROM meta ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return await interaction.response.send_message("集計対象が見つからない…", ephemeral=True)
        mid, kind, close_at_s, status = int(row[0]), row[1], row[2], row[3]
    else:
        with db() as conn:
            row = conn.execute("SELECT kind, close_at, status FROM meta WHERE message_id=?", (mid,)).fetchone()
        kind = row[0] if row else "weekly"
        close_at_s = row[1] if row else None
        status = row[2] if row else "open"

    close_at = parse_iso(close_at_s)
    closed = (status == "closed")

    try:
        msg = await interaction.channel.fetch_message(mid)
    except Exception:
        return await interaction.response.send_message("そのmessage_id、このチャンネルで取れなかった…", ephemeral=True)

    if kind == "tenko":
        await msg.edit(embed=tenko_embed(mid, closed=closed, close_at=close_at), view=TenkoView(disabled=closed))
    else:
        await msg.edit(embed=weekly_embed(mid, closed=closed, close_at=close_at), view=WeeklyView(disabled=closed))

    await interaction.response.send_message("更新した！", ephemeral=True)

# =========================
# Lifecycle
# =========================
@client.event
async def on_ready():
    print(f"READY: {client.user} (ID: {client.user.id})")

    # 永続View（再起動してもボタン/セレクトが効く）
    client.add_view(TenkoView(disabled=False))
    client.add_view(WeeklyView(disabled=False))

    # スラコマ同期（GUILD_IDあり＝即反映 / なし＝グローバルで時間かかる）
    try:
        if GUILD_ID:
            await tree.sync(guild=discord.Object(id=GUILD_ID))
            print("[SYNC] guild")
        else:
            await tree.sync()
            print("[SYNC] global (may take time)")
    except Exception as e:
        print("[SYNC ERROR]", e)

    if not daily_rollcall_loop.is_running():
        daily_rollcall_loop.start()

    if not weekly_post_loop.is_running():
        weekly_post_loop.start()

    if not closer_loop.is_running():
        closer_loop.start()

def wait_env():
    # Fly secrets 反映遅延対策（最大10秒）
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
        print("CHANNEL_ID がないので自動投稿は無効（/tenko /yobi は使える）")

    client.run(TOKEN)





