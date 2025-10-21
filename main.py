# main.py
import os
import json
import asyncio
import random
import logging
import datetime
import threading
from typing import Dict, Any, Optional, List, Set

import discord
from discord.ext import commands, tasks
from discord import ButtonStyle, Interaction, ui
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
QUIZ_DURATION_SECONDS = int(os.getenv("QUIZ_DURATION_SECONDS", "900"))  # domyślnie 15 min
ALERT_MINUTES_BEFORE = int(os.getenv("ALERT_MINUTES_BEFORE", "10"))
# Godziny w UTC, np. "10:05,13:35,18:39"
QUIZ_TIMES_ENV = os.getenv("QUIZ_TIMES", "10:05,13:35,18:39")

# >>> LIFELINES PATCH START: stałe i pomocniki
COOLDOWN_HOURS = 168  # 7 dni cooldownu – osobno dla każdego koła i użytkownika
LIFELINE_TYPES = {"5050", "publika", "telefon"}

# ostatni aktywny quiz na każdym kanale (żeby komendy wiedziały, którego quizu dotyczą)
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
    if s and not d: parts.append(f"{s}s")  # sekundy pokazuj tylko gdy < 1 dzień
    return " ".join(parts) or "0s"

def _cooldown_remaining(last_used: datetime.datetime, hours: int) -> datetime.timedelta:
    end = last_used + datetime.timedelta(hours=hours)
    return end - datetime.datetime.utcnow()

def get_state_for_channel(channel_id: int) -> Optional["QuizState"]:
    mid = last_quiz_id_per_channel.get(channel_id)
    if not mid:
        return None
    return active_quizzes.get(mid)
# >>> LIFELINES PATCH END

# -------------- Discord setup --------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------- Supabase client --------------
from supabase import create_client, Client  # type: ignore
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Async wrapper, bo supabase-py jest synchroniczny
async def sb_call(fn, *args, **kwargs):
    """Run a supabase-py chain with .execute() in a thread."""
    def _runner():
        q = fn(*args, **kwargs)
        if hasattr(q, "execute"):
            return q.execute()
        res = q
        if hasattr(res, "execute"):
            return res.execute()
        return res
    return await asyncio.to_thread(_runner)

# -------------- DB helpers -------------------
async def db_get_used_ids() -> Set[int]:
    try:
        resp = await asyncio.to_thread(lambda: supabase.table("used_questions").select("question_id").execute())
        return {int(row["question_id"]) for row in (resp.data or [])}
    except Exception as e:
        log.error("DB get used ids error: %r", e)
        return set()

async def db_add_used_id(qid: int) -> None:
    try:
        await asyncio.to_thread(lambda: supabase.table("used_questions").insert({"question_id": qid}).execute())
    except Exception as e:
        log.error("DB add used id error: %r", e)

async def db_clear_used_questions() -> None:
    try:
        await asyncio.to_thread(lambda: supabase.table("used_questions").delete().neq("id", 0).execute())
    except Exception as e:
        log.error("DB clear used error: %r", e)

async def db_load_ranking() -> Dict[str, Dict[str, Any]]:
    """Zwraca słownik: user_id -> {name, points, weekly, monthly}"""
    try:
        resp = await asyncio.to_thread(lambda: supabase.table("ranking").select("*").execute())
        out: Dict[str, Dict[str, Any]] = {}
        for row in (resp.data or []):
            uid = str(row["user_id"])
            out[uid] = {
                "name": row.get("name") or "",
                "points": int(row.get("points") or 0),
                "weekly": row.get("weekly") or {},
                "monthly": row.get("monthly") or {},
            }
        return out
    except Exception as e:
        log.error("DB load ranking error: %r", e)
        return {}

async def db_save_ranking(data: Dict[str, Dict[str, Any]]) -> None:
    """Upsert całego słownika rankingowego."""
    try:
        payload = []
        for uid, d in data.items():
            payload.append({
                "user_id": uid,
                "name": d.get("name", ""),
                "points": int(d.get("points", 0)),
                "weekly": d.get("weekly") or {},
                "monthly": d.get("monthly") or {},
            })
        await asyncio.to_thread(lambda: supabase.table("ranking").upsert(payload, on_conflict="user_id").execute())
    except Exception as e:
        log.error("DB save ranking error: %r", e)

