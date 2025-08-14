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
from supabase import create_client, Client

# === ŁADOWANIE ZMIENNYCH ŚRODOWISKOWYCH ===
load_dotenv()
TOKEN = os.getenv("TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
SUPPORTER_ROLE_ID = 1377326388415299777
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# 🔌 KILL-SWITCH — ustaw w Render ENV: BOT_DISABLED=true, żeby uśpić bota
if os.environ.get("BOT_DISABLED", "").lower() == "true":
    print("Bot is disabled temporarily.")
    raise SystemExit(0)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# === INTENCJE DISCORDA ===
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === ŚCIEŻKI DO PLIKÓW ===
PYTANIA_PATH = "pytania.json"

# === ZMIENNE POMOCNICZE ===
current_question = None
current_message = None
answered_users = {}
supporter_quiz_used_at = None
fired_times_today = set()
message_user_answers = {}
quiz_closed_messages = set()
quiz_channel = None

# === FUNKCJE SUPABASE: USED_QUESTIONS ===
def get_used_question_ids():
    try:
        response = supabase.table("used_questions").select("question_id").execute()
        used_ids = [item["question_id"] for item in response.data]
        print(f"[DB] Pobrano {len(used_ids)} użytych pytań z Supabase")
        return used_ids
    except Exception as e:
        print(f"[ERROR] Nie udało się pobrać użytych pytań: {e}")
        return []

def add_used_question_id(question_id):
    try:
        supabase.table("used_questions").insert({"question_id": question_id}).execute()
        print(f"[DB] Dodano pytanie ID {question_id} do used_questions")
    except Exception as e:
        print(f"[ERROR] Nie udało się dodać ID {question_id} do used_questions: {e}")

def clear_used_questions_if_needed():
    questions = load_questions()
    used_ids = get_used_question_ids()
    if len(used_ids) >= len(questions):
        print("[INFO] Wszystkie pytania zostały wykorzystane – czyszczę tabelę used_questions...")
        try:
            delete_response = supabase.table("used_questions").delete().neq("id", 0).execute()
            print("[INFO] Tabela used_questions została wyczyszczona ✅")
        except Exception as e:
            print(f"[ERROR] Wyjątek podczas czyszczenia tabeli: {e}")

# === KOMPONENT PRZYCISKÓW ===
from discord import ui, ButtonStyle, Interaction

class QuizView(ui.View):
    def __init__(self, correct_answer):
        super().__init__(timeout=None)
        self.correct_answer = correct_answer

    def disable_all_buttons(self):
        for child in self.children:
            child.disabled = True

    @ui.button(label="A", style=ButtonStyle.primary, custom_id="answer_a")
    async def answer_a(self, button: ui.Button, interaction: Interaction):
        await self.handle_answer(interaction, "A")

    @ui.button(label="B", style=ButtonStyle.primary, custom_id="answer_b")
    async def answer_b(self, button: ui.Button, interaction: Interaction):
        await self.handle_answer(interaction, "B")

    @ui.button(label="C", style=ButtonStyle.primary, custom_id="answer_c")
    async def answer_c(self, button: ui.Button, interaction: Interaction):
        await self.handle_answer(interaction, "C")

    @ui.button(label="D", style=ButtonStyle.primary, custom_id="answer_d")
    async def answer_d(self, button: ui.Button, interaction: Interaction):
        await self.handle_answer(interaction, "D")

    async def handle_answer(self, interaction, selected_letter):
        global message_user_answers, quiz_closed_messages

        user_id = str(interaction.user.id)
        message_id = str(interaction.message.id)

        if message_id in quiz_closed_messages:
            await interaction.response.send_message("\u23F1\uFE0F Czas na odpowiedzi minął. Nie można już odpowiadać.", ephemeral=True)
            return

        if message_id not in message_user_answers:
            message_user_answers[message_id] = {}

        if user_id in message_user_answers[message_id]:
            await interaction.response.send_message("\u2705 Już odpowiedziałeś na to pytanie!", ephemeral=True)
            return

        message_user_answers[message_id][user_id] = selected_letter
        await interaction.response.send_message(f"\ud83d\udcdd Zapisano Twoją odpowiedź: **{selected_letter}**", ephemeral=True)

# === FUNKCJE QUIZOWE ===
def load_questions():
    with open(PYTANIA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_ranking():
    response = supabase.table("ranking").select("*").execute()
    ranking = {}
    for row in response.data:
        ranking[row["user_id"]] = {
            "name": row["name"],
            "points": row["points"],
            "weekly": row["weekly"],
            "monthly": row["monthly"]
        }
    return ranking

def save_ranking(data):
    for user_id, user_data in data.items():
        existing = supabase.table("ranking").select("id").eq("user_id", user_id).execute().data
        if existing:
            supabase.table("ranking").update({
                "name": user_data["name"],
                "points": user_data["points"],
                "weekly": user_data["weekly"],
                "monthly": user_data["monthly"]
            }).eq("user_id", user_id).execute()
        else:
            supabase.table("ranking").insert({
                "user_id": user_id,
                "name": user_data["name"],
                "points": user_data["points"],
                "weekly": user_data["weekly"],
                "monthly": user_data["monthly"]
            }).execute()

async def run_quiz(channel):
    global current_question, current_message, answered_users, message_user_answers

    clear_used_questions_if_needed()

    questions = load_questions()
    used_ids = get_used_question_ids()
    available_questions = [q for q in questions if q["id"] not in used_ids]

    if not available_questions:
        await channel.send("Brak nowych pytań do wyświetlenia. Wszystkie zostały już użyte.")
        print("[INFO] Brak dostępnych pytań – wszystkie zostały wykorzystane.")
        return

    current_question = random.choice(available_questions)
    add_used_question_id(current_question["id"])
    print(f"[QUIZ] Wybrano pytanie ID: {current_question['id']}")

    answered_users = {}
    message_user_answers = {}

    quizowicz_role = discord.utils.get(channel.guild.roles, name="Quizowicz")
    mention = quizowicz_role.mention if quizowicz_role else "@Quizowicz"

    content = (
        f"{mention}\n"
        "\U0001F9E0 **Pytanie quizowe:**\n"
        f"{current_question['question']}\n\n"
        f"\U0001F1E6 {current_question['options']['A']}\n"
        f"\U0001F1E7 {current_question['options']['B']}\n"
        f"\U0001F1E8 {current_question['options']['C']}\n"
        f"\U0001F1E9 {current_question['options']['D']}\n\n"
        "Kliknij przycisk z odpowiedzią poniżej. Masz 15 minut na odpowiedź!"
    )

    quiz_view = QuizView(current_question["answer"])
    current_message = await channel.send(content, view=quiz_view)

    print("[QUIZ] Quiz wystartował, czekam 15 minut...")
    await asyncio.sleep(900)
    print("[QUIZ] Koniec quizu, podsumowanie...")

    quiz_view.disable_all_buttons()
    await current_message.edit(view=quiz_view)
    quiz_closed_messages.add(str(current_message.id))

    await reveal_answer(channel)

async def reveal_answer(channel):
    global current_question, current_message, answered_users, message_user_answers

    correct_letter = current_question["answer"]
    message_id = str(current_message.id)
    answers = message_user_answers.get(message_id, {})

    ranking = load_ranking()
    awarded_users = []

    for user_id, selected_letter in answers.items():
        if selected_letter == correct_letter:
            user = await bot.fetch_user(int(user_id))
            user_id_str = str(user.id)
            today = str(datetime.datetime.utcnow().date())

            if user_id_str not in ranking:
                ranking[user_id_str] = {
                    "name": user.name,
                    "points": 0,
                    "weekly": {},
                    "monthly": {}
                }

            ranking[user_id_str]["points"] += 1
            ranking[user_id_str]["weekly"][today] = ranking[user_id_str]["weekly"].get(today, 0) + 1
            ranking[user_id_str]["monthly"][today] = ranking[user_id_str]["monthly"].get(today, 0) + 1

            awarded_users.append(user.mention)

    save_ranking(ranking)

    await channel.send(f"Prawidłowa odpowiedź to: **{correct_letter}**")

    if awarded_users:
        await channel.send(f"\u2705 Punkty otrzymali: {', '.join(awarded_users)}")

# === KOMENDY ===
@bot.command()
async def quiz(ctx):
    global supporter_quiz_used_at

    today = datetime.datetime.utcnow().date()
    author = ctx.guild.get_member(ctx.author.id)

    if author is None:
        await ctx.send("Nie mogę pobrać Twoich ról. Spróbuj ponownie później.")
        return

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
    week_ago = datetime.datetime.utcnow().date() - datetime.timedelta(days=7)

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
    month_ago = datetime.datetime.utcnow().date() - datetime.timedelta(days=30)

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

# === QUIZ AUTOMATYCZNY ===
@tasks.loop(minutes=1)
async def daily_quiz_task():
    global fired_times_today, quiz_channel
    now = datetime.datetime.utcnow()
    now_time = now.time().replace(second=0, microsecond=0)

    quiz_times = [datetime.time(10, 5), datetime.time(13, 35), datetime.time(18, 39)]
    alert_times = [(datetime.datetime.combine(now.date(), qt) - datetime.timedelta(minutes=10)).time() for qt in quiz_times]

    print(f"[DEBUG] Teraz jest: {now_time.strftime('%H:%M')} UTC")
    print(f"[DEBUG] Zaplanowane quizy: {[qt.strftime('%H:%M') for qt in quiz_times]}")
    print(f"[DEBUG] Zaplanowane alerty: {[a.strftime('%H:%M') for a in alert_times]}")
    print(f"[DEBUG] Już dziś uruchomione: {[qt.strftime('%H:%M') for qt in fired_times_today]}")

    if quiz_channel is None:
        print("[WARNING] quiz_channel == None")
        return

    if now_time in alert_times:
        print("[DEBUG] Wysyłam alert: za 10 minut quiz")
        await quiz_channel.send("\U0001F9E0 Za 10 minut pojawi się pytanie quizowe! Bądźcie w gotowości!")

    for qt in quiz_times:
        if now_time == qt and qt not in fired_times_today:
            print(f"[QUIZ] Wywołuję quiz o godzinie {qt.strftime('%H:%M')}")
            await run_quiz(quiz_channel)
            fired_times_today.add(qt)

    if now.hour == 0 and fired_times_today:
        fired_times_today.clear()
        print("[INFO] Wyczyściłem fired_times_today na nowy dzień")

# === KEEP-ALIVE SERVER ===
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
    server = HTTPServer(('0.0.0.0', 8081), PingHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

# === START BOTA ===
if __name__ == "__main__":
    run_ping_server()

    @bot.event
    async def on_ready():
        global quiz_channel
        print(f"[INFO] Bot online jako: {bot.user}")
        guild = bot.get_guild(GUILD_ID)
        if guild:
            print(f"[INFO] Połączono z serwerem: {guild.name}")
        else:
            print(f"[ERROR] Nie znaleziono serwera o ID: {GUILD_ID}")

        quiz_channel = discord.utils.get(guild.text_channels, name="quiz-naruto")
        if quiz_channel:
            print(f"[INFO] Kanał #quiz znaleziony: {quiz_channel.id}")
        else:
            print(f"[ERROR] Nie znaleziono kanału #quiz")

        bot.add_view(QuizView("A"))

        if not daily_quiz_task.is_running():
            print("[INFO] Uruchamiam automatyczne quizy...")
            daily_quiz_task.start()

    bot.run(TOKEN)
