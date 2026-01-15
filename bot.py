import os
import time
import asyncio
from datetime import datetime
import pytz
import discord
from discord.ext import tasks

# ========= 環境変数を待って取得 =========
def get_env():
    token = os.getenv("DISCORD_TOKEN")
    guild = os.getenv("GUILD_ID")
    channel = os.getenv("CHANNEL_ID")
    return token, guild, channel

TOKEN = GUILD_ID_RAW = CHANNEL_ID_RAW = None

# 最大10秒待つ（Fly対策）
for _ in range(10):
    TOKEN, GUILD_ID_RAW, CHANNEL_ID_RAW = get_env()
    if TOKEN and GUILD_ID_RAW and CHANNEL_ID_RAW:
        break
    time.sleep(1)

print(
    "ENV CHECK:",
    "TOKEN?", bool(TOKEN),
    "GUILD?", bool(GUILD_ID_RAW),
    "CHANNEL?", bool(CHANNEL_ID_RAW),
)

# 取れなかったら安全に終了（クラッシュ扱いさせない）
if not TOKEN or not GUILD_ID_RAW or not CHANNEL_ID_RAW:
    print("Env vars not ready. Exit safely.")
    exit(0)

GUILD_ID = int(GUILD_ID_RAW)
CHANNEL_ID = int(CHANNEL_ID_RAW)

# ========= Discord 設定 =========
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # メンバー取得したい場合（Portal側でON必須）

client = discord.Client(intents=intents)

JST = pytz.timezone("Asia/Tokyo")

# ========= 起動時 =========
@client.event
async def on_ready():
    print(f"Bot起動: {client.user}")
    daily_rollcall.start()

# ========= 毎日17時 点呼 =========
@tasks.loop(minutes=1)
async def daily_rollcall():
    now = datetime.now(JST)

    # 17:00 ちょうど
    if now.hour == 17 and now.minute == 0:
        guild = client.get_guild(GUILD_ID)
        if guild is None:
            print("Guild not found")
            return

        channel = guild.get_channel(CHANNEL_ID)
        if channel is None:
            print("Channel not found")
            return

        await channel.send(
            "🕔 **17時の点呼です！**\n"
            "以下からリアクションしてください👇\n\n"
            "✅ はい\n"
            "❌ いいえ"
        )

# ========= 実行 =========
client.run(TOKEN)
