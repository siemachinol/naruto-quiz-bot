import discord
from discord.ext import commands, tasks
import random
import json
import asyncio
import datetime
from dotenv import load_dotenv
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# === ŁADOWANIE ZMIENNYCH ŚRODOWISKOWYCH ===
load_dotenv()
TOKEN = os.getenv("TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
SUPPORTER_ROLE_ID = 1377326388415299777

# === INTENCJE DISCORDA ===
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.reactions = True
intents.members = True  # WAŻNE

bot = commands.Bot(command_prefix="!", intents=intents)

# === ŚCIEŻKI DO PLIKÓW ===
PYTANIA_PATH = "pytania.json"
RANKING_PATH = "ranking.json"

# === ZMIENNE POMOCNICZE ===
current_question = None
current_message = None
answered_users = set()
supporter_quiz_used_at = None
fired_times_today = set()

# === FUNKCJE QUIZOWE ===

def load_questions():
    with open(PYTANIA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_ranking(data):
    with open(RANKING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_ranking():
    if not os.path.exists(RANKING_PATH):
        return {}
    with open(RANKING_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

async def run_quiz(channel):
    global current_question, current_message, answered_users

    questions = load_questions()
    current_question = random.choice(questions)
    answered_users = set()

    embed = discord.Embed(title="Pytanie Quizowe!", description=current_question["question"], color=0xff9900)
    for option in ["A", "B", "C", "D"]:
        embed.add_field(name=option, value=current_question["options"][option], inline=False)

    current_message = await channel.send(embed=embed)

    for emoji in ["🇦", "🇧", "🇨", "🇩"]:
        await current_message.add_reaction(emoji)

    await asyncio.sleep(900)  # 15 minut

    await reveal_answer(channel)

async def reveal_answer(channel):
    global current_question, current_message, answered_users

    correct_emoji = {
        "A": "🇦",
        "B": "🇧",
        "C": "🇨",
        "D": "🇩"
    }[current_question["answer"]]

    message = await channel.fetch_message(current_message.id)
    ranking = load_ranking()

    for reaction in message.reactions:
        if reaction.emoji == correct_emoji:
            users = await reaction.users().flatten()
            for user in users:
                if user.bot or user.id in answered_users:
                    continue
                answered_users.add(user.id)
                user_id = str(user.id)
                today = str(datetime.date.today())

                if user_id not in ranking:
                    ranking[user_id] = {
                        "name": user.name,
                        "points": 0,
                        "weekly": {},
                        "monthly": {}
                    }

                ranking[user_id]["points"] += 1
                ranking[user_id]["weekly"][today] = ranking[user_id]["weekly"].get(today, 0) + 1
                ranking[user_id]["monthly"][today] = ranking[user_id]["monthly"].get(today, 0) + 1

    save_ranking(ranking)
    await channel.send(f"Prawidłowa odpowiedź to: **{current_question['answer']}** {correct_emoji}")

# === KOMENDY BOTA ===

@bot.command()
async def quiz(ctx):
    global supporter_quiz_used_at

    today = datetime.date.today()
    author = ctx.author
    role_ids = [role.id for role in author.roles]

    print(f"== DEBUG ==")
    print(f"Autor: {author.name}")
    print(f"ID roli SUPPORTER: {SUPPORTER_ROLE_ID}")
    print(f"Role autora: {role_ids}")
    print(f"Admin?: {author.guild_permissions.administrator}")
    print(f"SUPPORTER w rolach?: {SUPPORTER_ROLE_ID in role_ids}")

    if SUPPORTER_ROLE_ID in role_ids or author.guild_permissions.administrator:
        if supporter_quiz_used_at == today and not author.guild_permissions.administrator:
            await ctx.send("Quiz został już dziś aktywowany przez wspierającego.")
            return
        else:
            supporter_quiz_used_at = today
            await run_quiz(ctx.channel)
    else:
        await ctx.send("Nie masz uprawnień do tej komendy.")

@bot.command()
async def punkty(ctx):
    ranking = load_ranking()
    user_id = str(ctx.author.id)
    user_data = ranking.get(user_id)

    if not user_data:
        await ctx.send("Nie masz jeszcze żadnych punktów.")
    else:
        await ctx.send(f"Masz {user_data['points']} punktów całkowitych.")

@bot.command()
async def ranking(ctx):
    ranking = load_ranking()
    sorted_users = sorted(ranking.items(), key=lambda x: x[1]["points"], reverse=True)

    embed = discord.Embed(title="Ranking All-Time", color=0x00ff00)
    for i, (user_id, data) in enumerate(sorted_users[:10], start=1):
        embed.add_field(name=f"{i}. {data['name']}", value=f"{data['points']} punktów", inline=False)

    await ctx.send(embed=embed)

@bot.command()
async def rankingweekly(ctx):
    ranking = load_ranking()
    week_ago = datetime.date.today() - datetime.timedelta(days=7)

    scores = {}
    for user_id, data in ranking.items():
        total = sum(points for date, points in data["weekly"].items() if datetime.date.fromisoformat(date) >= week_ago)
        if total > 0:
            scores[user_id] = (data["name"], total)

    sorted_scores = sorted(scores.items(), key=lambda x: x[1][1], reverse=True)

    embed = discord.Embed(title="Ranking Tygodniowy", color=0x00ffcc)
    for i, (user_id, (name, points)) in enumerate(sorted_scores[:10], start=1):
        embed.add_field(name=f"{i}. {name}", value=f"{points} punktów", inline=False)

    await ctx.send(embed=embed)

@bot.command()
async def rankingmonthly(ctx):
    ranking = load_ranking()
    month_ago = datetime.date.today() - datetime.timedelta(days=30)

    scores = {}
    for user_id, data in ranking.items():
        total = sum(points for date, points in data["monthly"].items() if datetime.date.fromisoformat(date) >= month_ago)
        if total > 0:
            scores[user_id] = (data["name"], total)

    sorted_scores = sorted(scores.items(), key=lambda x: x[1][1], reverse=True)

    embed = discord.Embed(title="Ranking Miesięczny", color=0x0099ff)
    for i, (user_id, (name, points)) in enumerate(sorted_scores[:10], start=1):
        embed.add_field(name=f"{i}. {name}", value=f"{points} punktów", inline=False)

    await ctx.send(embed=embed)

# === QUIZY O KONKRETNYCH GODZINACH ===

@tasks.loop(minutes=1)
async def daily_quiz_task():
    global fired_times_today
    now = datetime.datetime.now()
    now_time = now.time().replace(second=0, microsecond=0)

    quiz_times = [
        datetime.time(hour=12, minute=5),
        datetime.time(hour=15, minute=35),
        datetime.time(hour=20, minute=39)
    ]

    alert_times = [
        (datetime.datetime.combine(now.date(), qt) - datetime.timedelta(minutes=10)).time()
        for qt in quiz_times
    ]

    guild = bot.get_guild(GUILD_ID)
    channel = discord.utils.get(guild.text_channels, name="quiz")

    if not channel:
        return

    if now_time in alert_times:
        await channel.send("🧠 Za 10 minut pojawi się pytanie quizowe! Bądźcie w gotowości!")

    for qt in quiz_times:
        if now_time == qt and qt not in fired_times_today:
            await run_quiz(channel)
            fired_times_today.add(qt)

    if now.hour == 0 and fired_times_today:
        fired_times_today.clear()

# === KEEP-ALIVE SERVER (dla Render/UptimeRobot) ===

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is running.')

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()

def run_ping_server():
    server = HTTPServer(('0.0.0.0', 8080), PingHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

# === START BOTA ===

if __name__ == "__main__":
    run_ping_server()

    @bot.event
    async def on_ready():
        print(f"Zalogowano jako {bot.user}")
        if not daily_quiz_task.is_running():
            daily_quiz_task.start()

    bot.run(TOKEN)