# >>> LIFELINES PATCH START: DB helpers dla cooldownów
async def db_lifeline_last_used(user_id: int, lifeline_type: str) -> Optional[datetime.datetime]:
    """Zwraca ostatni timestamp użycia koła (UTC naive) albo None."""
    try:
        resp = await asyncio.to_thread(
            lambda: supabase.table("lifelines_usage")
            .select("used_at")
            .eq("user_id", str(user_id))
            .eq("type", lifeline_type)
            .order("used_at", desc=True)
            .limit(1)
            .execute()
        )
        data = getattr(resp, "data", None) or (resp.get("data", []) if isinstance(resp, dict) else [])
        if data:
            iso = data[0]["used_at"]
            if isinstance(iso, str):
                if iso.endswith("Z"):
                    iso = iso[:-1] + "+00:00"
                dt = datetime.datetime.fromisoformat(iso).astimezone(datetime.timezone.utc).replace(tzinfo=None)
                return dt
    except Exception as e:
        log.exception("db_lifeline_last_used error: %r", e)
    return None

async def db_lifeline_mark_use(user_id: int, lifeline_type: str) -> None:
    """Zapisuje użycie koła w DB."""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("lifelines_usage")
            .insert({
                "user_id": str(user_id),
                "type": lifeline_type,
                "used_at": datetime.datetime.utcnow().isoformat() + "Z",
            })
            .execute()
        )
    except Exception as e:
        log.exception("db_lifeline_mark_use error: %r", e)

async def lifeline_check_cooldown(user_id: int, lifeline_type: str) -> Optional[str]:
    """Jeśli cooldown trwa, zwraca tekst (np. '6d 3h'); jeśli można użyć — None."""
    last = await db_lifeline_last_used(user_id, lifeline_type)
    if not last:
        return None
    rem = _cooldown_remaining(last, COOLDOWN_HOURS)
    if rem.total_seconds() > 0:
        return _fmt_td(rem)
    return None
# >>> LIFELINES PATCH END

# -------------- Pytania ----------------------

def load_questions() -> List[Dict[str, Any]]:
    if not os.path.exists(QUESTIONS_FILE):
        raise FileNotFoundError(f"Brak pliku z pytaniami: {QUESTIONS_FILE}")
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # spodziewany format:
    # [{ "id": 1, "question": "...", "options": {"A":"...", "B":"...", "C":"...", "D":"..."}, "answer": "B" }, ...]
    normalized = []
    for q in data:
        try:
            qid = int(q["id"])
            question = str(q["question"])
            options = q["options"]
            correct = str(q["answer"]).strip().upper()
            assert correct in {"A","B","C","D"}
            normalized.append({
                "id": qid,
                "question": question,
                "options": options,
                "answer": correct
            })
        except Exception as e:
            log.warning("Pominięto pytanie o złym formacie: %r | err=%r", q, e)
    if not normalized:
        raise RuntimeError("Brak poprawnie wczytanych pytań.")
    return normalized

# -------------- Stan quizów ------------------

class QuizState:
    __slots__ = ("question", "message_id", "end_time", "answers")
    def __init__(self, question: Dict[str, Any], message_id: int, end_time: datetime.datetime):
        self.question = question
        self.message_id = message_id
        self.end_time = end_time  # UTC
        self.answers: Dict[int, str] = {}  # user_id -> "A"/"B"/"C"/"D"

active_quizzes: Dict[int, QuizState] = {}   # message_id -> state
finished_messages: Set[int] = set()         # kontrola anty-duplikat
quiz_lock = asyncio.Lock()

def fmt_timestr(dt: datetime.datetime) -> str:
    return dt.strftime("%H:%M:%S UTC")

# -------------- Widok z przyciskami ----------

