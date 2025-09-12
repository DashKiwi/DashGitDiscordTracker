import discord
from discord.ext import commands, tasks
import aiosqlite
import os
from dotenv import load_dotenv
import sys
import asyncio
import aiohttp
from datetime import datetime, timedelta

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
@tasks.loop(minutes=1)
async def check_commits():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, github_username, discord_id, last_event_id FROM github_accounts")
        accounts = await cursor.fetchall()

    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    now = datetime.utcnow()

    for acc_id, username, discord_id, last_event_id in accounts:
        repos = await get_public_repos(username)
        if not repos:
            print(f"No public repos found for {username}")
            continue

        async with aiohttp.ClientSession() as session:
            new_commits = []

            for repo in repos:
                commits_url = f"https://api.github.com/repos/{username}/{repo}/commits?per_page=10"
                async with session.get(commits_url, headers=headers) as resp:
                    if resp.status != 200:
                        print(f"⚠️ Failed to fetch commits for {repo}: {resp.status}")
                        continue
                    commits = await resp.json()
                    for commit in commits:
                        sha = commit["sha"]
                        commit_date = datetime.strptime(commit["commit"]["author"]["date"], "%Y-%m-%dT%H:%M:%SZ")
                        if last_event_id and sha == last_event_id:
                            break  # stop once we reach last seen commit
                        if now - commit_date <= timedelta(days=7):  # only last week
                            new_commits.append((repo, commit, commit_date))

            if new_commits:
                # Only take the newest commit per repo
                newest_commits = {}
                for repo_name, commit, commit_date in new_commits:
                    if repo_name not in newest_commits or commit_date > newest_commits[repo_name][1]:
                        newest_commits[repo_name] = (commit, commit_date)

                # Update last_event_id to the most recent commit overall
                most_recent_sha = max(newest_commits.values(), key=lambda x: x[1])[0]["sha"]
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE github_accounts SET last_event_id = ? WHERE id = ?",
                        (most_recent_sha, acc_id)
                    )
                    await db.commit()

                # Determine Discord channel
                channel = None
                if bot.guilds:
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
                    for repo_name, (commit, commit_date) in newest_commits.items():
                        message = commit["commit"]["message"]
                        author_name = commit["commit"]["author"]["name"]
                        html_url = commit["html_url"]
                        await channel.send(f"🔨 **{username}** pushed to **{repo_name}** by **{author_name}**:\n- {message}\n<{html_url}>")
                        print(f"Posted commit to Discord for {username}/{repo_name}")

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

# -------------------- HELPER: FETCH PUBLIC REPOS --------------------
async def get_public_repos(username):
    url = f"https://api.github.com/users/{username}/repos?per_page=100&type=owner"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                print(f"⚠️ Failed to fetch repos for {username}: {resp.status}")
                return []
            repos = await resp.json()
            return [repo["name"] for repo in repos if not repo["private"]]

# -------------------- SLASH COMMANDS --------------------
@bot.tree.command(name="add_github", description="Link a GitHub account to a Discord user")
@commands.has_permissions(administrator=True)
async def add_github(interaction: discord.Interaction, github_username: str, user: discord.Member = None):
    await interaction.response.defer()
    discord_id = user.id if user else None

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO github_accounts (github_username, discord_id, last_event_id) VALUES (?, ?, ?)",
            (github_username, discord_id, None)
        )
        acc_id = cursor.lastrowid
        await db.commit()

    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

    # Fetch all public repos
    public_repos = await get_public_repos(github_username)
    if not public_repos:
        await interaction.followup.send(f"❌ No public repos found for {github_username}")
        return

    newest_event_id = None

    async with aiohttp.ClientSession() as session:
        for repo in public_repos:
            repo_url = f"https://api.github.com/repos/{github_username}/{repo}/events"
            async with session.get(repo_url, headers=headers) as resp:
                if resp.status != 200:
                    continue
                events = await resp.json()
                for event in events:
                    if event["type"] == "PushEvent":
                        # Always keep the newest event ID (highest/latest)
                        if not newest_event_id or event["created_at"] > newest_event_id:
                            newest_event_id = event["id"]
                        break  # only need the latest PushEvent per repo

    # Update DB with the newest event ID
    if newest_event_id:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE github_accounts SET last_event_id = ? WHERE id = ?",
                (newest_event_id, acc_id)
            )
            await db.commit()
        print(f"First-run setup for {github_username}, storing last_event_id {newest_event_id}")

    await interaction.followup.send(
        f"✅ Linked **{github_username}** to {user.mention if user else 'no one'}"
    )

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
