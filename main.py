# main.py
import os
import json
import asyncio
import random
import logging
import datetime
import threading
from typing import Dict, Any, Optional, List, Set

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import ButtonStyle, Interaction, ui, app_commands
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- optional for local dev ---
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# ---------------- Logging ----------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("quizbot")

# -------------- ENV validation -------------
def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required ENV var: {name}")
    return v

# Kill-switch
if os.getenv("BOT_DISABLED", "").lower() == "true":
    log.warning("BOT_DISABLED=true → wychodzę.")
    raise SystemExit(0)

TOKEN = require_env("TOKEN")
GUILD_ID = int(require_env("GUILD_ID"))
SUPABASE_URL = require_env("SUPABASE_URL")
SUPABASE_KEY = require_env("SUPABASE_KEY")

QUIZ_CHANNEL_NAME = os.getenv("QUIZ_CHANNEL_NAME", "quiz-naruto")
QUESTIONS_FILE = os.getenv("QUESTIONS_FILE", "pytania.json")
QUIZ_DURATION_SECONDS = int(os.getenv("QUIZ_DURATION_SECONDS", "900"))  # 15 min
ALERT_MINUTES_BEFORE = int(os.getenv("ALERT_MINUTES_BEFORE", "10"))
QUIZ_TIMES_ENV = os.getenv("QUIZ_TIMES", "10:05,13:35,18:39")

QUIZ_ROLE_ID = os.getenv("QUIZ_ROLE_ID")
QUIZ_ROLE_NAME = os.getenv("QUIZ_ROLE_NAME", "Quizowicz")
PING_ROLE_IN_ALERTS = os.getenv("PING_ROLE_IN_ALERTS", "true").lower() == "true"

COOLDOWN_HOURS = 168
LIFELINE_TYPES = {"5050", "publika", "telefon"}
last_quiz_id_per_channel: Dict[int, int] = {}

def _fmt_td(td: datetime.timedelta) -> str:
    secs = int(td.total_seconds())
    if secs <= 0:
        return "0s"
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s and not d: parts.append(f"{s}s")
    return " ".join(parts) or "0s"

def _cooldown_remaining(last_used: datetime.datetime, hours: int) -> datetime.timedelta:
    end = last_used + datetime.timedelta(hours=hours)
    return end - datetime.datetime.utcnow()

def get_state_for_channel(channel_id: int) -> Optional["QuizState"]:
    mid = last_quiz_id_per_channel.get(channel_id)
    if not mid:
        return None
    return active_quizzes.get(mid)

# -------------- Discord setup --------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# -------------- Supabase client --------------
from supabase import create_client, Client  # type: ignore
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# -------------- Data models / state ----------------
class QuizState:
    def __init__(
        self,
        message_id: int,
        channel_id: int,
        correct_index: int,
        end_time: datetime.datetime,
        options_labels: List[str],
        voters_per_option: Dict[int, Set[int]],
    ):
        self.message_id = message_id
        self.channel_id = channel_id
        self.correct_index = correct_index
        self.end_time = end_time  # UTC
        self.options_labels = options_labels
        self.voters_per_option = voters_per_option  # idx -> set(user_id)
        self.used_5050: Set[int] = set()     # users who already used 50/50 on this quiz

    def total_votes(self) -> int:
        return sum(len(s) for s in self.voters_per_option.values())

# active quiz states by message id
active_quizzes: Dict[int, QuizState] = {}

