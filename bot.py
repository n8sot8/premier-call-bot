import os
import discord
from discord.ext import commands, tasks
from datetime import datetime
import pytz

TOKEN = os.getenv("MTQ2MTE0MDE5MTk5NjY3NDMzMg.GRKlhg.KzwKIzi5OLktodpJIcI3YlfZlT7y5mDvmCFZRY")
GUILD_ID = int(os.getenv("1381879028331577434", "0"))
CHANNEL_ID = int(os.getenv("1461138164902002840", "0"))

if not TOKEN or GUILD_ID == 0 or CHANNEL_ID == 0:
    raise RuntimeError("Missing env vars: DISCORD_TOKEN / GUILD_ID / CHANNEL_ID")

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

attendance = {"yes": set(), "no": set()}

class RollCallView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="出席", style=discord.ButtonStyle.green)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        attendance["yes"].add(interaction.user.id)
        attendance["no"].discard(interaction.user.id)
        await interaction.response.send_message("✅ 出席で記録しました", ephemeral=True)

    @discord.ui.button(label="欠席", style=discord.ButtonStyle.red)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        attendance["no"].add(interaction.user.id)
        attendance["yes"].discard(interaction.user.id)
        await interaction.response.send_message("❌ 欠席で記録しました", ephemeral=True)

@tasks.loop(minutes=1)
async def daily_rollcall():
    jst = pytz.timezone("Asia/Tokyo")
    now = datetime.now(jst)
    if now.hour == 17 and now.minute == 0:
        attendance["yes"].clear()
        attendance["no"].clear()
        channel = bot.get_channel(CHANNEL_ID)
        await channel.send("🕔 **本日のギルド点呼**\n出席 or 欠席 を押してください", view=RollCallView())

@tasks.loop(minutes=1)
async def report_result():
    jst = pytz.timezone("Asia/Tokyo")
    now = datetime.now(jst)
    if now.hour == 17 and now.minute == 10:
        guild = bot.get_guild(GUILD_ID)
        members = [m for m in guild.members if not m.bot]
        yes, no = attendance["yes"], attendance["no"]
        no_response = [m.mention for m in members if m.id not in yes and m.id not in no]
        channel = bot.get_channel(CHANNEL_ID)

        msg = "📊 **本日の点呼結果**\n"
        msg += f"✅ 出席: {len(yes)}人\n"
        msg += f"❌ 欠席: {len(no)}人\n"
        if no_response:
            msg += "\n⏰ 未回答:\n" + " ".join(no_response)
        await channel.send(msg)

@bot.event
async def on_ready():
    print("Bot起動")
    daily_rollcall.start()
    report_result.start()

bot.run(TOKEN)
