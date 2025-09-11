import discord
from discord.ext import commands, tasks
import aiosqlite
import os
from dotenv import load_dotenv
import sys
import asyncio
import aiohttp
from datetime import datetime

# -------------------- WINDOWS EVENT LOOP FIX --------------------
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# -------------------- LOAD TOKENS --------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # GitHub personal access token

if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN not found in .env")
if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN not found in .env")

# -------------------- BOT SETUP --------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
DB_PATH = "github_bot.db"

# -------------------- DATABASE --------------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS github_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                github_username TEXT NOT NULL,
                discord_id INTEGER,
                last_event_id TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                update_channel_id INTEGER
            )
        """)
        await db.commit()

# -------------------- GITHUB POLLING --------------------
@tasks.loop(minutes=5)
async def check_commits():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, github_username, discord_id, last_event_id FROM github_accounts")
        accounts = await cursor.fetchall()

    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    async with aiohttp.ClientSession() as session:
        for acc in accounts:
            acc_id, username, discord_id, last_event_id = acc
            url = f"https://api.github.com/users/{username}/events"
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    print(f"⚠️ Failed to fetch events for {username}: {resp.status}")
                    continue
                events = await resp.json()
                if not events:
                    continue

                # Collect all new push events since last_event_id
                new_events = []
                for event in events:
                    if event["type"] == "PushEvent":
                        if last_event_id and event["id"] == last_event_id:
                            break  # stop at last seen event
                        new_events.append(event)

                if new_events:
                    # Update last_event_id to the newest fetched event
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE github_accounts SET last_event_id = ? WHERE id = ?",
                            (new_events[0]["id"], acc_id)
                        )
                        await db.commit()

                    # Determine update channel
                    async with aiosqlite.connect(DB_PATH) as db:
                        cursor = await db.execute(
                            "SELECT update_channel_id FROM settings WHERE guild_id = ?",
                            (bot.guilds[0].id,)
                        )
                        row = await cursor.fetchone()
                        channel_id = row[0] if row else None

                    channel = bot.get_channel(channel_id) if channel_id else discord.utils.get(
                        bot.guilds[0].channels, name="github-activity"
                    )

                    if channel:
                        # Send events oldest first
                        for event in reversed(new_events):
                            repo = event["repo"]["name"]
                            commit_msgs = [c["message"] for c in event["payload"]["commits"]]
                            msg = "\n".join([f"- {m}" for m in commit_msgs])
                            await channel.send(f"🔨 **{username}** pushed to **{repo}**:\n{msg}")

# -------------------- EVENTS --------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"⚠️ Error syncing commands: {e}")

@bot.event
async def setup_hook():
    await init_db()
    check_commits.start()

# -------------------- SLASH COMMANDS --------------------
@bot.tree.command(name="add_github", description="Link a GitHub account to a Discord user")
@commands.has_permissions(administrator=True)
async def add_github(interaction: discord.Interaction, github_username: str, user: discord.Member = None):
    discord_id = user.id if user else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO github_accounts (github_username, discord_id) VALUES (?, ?)", (github_username, discord_id))
        await db.commit()
    await interaction.response.send_message(f"✅ Linked **{github_username}** to {user.mention if user else 'no one'}")

@bot.tree.command(name="remove_github", description="Remove a GitHub account")
@commands.has_permissions(administrator=True)
async def remove_github(interaction: discord.Interaction, github_username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM github_accounts WHERE github_username = ?", (github_username,))
        await db.commit()
    await interaction.response.send_message(f"🗑️ Removed **{github_username}**")

@bot.tree.command(name="list_githubs", description="List linked GitHub accounts")
async def list_githubs(interaction: discord.Interaction, user: discord.Member = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if user:
            cursor = await db.execute("SELECT github_username FROM github_accounts WHERE discord_id = ?", (user.id,))
            accounts = await cursor.fetchall()
            if accounts:
                accounts_str = ", ".join(acc[0] for acc in accounts)
                await interaction.response.send_message(f"📋 GitHub accounts for {user.mention}: {accounts_str}")
            else:
                await interaction.response.send_message(f"❌ No GitHub accounts linked for {user.mention}")
        else:
            cursor = await db.execute("SELECT github_username, discord_id FROM github_accounts")
            accounts = await cursor.fetchall()
            if accounts:
                msg = "📋 Linked GitHub accounts:\n"
                for acc in accounts:
                    discord_user = f"<@{acc[1]}>" if acc[1] else "(unlinked)"
                    msg += f"- {acc[0]} → {discord_user}\n"
                await interaction.response.send_message(msg)
            else:
                await interaction.response.send_message("❌ No GitHub accounts linked yet.")

@bot.tree.command(name="change_github", description="Change Discord account associated with a GitHub account")
@commands.has_permissions(administrator=True)
async def change_github(interaction: discord.Interaction, github_username: str, user: discord.Member):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE github_accounts SET discord_id = ? WHERE github_username = ?",
            (user.id, github_username)
        )
        await db.commit()
    await interaction.response.send_message(f"✅ **{github_username}** is now linked to {user.mention}")

@bot.tree.command(name="current_streak", description="Show current contribution streak for a GitHub user")
async def current_streak(interaction: discord.Interaction, github_username: str):
    query = """
    query($username: String!) {
      user(login: $username) {
        contributionsCollection {
          contributionCalendar {
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    headers = {"Authorization": f"bearer {GITHUB_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.github.com/graphql", json={"query": query, "variables": {"username": github_username}}, headers=headers) as resp:
            if resp.status != 200:
                await interaction.response.send_message(f"⚠️ Failed to fetch data: {resp.status}")
                return
            data = await resp.json()
    if "errors" in data:
        await interaction.response.send_message(f"⚠️ Error: {data['errors'][0]['message']}")
        return
    weeks = data["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
    streak = 0
    counting = True
    today = datetime.utcnow().date()
    days = [day for week in weeks for day in week["contributionDays"]]
    days = sorted(days, key=lambda x: x["date"], reverse=True)
    for day in days:
        day_date = datetime.strptime(day["date"], "%Y-%m-%d").date()
        if day_date > today:
            continue
        if counting:
            if day["contributionCount"] > 0:
                streak += 1
            else:
                counting = False
        else:
            break
    await interaction.response.send_message(f"🔥 **{github_username}** current contribution streak: **{streak} day(s)**")

@bot.tree.command(name="streak_repo", description="Show current contribution streak for a user in a specific repository")
async def streak_repo(interaction: discord.Interaction, github_username: str, repo_name: str):
    query = """
    query($username: String!, $repoName: String!) {
      user(login: $username) {
        contributionsCollection(repositoryName: $repoName) {
          contributionCalendar {
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    headers = {"Authorization": f"bearer {GITHUB_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.github.com/graphql", json={"query": query, "variables": {"username": github_username, "repoName": repo_name}}, headers=headers) as resp:
            if resp.status != 200:
                await interaction.response.send_message(f"⚠️ Failed to fetch data: {resp.status}")
                return
            data = await resp.json()
    if "errors" in data:
        await interaction.response.send_message(f"⚠️ Error: {data['errors'][0]['message']}")
        return
    weeks = data["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
    streak = 0
    counting = True
    today = datetime.utcnow().date()
    days = [day for week in weeks for day in week["contributionDays"]]
    days = sorted(days, key=lambda x: x["date"], reverse=True)
    for day in days:
        day_date = datetime.strptime(day["date"], "%Y-%m-%d").date()
        if day_date > today:
            continue
        if counting:
            if day["contributionCount"] > 0:
                streak += 1
            else:
                counting = False
        else:
            break
    await interaction.response.send_message(f"🔥 **{github_username}** streak in **{repo_name}**: **{streak} day(s)**")

# -------------------- OPTIONAL: SET UPDATE CHANNEL --------------------
@bot.tree.command(name="set_github_channel", description="Set the channel for GitHub updates")
@commands.has_permissions(administrator=True)
async def set_github_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (guild_id, update_channel_id) VALUES (?, ?)",
            (interaction.guild.id, channel.id)
        )
        await db.commit()
    await interaction.response.send_message(f"✅ GitHub updates will now post in {channel.mention}")

# -------------------- RUN BOT --------------------
bot.run(TOKEN)