# -------------- Questions --------------
def load_questions() -> List[Dict[str, Any]]:
    if not os.path.exists(QUESTIONS_FILE):
        raise FileNotFoundError(f"Brak pliku z pytaniami: {QUESTIONS_FILE}")
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# -------------- Supabase helpers --------------
async def sb_get_last_lifeline_usage(user_id: int, lifeline: str) -> Optional[datetime.datetime]:
    # select last used_at
    try:
        data = (
            supabase.table("lifelines_usage")
            .select("used_at")
            .eq("user_id", str(user_id))
            .eq("type", lifeline)
            .order("used_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = data.data or []
        if rows:
            return datetime.datetime.fromisoformat(rows[0]["used_at"].replace("Z", "+00:00")).replace(tzinfo=None)
        return None
    except Exception as e:
        log.exception("SB lifeline select err: %r", e)
        return None

async def sb_insert_lifeline_usage(user_id: int, lifeline: str) -> None:
    try:
        supabase.table("lifelines_usage").insert(
            {"user_id": str(user_id), "type": lifeline, "used_at": datetime.datetime.utcnow().isoformat()+"Z"}
        ).execute()
    except Exception as e:
        log.exception("SB lifeline insert err: %r", e)

async def sb_get_used_questions() -> Set[int]:
    try:
        data = supabase.table("used_questions").select("question_id").execute()
        return {int(r["question_id"]) for r in (data.data or [])}
    except Exception as e:
        log.exception("SB used_questions select err: %r", e)
        return set()

async def sb_add_used_question(qid: int) -> None:
    try:
        supabase.table("used_questions").insert({"question_id": qid}).execute()
    except Exception as e:
        log.exception("SB add used_question err: %r", e)

async def sb_upsert_ranking(user_id: int, name: str, delta_points: int) -> None:
    try:
        # upsert by user_id
        payload = {
            "user_id": str(user_id),
            "name": name,
            "points": delta_points,
            "weekly": {},   # placeholder - zachowujemy format
            "monthly": {}
        }
        supabase.table("ranking").upsert(payload, on_conflict="user_id").execute()
    except Exception as e:
        log.error("DB save ranking error: %r", e)

# -------------- Buttons view --------------
class QuizButtons(ui.View):
    def __init__(self, message_id: int, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.message_id = message_id

    async def _register_vote(self, interaction: Interaction, option_index: int):
        state = active_quizzes.get(self.message_id)
        if not state:
            await interaction.response.send_message("Ten quiz już się zakończył.", ephemeral=True)
            return

        if datetime.datetime.utcnow() >= state.end_time:
            await interaction.response.send_message("Czas na odpowiedź minął.", ephemeral=True)
            return

        # usuń poprzedni głos usera, jeśli był
        for idx, voters in state.voters_per_option.items():
            if interaction.user.id in voters:
                voters.remove(interaction.user.id)

        state.voters_per_option[option_index].add(interaction.user.id)
        await interaction.response.send_message("Zapisano odpowiedź ✅", ephemeral=True)

    @ui.button(label="A", style=ButtonStyle.primary)
    async def a(self, interaction: Interaction, button: ui.Button):
        await self._register_vote(interaction, 0)

    @ui.button(label="B", style=ButtonStyle.primary)
    async def b(self, interaction: Interaction, button: ui.Button):
        await self._register_vote(interaction, 1)

    @ui.button(label="C", style=ButtonStyle.primary)
    async def c(self, interaction: Interaction, button: ui.Button):
        await self._register_vote(interaction, 2)

    @ui.button(label="D", style=ButtonStyle.primary)
    async def d(self, interaction: Interaction, button: ui.Button):
        await self._register_vote(interaction, 3)

class QuizPersistentView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="A", style=ButtonStyle.primary, custom_id="quiz:a")
    async def a(self, interaction: Interaction, button: ui.Button):
        await self._handle(interaction, 0)

    @ui.button(label="B", style=ButtonStyle.primary, custom_id="quiz:b")
    async def b(self, interaction: Interaction, button: ui.Button):
        await self._handle(interaction, 1)

    @ui.button(label="C", style=ButtonStyle.primary, custom_id="quiz:c")
    async def c(self, interaction: Interaction, button: ui.Button):
        await self._handle(interaction, 2)

    @ui.button(label="D", style=ButtonStyle.primary, custom_id="quiz:d")
    async def d(self, interaction: Interaction, button: ui.Button):
        await self._handle(interaction, 3)

    async def _handle(self, interaction: Interaction, idx: int):
        # znajdź stan po message_id
        msg = interaction.message
        if not msg:
            await interaction.response.send_message("Brak kontekstu wiadomości.", ephemeral=True)
            return
        state = active_quizzes.get(msg.id)
        if not state:
            await interaction.response.send_message("Ten quiz już się zakończył.", ephemeral=True)
            return
        if datetime.datetime.utcnow() >= state.end_time:
            await interaction.response.send_message("Czas na odpowiedź minął.", ephemeral=True)
            return
        # usuń poprzedni głos
        for voters in state.voters_per_option.values():
            voters.discard(interaction.user.id)
        state.voters_per_option[idx].add(interaction.user.id)
        await interaction.response.send_message("Zapisano odpowiedź ✅", ephemeral=True)

# -------------- Announce / role mention --------------
def _role_mention(guild: discord.Guild) -> str:
    if QUIZ_ROLE_ID:
        role = guild.get_role(int(QUIZ_ROLE_ID))
    else:
        role = discord.utils.get(guild.roles, name=QUIZ_ROLE_NAME)
    return role.mention if role and PING_ROLE_IN_ALERTS else ""

# -------------- Quiz logic --------------
async def post_quiz(channel: discord.TextChannel):
    questions = load_questions()
    used = await sb_get_used_questions()
    pool = [q for q in questions if int(q.get("id", -1)) not in used] or questions

    q = random.choice(pool)
    correct_index = int(q.get("correct", 0))
    options = q.get("options", [])
    if len(options) != 4:
        raise ValueError("Pytanie musi mieć dokładnie 4 odpowiedzi.")

    # embed
    embed = discord.Embed(color=discord.Color.orange())
    embed.set_author(name="Pytanie:")
    nl = "\n"
    body = (
        f"**{q['question']}**\n\n"
        f":regional_indicator_a: {options[0]}\n"
        f":regional_indicator_b: {options[1]}\n"
        f":regional_indicator_c: {options[2]}\n"
        f":regional_indicator_d: {options[3]}"
    )
    embed.description = body

    end_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=QUIZ_DURATION_SECONDS)
    footer = f"Kliknij przycisk z odpowiedzią poniżej. Masz {QUIZ_DURATION_SECONDS//60} min na odpowiedź!"
    embed.set_footer(text=footer)

    view = QuizButtons(message_id=0, timeout=None)
    msg = await channel.send(content=_role_mention(channel.guild) + " **Pytanie quizowe:**", embed=embed, view=view)
    view.message_id = msg.id

    # zapisz stan
    voters = {0: set(), 1: set(), 2: set(), 3: set()}
    state = QuizState(message_id=msg.id, channel_id=channel.id, correct_index=correct_index,
                      end_time=end_time, options_labels=["A", "B", "C", "D"], voters_per_option=voters)
    active_quizzes[msg.id] = state
    last_quiz_id_per_channel[channel.id] = msg.id

    # dodaj used_questions
    qid = int(q.get("id", -1))
    if qid != -1:
        await sb_add_used_question(qid)

    # schedule zakończenie
    asyncio.create_task(finish_quiz_after(state, msg, q))

async def finish_quiz_after(state: QuizState, msg: discord.Message, q: Dict[str, Any]):
    # czekaj do końca
    now = datetime.datetime.utcnow()
    delay = max(0, int((state.end_time - now).total_seconds()))
    await asyncio.sleep(delay)

    # zlicz
    correct = state.correct_index
    totals = [len(state.voters_per_option[i]) for i in range(4)]
    total_votes = sum(totals) or 1  # unik dzielenia przez zero
    perc = [round(100*t/total_votes) for t in totals]

    # przygotuj embed z wynikami
    options = q["options"]
    correct_letter = ["A","B","C","D"][correct]

    results = (
        f"**Poprawna:** {correct_letter} — {options[correct]}\n"
        f"Głosy: A={totals[0]} ({perc[0]}%), B={totals[1]} ({perc[1]}%), "
        f"C={totals[2]} ({perc[2]}%), D={totals[3]} ({perc[3]}%)"
    )

    embed = discord.Embed(color=discord.Color.green(), title="Wynik pytania")
    embed.description = results

    try:
        await msg.reply(embed=embed)
    except Exception:
        pass

    # sprzątanie
    active_quizzes.pop(msg.id, None)

# -------------- Lifelines (slash) --------------
async def _check_cooldown(user_id: int, lifeline: str) -> Optional[str]:
    last = await sb_get_last_lifeline_usage(user_id, lifeline)
    if last is None:
        return None
    left = _cooldown_remaining(last, COOLDOWN_HOURS)
    if left.total_seconds() > 0:
        return f"To koło będzie dostępne za **{_fmt_td(left)}**."
    return None

async def _ensure_active_quiz(interaction: Interaction) -> Optional[QuizState]:
    state = get_state_for_channel(interaction.channel_id)
    if not state:
        await interaction.response.send_message("Brak aktywnego pytania na tym kanale.", ephemeral=True)
        return None
    if datetime.datetime.utcnow() >= state.end_time:
        await interaction.response.send_message("Czas na odpowiedź już minął.", ephemeral=True)
        return None
    return state

@bot.tree.command(name="polnapol", description="Koło ratunkowe 50/50")
async def polnapol_cmd(interaction: Interaction):
    state = await _ensure_active_quiz(interaction)
    if not state:
        return

    # cooldown
    cd = await _check_cooldown(interaction.user.id, "5050")
    if cd:
        await interaction.response.send_message(cd, ephemeral=True)
        return

    # nie pozwól użyć 2x w tym samym pytaniu
    if interaction.user.id in state.used_5050:
        await interaction.response.send_message("Już użyłeś 50/50 w tym pytaniu.", ephemeral=True)
        return

    wrong_indices = [i for i in range(4) if i != state.correct_index]
    to_hide = set(random.sample(wrong_indices, 2))
    state.used_5050.add(interaction.user.id)

    # zapisz użycie
    await sb_insert_lifeline_usage(interaction.user.id, "5050")

    # zbuduj info dla usera
    letters = ["A","B","C","D"]
    hidden = ", ".join(letters[i] for i in sorted(to_hide))
    await interaction.response.send_message(
        f"🔎 50/50: odrzucam dwie błędne odpowiedzi → **{hidden}**.",
        ephemeral=True
    )

@bot.tree.command(name="publika", description="Koło ratunkowe: pytanie do publiczności")
async def publika_cmd(interaction: Interaction):
    state = await _ensure_active_quiz(interaction)
    if not state:
        return

    cd = await _check_cooldown(interaction.user.id, "publika")
    if cd:
        await interaction.response.send_message(cd, ephemeral=True)
        return

    # policz aktualne głosy
    totals = [len(state.voters_per_option[i]) for i in range(4)]
    total_votes = sum(totals) or 1
    perc = [round(100*t/total_votes) for t in totals]

    await sb_insert_lifeline_usage(interaction.user.id, "publika")

    await interaction.response.send_message(
        f"📊 Głosy publiczności: A={perc[0]}%, B={perc[1]}%, C={perc[2]}%, D={perc[3]}%.",
        ephemeral=True
    )

def _friend_text(user: discord.User, answer_letter: str) -> str:
    templates = [
        "Słuchaj, nie jestem pewien, ale wydaje mi się, że to będzie **{ans}**.",
        "Kurczę… myślę, że **{ans}**. Nie dam sobie ręki uciąć, ale brzmi najlepiej.",
        "Jakbym miał zgadywać, to **{ans}**. Na 60–70%!",
        "Chyba **{ans}**. Serio, tak mi świta w głowie.",
        "Moim zdaniem **{ans}**, ale nie obrażaj się, jak będzie inaczej 😅",
    ]
    base = random.choice(templates)
    return f"📞 Telefon do przyjaciela → {user.display_name} zaznaczył(a): **{answer_letter}**\n{base.format(ans=answer_letter)}"

@bot.tree.command(name="telefon", description="Koło ratunkowe: telefon do przyjaciela (pokaż swoją zaznaczoną odpowiedź)")
async def telefon_cmd(interaction: Interaction):
    state = await _ensure_active_quiz(interaction)
    if not state:
        return

    cd = await _check_cooldown(interaction.user.id, "telefon")
    if cd:
        await interaction.response.send_message(cd, ephemeral=True)
        return

    # znajdź zaznaczenie użytkownika
    user_choice: Optional[int] = None
    for idx, voters in state.voters_per_option.items():
        if interaction.user.id in voters:
            user_choice = idx
            break

    await sb_insert_lifeline_usage(interaction.user.id, "telefon")

    letters = ["A", "B", "C", "D"]
    if user_choice is None:
        await interaction.response.send_message(
            "📞 Telefon do przyjaciela: jeszcze nic nie zaznaczyłeś. Zrób to najpierw przyciskami A–D.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            _friend_text(interaction.user, letters[user_choice]),
            ephemeral=True
        )

@bot.tree.command(name="mojekola", description="Pokaż własne cooldowny kół ratunkowych")
async def mojekola_cmd(interaction: Interaction):
    # trzy koła
    lines: List[str] = []
    now = datetime.datetime.utcnow()
    for lf in ("5050", "publika", "telefon"):
        last = await sb_get_last_lifeline_usage(interaction.user.id, lf)
        if not last:
            lines.append(f"**{lf}**: dostępne ✅ (jeszcze nie używałeś)")
        else:
            left = _cooldown_remaining(last, COOLDOWN_HOURS)
            if left.total_seconds() > 0:
                lines.append(f"**{lf}**: cooldown **{_fmt_td(left)}** ⏳")
            else:
                lines.append(f"**{lf}**: dostępne ✅")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)

