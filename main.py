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

load_dotenv()

TOKEN = os.getenv("TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

SUPPORTER_ROLE_ID = 1377326388415299777

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Ścieżki do plików
PYTANIA_PATH = "pytania.json"
RANKING_PATH = "ranking.json"

# Zmienne pomocnicze
current_question = None
current_message = None
answered_users = set()
quiz_hours = []
quiz_date = None
supporter_quiz_used_at = None

# Ładowanie pytań
def load_questions():
    with open(PYTANIA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

# Zapisywanie rankingu
def save_ranking(data):
    with open(RANKING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# Ładowanie rankingu
def load_ranking():
    if not os.path.exists(RANKING_PATH):
        return {}
    with open(RANKING_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

# Główna funkcja do quizu
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

# Komenda !quiz (dla wspierających raz dziennie lub admina)
@bot.command()
async def quiz(ctx):
    global supporter_quiz_used_at

    today = datetime.date.today()
    author = ctx.author
    role_ids = [role.id for role in author.roles]

    if SUPPORTER_ROLE_ID in role_ids or author.guild_permissions.administrator:
        if supporter_quiz_used_at == today and not author.guild_permissions.administrator:
            await ctx.send("Quiz został już dziś aktywowany przez wspierającego.")
            return
        else:
            supporter_quiz_used_at = today
            await run_quiz(ctx.channel)
    else:
        await ctx.send("Nie masz uprawnień do tej komendy.")

# Komenda !punkty
@bot.command()
async def punkty(ctx):
    ranking = load_ranking()
    user_id = str(ctx.author.id)
    user_data = ranking.get(user_id)

    if not user_data:
        await ctx.send("Nie masz jeszcze żadnych punktów.")
    else:
        await ctx.send(f"Masz {user_data['points']} punktów całkowitych.")

# Ranking ogólny
@bot.command()
async def ranking(ctx):
    ranking = load_ranking()
    sorted_users = sorted(ranking.items(), key=lambda x: x[1]["points"], reverse=True)

    embed = discord.Embed(title="Ranking All-Time", color=0x00ff00)
    for i, (user_id, data) in enumerate(sorted_users[:10], start=1):
        embed.add_field(name=f"{i}. {data['name']}", value=f"{data['points']} punktów", inline=False)

    await ctx.send(embed=embed)

# Ranking tygodniowy
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

# Ranking miesięczny
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

# Codzienne 3 pytania w losowych godzinach
@tasks.loop(minutes=1)
async def daily_quiz_task():
    global quiz_hours, quiz_date

    now = datetime.datetime.now()
    current_hour = now.hour
    current_minute = now.minute

    if not (9 <= current_hour <= 21):
        return

    if quiz_date != now.date():
        quiz_hours = sorted(random.sample(range(9, 21), 3))
        quiz_date = now.date()

    guild = bot.get_guild(GUILD_ID)
    channel = discord.utils.get(guild.text_channels, name="quiz")  # Zmień na swój kanał!

    if not channel:
        return

    for hour in quiz_hours[:]:
        # Pre-alert 10 minut przed quizem
        if current_hour == hour - 1 and current_minute == 50:
            await channel.send("🧠 Za 10 minut pojawi się pytanie quizowe! Bądźcie w gotowości!")

        # Sam quiz
        if current_hour == hour and current_minute == 0:
            await run_quiz(channel)
            quiz_hours.remove(hour)

# Keep alive HTTP serwer
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is running.')

def run_ping_server():
    server = HTTPServer(('0.0.0.0', 8080), PingHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

@bot.event
async def on_ready():
    print(f"Zalogowano jako {bot.user}")
    daily_quiz_task.start()
    run_ping_server()

bot.run(TOKEN)