class QuizPersistentView(ui.View):
    """Widok „persistent” – te same custom_id, żeby po restarcie dalej łapać kliki."""
    def __init__(self, disabled: bool=False):
        super().__init__(timeout=None)
        self._disabled = disabled
        # jeśli disabled=True, wyłącz przyciski w tym widoku
        if disabled:
            for child in self.children:
                try:
                    child.disabled = True
                except Exception:
                    pass

    def _maybe_disable(self, button: ui.Button) -> ui.Button:
        button.disabled = self._disabled
        return button

    @ui.button(label="A", custom_id="quiz_A", style=ButtonStyle.secondary)
    async def _a(self, interaction: Interaction, button: ui.Button):
        await handle_answer_click(interaction, "A")

    @ui.button(label="B", custom_id="quiz_B", style=ButtonStyle.secondary)
    async def _b(self, interaction: Interaction, button: ui.Button):
        await handle_answer_click(interaction, "B")

    @ui.button(label="C", custom_id="quiz_C", style=ButtonStyle.secondary)
    async def _c(self, interaction: Interaction, button: ui.Button):
        await handle_answer_click(interaction, "C")

    @ui.button(label="D", custom_id="quiz_D", style=ButtonStyle.secondary)
    async def _d(self, interaction: Interaction, button: ui.Button):
        await handle_answer_click(interaction, "D")

async def handle_answer_click(interaction: Interaction, letter: str):
    """Obsługa kliknięcia przycisku odpowiedzi."""
    mid = interaction.message.id if interaction.message else None
    if not mid:
        return await interaction.response.send_message("Brak powiązanego pytania.", ephemeral=True)
    state = active_quizzes.get(mid)
    now = datetime.datetime.utcnow()
    if not state:
        return await interaction.response.send_message("Ten quiz już nie przyjmuje odpowiedzi.", ephemeral=True)
    if now > state.end_time:
        return await interaction.response.send_message("Czas minął. Odpowiedzi po czasie nie są liczone.", ephemeral=True)
    uid = interaction.user.id
    if uid in state.answers:
        return await interaction.response.send_message("Masz już zapisaną odpowiedź.", ephemeral=True)
    state.answers[uid] = letter
    try:
        await interaction.response.send_message("Zapisano odpowiedź ✅", ephemeral=True)
    except discord.errors.InteractionResponded:
        pass

def build_question_message(q: Dict[str, Any]) -> str:
    return (
        f"**Pytanie:** {q['question']}\n\n"
        f":regional_indicator_a: {q['options']['A']}\n"
        f":regional_indicator_b: {q['options']['B']}\n"
        f":regional_indicator_c: {q['options']['C']}\n"
        f":regional_indicator_d: {q['options']['D']}\n\n"
        f"Kliknij przycisk z odpowiedzią poniżej. Masz {QUIZ_DURATION_SECONDS//60} min na odpowiedź!"
    )

async def conclude_quiz(channel: discord.TextChannel, state: QuizState):
    """Zamyka quiz, przyznaje punkty, ogłasza wynik."""
    if state.message_id in finished_messages:
        return
    finished_messages.add(state.message_id)

    correct = state.question["answer"]
    winners: List[int] = [uid for uid, letter in state.answers.items() if letter == correct]

    # update DB ranking
    ranking = await db_load_ranking()
    today = datetime.datetime.utcnow().date().isoformat()
    for uid in winners:
        uid_s = str(uid)
        member: Optional[discord.Member] = channel.guild.get_member(uid)
        name = member.display_name if member else f"Użytkownik {uid_s}"
        user_data = ranking.get(uid_s) or {"name": name, "points": 0, "weekly": {}, "monthly": {}}
        user_data["name"] = name
        user_data["points"] = int(user_data.get("points", 0)) + 1
        weekly = dict(user_data.get("weekly") or {})
        monthly = dict(user_data.get("monthly") or {})
        weekly[today] = int(weekly.get(today, 0)) + 1
        monthly[today] = int(monthly.get(today, 0)) + 1
        user_data["weekly"] = weekly
        user_data["monthly"] = monthly
        ranking[uid_s] = user_data
    await db_save_ranking(ranking)

    # Ogłoszenie wyniku
    if winners:
        mentions = ", ".join(f"<@{uid}>" for uid in winners)
        msg = (
            f"**Koniec czasu!**\n"
            f"Prawidłowa odpowiedź: **{correct}**\n"
            f"Gratulacje dla: {mentions} (+1 pkt)"
        )
    else:
        msg = (
            f"**Koniec czasu!**\n"
            f"Prawidłowa odpowiedź: **{correct}**\n"
            f"Nikt nie trafił tym razem."
        )

    try:
        message = await channel.fetch_message(state.message_id)
        # Zdezaktywuj przyciski
        try:
            await message.edit(view=QuizPersistentView(disabled=True))
        except Exception:
            pass
        await channel.send(msg)
    except discord.NotFound:
        await channel.send(msg)