# -------------- Ping (debug) --------------
@bot.tree.command(name="ping", description="Sprawdź, czy bot żyje")
async def ping_cmd(interaction: Interaction):
    await interaction.response.send_message(f"Pong! Latency ~ {round(bot.latency*1000)}ms", ephemeral=True)

# -------------- Daily quiz scheduler --------------
def _parse_quiz_times(env: str) -> List[datetime.time]:
    parts = [p.strip() for p in env.split(",") if p.strip()]
    out: List[datetime.time] = []
    for p in parts:
        try:
            h, m = p.split(":")
            out.append(datetime.time(hour=int(h), minute=int(m)))
        except Exception:
            log.warning("Niepoprawna godzina w QUIZ_TIMES: %r", p)
    return out or [datetime.time(20, 0)]

QUIZ_TIMES = _parse_quiz_times(QUIZ_TIMES_ENV)

async def _get_quiz_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = discord.utils.get(guild.text_channels, name=QUIZ_CHANNEL_NAME)
    return ch

@tasks.loop(minutes=1)
async def daily_quiz_task():
    try:
        await bot.wait_until_ready()
        now = datetime.datetime.now(datetime.timezone.utc)
        for guild in bot.guilds:
            ch = await _get_quiz_channel(guild)
            if not ch:
                continue
            local_now = datetime.datetime.utcnow()
            # prosty harmonogram: jeśli minuta == jedna z ustalonych, wyślij
            if local_now.minute in {t.minute for t in QUIZ_TIMES} and local_now.hour in {t.hour for t in QUIZ_TIMES}:
                # żeby nie wysyłać wielokrotnie, sprawdź ostatnie 70s
                state = get_state_for_channel(ch.id)
                if state and (datetime.datetime.utcnow() - (state.end_time - datetime.timedelta(seconds=QUIZ_DURATION_SECONDS))).total_seconds() < 70:
                    continue
                # ping roli (opcjonalnie)
                await post_quiz(ch)
    except Exception as e:
        log.exception("daily_quiz_task err: %r", e)

