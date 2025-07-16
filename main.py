import os
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")

import discord
from discord.ext import commands, tasks
import json
import random
import asyncio
import datetime

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.reactions = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

QUIZ_CHANNEL_ID = 1394762078115336365  # <- ID kanału do quizu
QUIZ_ROLE_IDS = [1356372381043523584]  # <- ID ról uprawnionych do !quiz

DAILY_QUESTION_HOUR_RANGE = (9, 21)
QUESTION_FILE = 'pytania.json'
RANKING_FILE = 'ranking.json'

current_question = None
answered_users = set()
reaction_msg_id = None
question_end_time = None

def load_questions():
    with open(QUESTION_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_ranking():
    if not os.path.exists(RANKING_FILE):
        return {}
    with open(RANKING_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_ranking(ranking):
    with open(RANKING_FILE, 'w', encoding='utf-8') as f:
        json.dump(ranking, f, indent=2, ensure_ascii=False)

def get_filtered_ranking(days=None):
    ranking = load_ranking()
    filtered = {}
    now = datetime.datetime.utcnow()

    for uid, timestamps in ranking.items():
        count = 0
        for ts in timestamps:
            dt = datetime.datetime.fromisoformat(ts)
            if not days or (now - dt).days < days:
                count += 1
        if count > 0:
            filtered[uid] = count

    return dict(sorted(filtered.items(), key=lambda x: x[1], reverse=True))

@bot.event
async def on_ready():
    print(f'Zalogowano jako {bot.user}')
    post_daily_question.start()

@bot.event
async def on_reaction_add(reaction, user):
    global current_question, answered_users, reaction_msg_id, question_end_time

    if user.bot:
        return
    if current_question is None or reaction.message.id != reaction_msg_id:
        return
    if datetime.datetime.utcnow() > question_end_time:
        return

    correct_index = ord(current_question["answer"]) - ord("A")
    correct_emoji = ["🇦", "🇧", "🇨", "🇩"][correct_index]

    if reaction.emoji != correct_emoji:
        return
    if user.id in answered_users:
        return

    ranking = load_ranking()
    now_str = datetime.datetime.utcnow().isoformat()
    if str(user.id) not in ranking:
        ranking[str(user.id)] = []
    ranking[str(user.id)].append(now_str)
    save_ranking(ranking)

    answered_users.add(user.id)

async def post_quiz_question(channel):
    global current_question, answered_users, reaction_msg_id, question_end_time

    questions = load_questions()
    current_question = random.choice(questions)
    answered_users = set()

    q = current_question
    msg = await channel.send(
        f"@everyone\n📜 **Pytanie dnia:**\n{q['question']}\n\n"
        f"🇦 {q['options'][0]}\n"
        f"🇧 {q['options'][1]}\n"
        f"🇨 {q['options'][2]}\n"
        f"🇩 {q['options'][3]}"
    )
    for emoji in ["🇦", "🇧", "🇨", "🇩"]:
        await msg.add_reaction(emoji)

    reaction_msg_id = msg.id
    question_end_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=300)

    await asyncio.sleep(300)

    if answered_users:
        user_mentions = []
        for uid in answered_users:
            user = await bot.fetch_user(uid)
            user_mentions.append(user.mention)
        users_list = ', '.join(user_mentions)
        await channel.send(
            f"✅ Czas minął! Odpowiedź poprawna to: **{current_question['answer']}**\n"
            f"Punkty zdobyli: {users_list}"
        )
    else:
        await channel.send(
            f"✅ Czas minął! Odpowiedź poprawna to: **{current_question['answer']}**\n"
            f"Nikt nie zdobył punktu 😢"
        )

@bot.command()
async def quiz(ctx):
    if not any(role.id in QUIZ_ROLE_IDS for role in ctx.author.roles):
        return
    await post_quiz_question(ctx.channel)

@bot.command()
async def ranking(ctx):
    await send_ranking(ctx, None, "🏆 Ranking wszechczasów")

@bot.command()
async def rankingweekly(ctx):
    await send_ranking(ctx, 7, "📅 Ranking tygodniowy (7 dni)")

@bot.command()
async def rankingmonthly(ctx):
    await send_ranking(ctx, 30, "🗓 Ranking miesięczny (30 dni)")

async def send_ranking(ctx, days, title):
    filtered = get_filtered_ranking(days)
    if not filtered:
        await ctx.send("Brak wyników.")
        return
    msg = f"**{title}:**\n"
    for i, (uid, score) in enumerate(filtered.items(), start=1):
        user = await bot.fetch_user(int(uid))
        msg += f"{i}. {user.name} – {score} pkt\n"
    await ctx.send(msg)

@bot.command()
async def punkty(ctx):
    user_id = str(ctx.author.id)
    now = datetime.datetime.utcnow()
    data = load_ranking()

    if user_id not in data or not data[user_id]:
        await ctx.send(f"{ctx.author.mention}, nie masz jeszcze żadnych punktów.")
        return

    all_points = len(data[user_id])
    weekly_points = sum(
        1 for ts in data[user_id]
        if (now - datetime.datetime.fromisoformat(ts)).days < 7
    )
    monthly_points = sum(
        1 for ts in data[user_id]
        if (now - datetime.datetime.fromisoformat(ts)).days < 30
    )

    await ctx.send(
        f"📊 {ctx.author.mention}, Twój wynik:\n"
        f"• 🏆 Ogólnie: **{all_points}** pkt\n"
        f"• 📅 Ostatnie 7 dni: **{weekly_points}** pkt\n"
        f"• 🗓 Ostatnie 30 dni: **{monthly_points}** pkt"
    )

@tasks.loop(hours=24)
async def post_daily_question():
    await bot.wait_until_ready()
    channel = bot.get_channel(QUIZ_CHANNEL_ID)
    now = datetime.datetime.now()
    target_hour = random.randint(*DAILY_QUESTION_HOUR_RANGE)
    target_time = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if target_time < now:
        target_time += datetime.timedelta(days=1)
    wait_seconds = (target_time - now).total_seconds()
    await asyncio.sleep(wait_seconds)
    await post_quiz_question(channel)

token = os.getenv("TOKEN")
bot.run(token)