# -------------- Uruchamianie quizu ------------

async def run_quiz(channel: discord.TextChannel):
    async with quiz_lock:
        questions = load_questions()
        used = await db_get_used_ids()
        available = [q for q in questions if int(q["id"]) not in used]
        if not available:
            log.info("Wszystkie pytania zostały wykorzystane – czyszczę used_questions.")
            await db_clear_used_questions()
            available = questions[:]  # po wyczyszczeniu bierzemy całą pulę

        question = random.choice(available)
        qid = int(question["id"])

        content = build_question_message(question)
        view = QuizPersistentView()
        msg = await channel.send(content, view=view)

        # >>> LIFELINES PATCH START: mapowanie kanał -> ostatni quiz
        try:
            last_quiz_id_per_channel[channel.id] = msg.id
        except Exception:
            pass
        # >>> LIFELINES PATCH END

        end_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=QUIZ_DURATION_SECONDS)
        state = QuizState(question=question, message_id=msg.id, end_time=end_time)
        active_quizzes[msg.id] = state

        log.info("Quiz wystartował (id=%s). Koniec: %s", qid, fmt_timestr(end_time))
        # po czasie – ogłoś wynik
        async def _finish():
            try:
                await asyncio.sleep(QUIZ_DURATION_SECONDS)
                await conclude_quiz(channel, state)
                await db_add_used_id(qid)
            finally:
                # porządek
                active_quizzes.pop(msg.id, None)
        asyncio.create_task(_finish())

# -------------- Komendy -----------------------

def _top_embed(title: str, pairs: List[tuple[str, int]]) -> discord.Embed:
    embed = discord.Embed(title=title, colour=0x2b7cff)
    if not pairs:
        embed.description = "Brak wyników."
        return embed
    for i, (name, pts) in enumerate(pairs[:10], start=1):
        embed.add_field(name=f"{i}. {name}", value=f"{pts} pkt", inline=False)
    return embed

@bot.command()
async def quiz(ctx: commands.Context):
    """Ręczne uruchomienie quizu"""
    if not isinstance(ctx.channel, discord.TextChannel):
        return await ctx.reply("Tylko na kanałach tekstowych.")
    await run_quiz(ctx.channel)

@bot.command()
async def ranking(ctx: commands.Context):
    data = await db_load_ranking()
    pairs = sorted(((v.get("name") or str(uid), int(v.get("points",0))) for uid, v in data.items()), key=lambda x: x[1], reverse=True)
    await ctx.send(embed=_top_embed("Ranking – All time", pairs))

def _sum_period(d: Dict[str, int], days: int) -> int:
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    total = 0
    for k, v in (d or {}).items():
        try:
            if datetime.date.fromisoformat(k) >= cutoff:
                total += int(v)
        except Exception:
            continue
    return total

@bot.command()
async def rankingweekly(ctx: commands.Context):
    data = await db_load_ranking()
    pairs = []
    for v in data.values():
        name = v.get("name") or "?"
        total = _sum_period(v.get("weekly") or {}, 7)
        if total:
            pairs.append((name, total))
    pairs.sort(key=lambda x: x[1], reverse=True)
    await ctx.send(embed=_top_embed("Ranking tygodniowy (7d)", pairs))

@bot.command()
async def rankingmonthly(ctx: commands.Context):
    data = await db_load_ranking()
    pairs = []
    for v in data.values():
        name = v.get("name") or "?"
        total = _sum_period(v.get("monthly") or {}, 30)
        if total:
            pairs.append((name, total))
    pairs.sort(key=lambda x: x[1], reverse=True)
    await ctx.send(embed=_top_embed("Ranking miesięczny (30d)", pairs))