# -------------- Health server + watchdog ------
class PingHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _ok_headers(self):
        self.send_response(200)
        self.send_header("Content-Type","text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/healthz", "/", "/health"):
            self._ok_headers()
            try:
                self.wfile.write(b"ok")
            except BrokenPipeError:
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        if self.path in ("/healthz", "/", "/health"):
            self._ok_headers()
        else:
            self.send_response(404)
            self.end_headers()

def run_health_server():
    port = int(os.getenv("PORT", "8081"))
    log.info("Start health server on 0.0.0.0:%s", port)
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()

@tasks.loop(seconds=30)
async def watchdog():
    try:
        latency = bot.latency
        if latency is None or latency > 180:
            log.error("Watchdog wykryl problem z pingiem (%s). Restart procesu.", latency)
            os._exit(1)
    except Exception:
        os._exit(1)

# -------------- Self-uptime ping ------------
@tasks.loop(minutes=5)
async def uptime_ping():
    url = "https://naruto-quiz-bot.onrender.com/healthz"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent":"NarutoQuizBot/keepalive"}) as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    log.info("Uptime ping OK (%s)", url)
                elif resp.status == 429:
                    log.warning("Uptime ping rate-limited (429) — spróbuję później.")
                else:
                    log.warning("Uptime ping FAIL %s (%s)", url, resp.status)
    except Exception as e:
        log.warning("Uptime ping exception: %r", e)

@uptime_ping.before_loop
async def uptime_ping_before_loop():
    await bot.wait_until_ready()
    await asyncio.sleep(60)

# -------------- Ready & startup --------------
@bot.event
async def on_ready():
    log.info("Zalogowano jako %s (%s)", bot.user, bot.user.id if bot.user else "?")
    bot.add_view(QuizPersistentView())
    if not daily_quiz_task.is_running():
        daily_quiz_task.start()
    if not watchdog.is_running():
        watchdog.start()
    if not uptime_ping.is_running():
        uptime_ping.start()
    try:
        await bot.tree.sync()
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
        names = [cmd.name for cmd in bot.tree.get_commands()]
        log.info("Slash commands synced. Global list: %s", names)
    except Exception as e:
        log.exception("Slash sync error: %r", e)

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