@bot.command()
async def punkty(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    data = await db_load_ranking()
    uid = str(member.id)
    d = data.get(uid)
    if not d:
        return await ctx.reply(f"{member.display_name} ma 0 pkt.")
    await ctx.reply(f"{d.get('name') or member.display_name} ma **{int(d.get('points',0))}** pkt.")

# >>> LIFELINES PATCH START: 3 komendy kół (wynik w DM, cooldown 168h)

@bot.command(name="5050")
async def lifeline_5050(ctx: commands.Context):
    """Pół na pół – DM do używającego. Cooldown 168h per user."""
    if not isinstance(ctx.channel, discord.TextChannel):
        return await ctx.reply("Użyj na kanale tekstowym.", delete_after=8)
    state = get_state_for_channel(ctx.channel.id)
    if not state:
        return await ctx.reply("Brak aktywnego pytania.", delete_after=8)
    if datetime.datetime.utcnow() > state.end_time:
        return await ctx.reply("Czas na to pytanie już minął.", delete_after=8)

    cd = await lifeline_check_cooldown(ctx.author.id, "5050")
    if cd:
        return await ctx.reply(f"50/50 w cooldownie jeszcze {cd}.", delete_after=10)

    correct = state.question["answer"]
    wrong = [x for x in ["A","B","C","D"] if x != correct]
    kept = [correct, random.choice(wrong)]
    random.shuffle(kept)

    await db_lifeline_mark_use(ctx.author.id, "5050")
    try:
        await ctx.author.send(f"🔔 50/50 → zostały: **{kept[0]}** lub **{kept[1]}**")
    except discord.Forbidden:
        return await ctx.reply("Nie mogę wysłać Ci DM (włącz prywatne wiadomości).", delete_after=10)
    await ctx.reply("Wysłałem szczegóły w DM. 📩", delete_after=6)


@bot.command(name="publika")
async def lifeline_audience(ctx: commands.Context):
    """Pytanie do publiczności – procenty aktualnych głosów. DM + cooldown 168h."""
    if not isinstance(ctx.channel, discord.TextChannel):
        return await ctx.reply("Użyj na kanale tekstowym.", delete_after=8)
    state = get_state_for_channel(ctx.channel.id)
    if not state:
        return await ctx.reply("Brak aktywnego pytania.", delete_after=8)
    if datetime.datetime.utcnow() > state.end_time:
        return await ctx.reply("Czas na to pytanie już minął.", delete_after=8)

    cd = await lifeline_check_cooldown(ctx.author.id, "publika")
    if cd:
        return await ctx.reply(f"„Pytanie do publiczności” w cooldownie jeszcze {cd}.", delete_after=10)

    counts = {k: 0 for k in ["A", "B", "C", "D"]}
    for letter in state.answers.values():
        if letter in counts:
            counts[letter] += 1
    total = sum(counts.values()) or 1
    perc = {k: round(v * 100 / total) for k, v in counts.items()}

    await db_lifeline_mark_use(ctx.author.id, "publika")
    text = (
        "📊 Głosy do tej pory:\n"
        f"A: {counts['A']} ({perc['A']}%)\n"
        f"B: {counts['B']} ({perc['B']}%)\n"
        f"C: {counts['C']} ({perc['C']}%)\n"
        f"D: {counts['D']} ({perc['D']}%)"
    )
    try:
        await ctx.author.send(text)
    except discord.Forbidden:
        return await ctx.reply("Nie mogę wysłać Ci DM (włącz prywatne wiadomości).", delete_after=10)
    await ctx.reply("Wysłałem szczegóły w DM. 📩", delete_after=6)


@bot.command(name="telefon")
async def lifeline_phone(ctx: commands.Context, friend: Optional[discord.Member] = None):
    """Telefon do przyjaciela – pokaż w DM jaką literę zaznaczył wskazany gracz (jeśli już odpowiedział)."""
    if not isinstance(ctx.channel, discord.TextChannel):
        return await ctx.reply("Użyj na kanale tekstowym.", delete_after=8)
    state = get_state_for_channel(ctx.channel.id)
    if not state:
        return await ctx.reply("Brak aktywnego pytania.", delete_after=8)
    if datetime.datetime.utcnow() > state.end_time:
        return await ctx.reply("Czas na to pytanie już minął.", delete_after=8)
    if not friend:
        return await ctx.reply("Użycie: `!telefon @nick`", delete_after=8)

    cd = await lifeline_check_cooldown(ctx.author.id, "telefon")
    if cd:
        return await ctx.reply(f"„Telefon do przyjaciela” w cooldownie jeszcze {cd}.", delete_after=10)

    letter = state.answers.get(friend.id)
    await db_lifeline_mark_use(ctx.author.id, "telefon")
    msg = (
        f"{friend.display_name} **jeszcze nie odpowiedział(a)**."
        if not letter else
        f"{friend.display_name} zaznaczył(a): **{letter}**"
    )
    try:
        await ctx.author.send(f"📞 Telefon do przyjaciela → {msg}")
    except discord.Forbidden:
        return await ctx.reply("Nie mogę wysłać Ci DM (włącz prywatne wiadomości).", delete_after=10)
    await ctx.reply("Wysłałem szczegóły w DM. 📩", delete_after=6)

# >>> LIFELINES PATCH END

# -------------- Scheduler ---------------------

def _parse_quiz_times(spec: str) -> List[datetime.time]:
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h, m = part.split(":")
            out.append(datetime.time(int(h), int(m)))
        except Exception:
            log.warning("Niepoprawna godzina w QUIZ_TIMES: %r", part)
    return out or [datetime.time(10,5), datetime.time(13,35), datetime.time(18,39)]

_fired_today: Set[str] = set()
_last_reset_date: Optional[datetime.date] = None

@tasks.loop(minutes=1)
async def daily_quiz_task():
    global _last_reset_date
    now = datetime.datetime.utcnow()
    times = _parse_quiz_times(QUIZ_TIMES_ENV)
    # reset na zmianę dnia
    if _last_reset_date != now.date():
        _fired_today.clear()
        _last_reset_date = now.date()
    # alert
    for t in times:
        alert_dt = (datetime.datetime.combine(now.date(), t) - datetime.timedelta(minutes=ALERT_MINUTES_BEFORE)).time()
        if alert_dt.hour == now.hour and alert_dt.minute == now.minute:
            ch = await get_quiz_channel()
            if ch:
                await ch.send("🧠 Za {} minut pojawi się pytanie quizowe!".format(ALERT_MINUTES_BEFORE))
    # okno 2 minut
    for t in times:
        key = f"{t.hour:02d}:{t.minute:02d}"
        target = datetime.datetime.combine(now.date(), t)
        if abs((now - target)) < datetime.timedelta(minutes=2) and key not in _fired_today:
            ch = await get_quiz_channel()
            if ch:
                await run_quiz(ch)
                _fired_today.add(key)

# -------------- Health server + watchdog ------

class PingHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return  # wycisz standardowy log

    def do_GET(self):
        if self.path in ("/healthz", "/"):
            self.send_response(200)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
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
            os._exit(1)  # Render postawi nowy proces
    except Exception:
        os._exit(1)

# -------------- Utility -----------------------

_guild_cache: Optional[discord.Guild] = None
_channel_cache: Optional[discord.TextChannel] = None

async def get_quiz_channel() -> Optional[discord.TextChannel]:
    global _guild_cache, _channel_cache
    if _channel_cache:
        return _channel_cache
    if not _guild_cache:
        _guild_cache = bot.get_guild(GUILD_ID)
        if not _guild_cache:
            try:
                _guild_cache = await bot.fetch_guild(GUILD_ID)
            except Exception:
                return None
    # po nazwie
    ch = discord.utils.get(_guild_cache.text_channels, name=QUIZ_CHANNEL_NAME)
    if ch:
        _channel_cache = ch
        return ch
    # fallback po ID z ENV QUIZ_CHANNEL_ID
    ch_id = os.getenv("QUIZ_CHANNEL_ID")
    if ch_id:
        try:
            ch = _guild_cache.get_channel(int(ch_id))  # type: ignore
            if isinstance(ch, discord.TextChannel):
                _channel_cache = ch
                return ch
        except Exception:
            pass
    return None

# -------------- Events ------------------------

@bot.event
async def on_ready():
    log.info("Zalogowano jako %s (%s)", bot.user, bot.user.id if bot.user else "?")
    # rejestruj persistent view żeby po restarcie łapać kliki
    bot.add_view(QuizPersistentView())
    if not daily_quiz_task.is_running():
        daily_quiz_task.start()
    if not watchdog.is_running():
        watchdog.start()

def main():
    # health server
    threading.Thread(target=run_health_server, daemon=True).start()
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
