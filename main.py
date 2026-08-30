# main.py
import os
import json
import asyncio
import random
import logging
import datetime
import threading
import hashlib
from typing import Dict, Any, Optional, List, Set
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import ButtonStyle, Interaction, ui, app_commands
from http.server import BaseHTTPRequestHandler, HTTPServer
from discord.errors import HTTPException  # <— do obsługi 429

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
if os.getenv("BOT_DISABLED", "").lower() == "false":
    log.warning("BOT_DISABLED=true → wychodzę.")
    raise SystemExit(0)

TOKEN = require_env("TOKEN")
GUILD_ID = int(require_env("GUILD_ID"))
SUPABASE_URL = require_env("SUPABASE_URL")
SUPABASE_KEY = require_env("SUPABASE_KEY")

QUIZ_CHANNEL_NAME = os.getenv("QUIZ_CHANNEL_NAME", "quiz-naruto")
QUESTIONS_FILE = os.getenv("QUESTIONS_FILE", "pytania.json")
QUIZ_DURATION_SECONDS = int(os.getenv("QUIZ_DURATION_SECONDS", "900"))  # 15 min
ALERT_MINUTES_BEFORE = int(os.getenv("ALERT_MINUTES_BEFORE", "5"))

# Jeden automatyczny quiz dziennie o losowej godzinie czasu polskiego.
POLAND_TZ = ZoneInfo("Europe/Warsaw")
DAILY_QUIZ_START_HOUR = 10
DAILY_QUIZ_END_HOUR = 22
DAILY_QUIZ_RANDOM_SEED = os.getenv("DAILY_QUIZ_RANDOM_SEED", str(GUILD_ID))

# --- Punktacja zależna od kategorii pytania ---
POINTS_BY_DIFFICULTY = {
    "easy": 1,
    "medium": 2,
    "hard": 3,
    "nws": 2,
}

# --- WŁĄCZNIK/WYŁĄCZNIK REMINDERA PRZED QUIZEM ---
# Domyślnie włączony; można wyłączyć przez QUIZ_ALERTS_ENABLED=false.
QUIZ_ALERTS_ENABLED = os.getenv("QUIZ_ALERTS_ENABLED", "true").lower() == "true"

# --- Ping roli (@Quizowicz) ---
QUIZ_ROLE_ID = os.getenv("QUIZ_ROLE_ID")
QUIZ_ROLE_NAME = os.getenv("QUIZ_ROLE_NAME", "Quizowicz")
PING_ROLE_IN_ALERTS = os.getenv("PING_ROLE_IN_ALERTS", "true").lower() == "true"

# --- Lifelines / cooldown ---
COOLDOWN_HOURS = 168  # 7 dni
LIFELINE_TYPES = {"5050", "publika", "telefon"}

# ostatni aktywny quiz per kanał
last_quiz_id_per_channel: Dict[int, int] = {}

def _fmt_td(td: datetime.timedelta) -> str:
    secs = int(td.total_seconds())
    if secs <= 0:
        return "0s"
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s and not d:
        parts.append(f"{s}s")
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

# --- REPORT FEATURE: helper do DM do ownera ---
async def _send_report_to_owner(content: str) -> bool:
    try:
        app_info = await bot.application_info()
        owner = app_info.owner
        if owner:
            try:
                await owner.send(content)
                return True
            except Exception:
                ch_id = os.getenv("REPORT_CHANNEL_ID")
                if ch_id:
                    guild = bot.get_guild(GUILD_ID) or await bot.fetch_guild(GUILD_ID)
                    ch = guild.get_channel(int(ch_id)) if guild else None  # type: ignore
                    if isinstance(ch, discord.TextChannel):
                        await ch.send(content)
                        return True
                return False
        return False
    except Exception:
        log.exception("Report: nie udało się pobrać application_info / wysłać DM")
        return False
# --- END REPORT FEATURE ---

# -------------- Supabase client --------------
from supabase import create_client, Client  # type: ignore
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# -------------- DB helpers -------------------
class SupabaseOperationError(RuntimeError):
    """Kontrolowany błąd operacji bazodanowej, bez cichego fail-open."""


async def _db_call(operation: str, callback):
    try:
        return await asyncio.to_thread(callback)
    except Exception as e:
        log.exception("Supabase operation failed [%s]: %r", operation, e)
        raise SupabaseOperationError(operation) from e


async def db_get_used_ids() -> Set[int]:
    resp = await _db_call(
        "used_questions.select",
        lambda: supabase.table("used_questions").select("question_id").execute(),
    )
    return {int(row["question_id"]) for row in (resp.data or [])}

async def db_add_used_id(qid: int) -> None:
    await _db_call(
        "used_questions.insert",
        lambda: supabase.table("used_questions").insert({"question_id": qid}).execute(),
    )

async def db_clear_used_questions() -> None:
    await _db_call(
        "used_questions.clear",
        lambda: supabase.table("used_questions").delete().neq("id", 0).execute(),
    )

def _is_unique_violation(error: Exception) -> bool:
    """Rozpoznaje konflikt z unikalnym indeksem Supabase/Postgresa."""
    current: Optional[BaseException] = error
    while current is not None:
        code = getattr(current, "code", None)
        error_text = str(current).lower()
        if code == "23505" or "23505" in error_text or "duplicate key" in error_text:
            return True
        current = current.__cause__
    return False

async def db_claim_daily_quiz(
    guild_id: int,
    channel_id: int,
    local_date: datetime.date,
    scheduled_for: datetime.datetime,
) -> Optional[bool]:
    """
    Atomowo rezerwuje dzienny quiz.

    True  – rezerwacja utworzona, można opublikować quiz.
    False – wpis już istnieje, więc quiz został wcześniej zarezerwowany.
    None  – baza jest niedostępna; scheduler ponowi próbę za minutę.
    """
    payload = {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "date_local": local_date.isoformat(),
        "scheduled_for": scheduled_for.astimezone(datetime.timezone.utc).isoformat(),
    }
    try:
        await asyncio.to_thread(
            lambda: supabase.table("fired_quizzes").insert(payload).execute()
        )
        return True
    except Exception as e:
        if _is_unique_violation(e):
            return False
        log.exception("Supabase operation failed [fired_quizzes.insert]: %r", e)
        return None

async def db_release_daily_quiz(guild_id: int, local_date: datetime.date) -> None:
    """Usuwa rezerwację, jeżeli publikacja quizu faktycznie się nie udała."""
    try:
        await _db_call(
            "fired_quizzes.delete",
            lambda: supabase.table("fired_quizzes")
            .delete()
            .eq("guild_id", str(guild_id))
            .eq("date_local", local_date.isoformat())
            .execute()
        )
    except Exception as e:
        log.error("DB release daily quiz error: %r", e)

async def db_load_ranking() -> Dict[str, Dict[str, Any]]:
    resp = await _db_call(
        "ranking.select",
        lambda: supabase.table("ranking").select("*").execute(),
    )
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

async def db_save_ranking(data: Dict[str, Dict[str, Any]]) -> None:
    payload = []
    for uid, d in data.items():
        payload.append({
            "user_id": uid,
            "name": d.get("name", ""),
            "points": int(d.get("points", 0)),
            "weekly": d.get("weekly") or {},
            "monthly": d.get("monthly") or {},
        })
    await _db_call(
        "ranking.upsert",
        lambda: supabase.table("ranking").upsert(payload, on_conflict="user_id").execute(),
    )

# --- Lifelines: DB helpers (cooldown) ---
async def db_lifeline_last_used(user_id: int, lifeline_type: str) -> Optional[datetime.datetime]:
    resp = await _db_call(
        "lifelines_usage.select",
        lambda: supabase.table("lifelines_usage")
        .select("used_at")
        .eq("user_id", str(user_id))
        .eq("type", lifeline_type)
        .order("used_at", desc=True)
        .limit(1)
        .execute(),
    )
    data = getattr(resp, "data", None) or []
    if data:
        iso = data[0]["used_at"]
        if isinstance(iso, str):
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            return datetime.datetime.fromisoformat(iso).astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return None

async def db_lifeline_mark_use(user_id: int, lifeline_type: str) -> None:
    await _db_call(
        "lifelines_usage.insert",
        lambda: supabase.table("lifelines_usage")
        .insert({
            "user_id": str(user_id),
            "type": lifeline_type,
            "used_at": datetime.datetime.utcnow().isoformat() + "Z",
        })
        .execute(),
    )

async def lifeline_check_cooldown(user_id: int, lifeline_type: str) -> Optional[str]:
    last = await db_lifeline_last_used(user_id, lifeline_type)
    if not last:
        return None
    rem = _cooldown_remaining(last, COOLDOWN_HOURS)
    if rem.total_seconds() > 0:
        return _fmt_td(rem)
    return None

async def safe_lifeline_cooldown(
    interaction: Interaction,
    lifeline_type: str,
) -> tuple[Optional[str], bool]:
    """Zwraca (cooldown, db_error) i pokazuje czytelny błąd użytkownikowi."""
    try:
        return await lifeline_check_cooldown(interaction.user.id, lifeline_type), False
    except SupabaseOperationError:
        await safe_ephemeral(
            interaction,
            "⚠️ Nie udało się sprawdzić koła ratunkowego. Spróbuj ponownie później.",
        )
        return None, True

async def safe_lifeline_mark_use(interaction: Interaction, lifeline_type: str) -> bool:
    try:
        await db_lifeline_mark_use(interaction.user.id, lifeline_type)
        return True
    except SupabaseOperationError:
        await safe_ephemeral(
            interaction,
            "⚠️ Nie udało się zapisać użycia koła. Spróbuj ponownie później.",
        )
        return False

# -------------- Pytania ----------------------
def load_questions() -> List[Dict[str, Any]]:
    if not os.path.exists(QUESTIONS_FILE):
        raise FileNotFoundError(f"Brak pliku z pytaniami: {QUESTIONS_FILE}")
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    normalized = []
    for q in data:
        try:
            qid = int(q["id"])
            question = str(q["question"])
            options = q["options"]
            correct = str(q["answer"]).strip().upper()
            difficulty = str(q["difficulty"]).strip().lower()
            if correct not in {"A", "B", "C", "D"}:
                raise ValueError(f"Nieprawidłowa odpowiedź: {correct}")
            if difficulty not in POINTS_BY_DIFFICULTY:
                raise ValueError(f"Nieprawidłowa kategoria difficulty: {difficulty}")
            normalized.append({
                "id": qid,
                "difficulty": difficulty,
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
        self.answers: Dict[int, str] = {}

active_quizzes: Dict[int, QuizState] = {}
finished_messages: Set[int] = set()
quiz_lock = asyncio.Lock()

class QuizAlreadyActiveError(RuntimeError):
    """Na kanale trwa już quiz i nie wolno uruchomić kolejnego."""

# ---------- PROSTSZE I PEWNIEJSZE EPHEMERAL ----------
async def safe_ephemeral(interaction: Interaction, content: str = "", view: Optional[discord.ui.View] = None):
    for attempt in (1, 2):
        try:
            if not interaction.response.is_done():
                if view is None:
                    return await interaction.response.send_message(content, ephemeral=True)
                return await interaction.response.send_message(content, ephemeral=True, view=view)
            # już odpowiedziano – followup
            if view is None:
                return await interaction.followup.send(content, ephemeral=True)
            return await interaction.followup.send(content, ephemeral=True, view=view)
        except HTTPException as e:
            if getattr(e, "status", None) == 429 and attempt == 1:
                await asyncio.sleep(1.5)
                continue
            log.warning("safe_ephemeral failed (status=%s): %r", getattr(e, "status", "?"), e)
            return None
        except Exception as e:
            log.warning("safe_ephemeral unexpected error: %r", e)
            return None

# ----------------------------------------------------

# --- REPORT FEATURE: modal ---
class ReportQuestionModal(ui.Modal, title="Zgłoś pytanie"):
    reason = ui.TextInput(
        label="Co jest nie tak?",
        placeholder="Opisz błąd / literówkę / dwuznaczność / źródło...",
        style=discord.TextStyle.paragraph,
        max_length=1000
    )

    def __init__(self, source_message_id: int):
        super().__init__(timeout=180)
        self.source_message_id = source_message_id

    async def on_submit(self, interaction: Interaction):
        try:
            state = active_quizzes.get(self.source_message_id)
            guild = interaction.guild
            channel = interaction.channel
            guild_id = guild.id if guild else 0
            channel_id = channel.id if isinstance(channel, (discord.TextChannel, discord.Thread)) else 0
            jump_url = (
                f"https://discord.com/channels/{guild_id}/{channel_id}/{self.source_message_id}"
                if (guild_id and channel_id) else "(brak linku)"
            )
            lines = [
                "🚩 **Zgłoszenie pytania**",
                f"Zgłosił: {interaction.user.mention} ({interaction.user.id})",
                f"Serwer: {getattr(guild, 'name', '?')} ({guild_id})",
                f"Kanał: {getattr(channel, 'name', '?')} ({channel_id})",
                f"Link do wiadomości: {jump_url}",
                "",
                f"Powód: {self.reason.value.strip() or '(pusty)'}",
                ""
            ]
            if state:
                q = state.question
                lines += [
                    "**Szczegóły pytania:**",
                    f"ID: {q.get('id')} | Kategoria: **{q.get('difficulty')}** | Poprawna: **{q.get('answer')}**",
                    f"Treść: {q.get('question')}",
                    f"A: {q['options'].get('A')}",
                    f"B: {q['options'].get('B')}",
                    f"C: {q['options'].get('C')}",
                    f"D: {q['options'].get('D')}",
                ]
            else:
                lines.append("_Uwaga: stan pytania nieaktywny (quiz mógł się zakończyć)._")

            sent = await _send_report_to_owner("\n".join(lines))
            if sent:
                await interaction.response.send_message("Dzięki! Twoja odpowiedź została zapisana. ✅", ephemeral=True)
            else:
                await interaction.response.send_message("Nie udało się wysłać zgłoszenia. ❌", ephemeral=True)
        except Exception as e:
            log.exception("Report modal submit error: %r", e)
            try:
                await interaction.response.send_message("Wystąpił błąd przy wysyłaniu zgłoszenia. ❌", ephemeral=True)
            except Exception:
                pass
# --- END REPORT FEATURE ---

# --- LIFELINES FEATURE: UserSelect do „Telefonu” ---
class PhoneFriendSelectView(ui.View):
    def __init__(self, source_message_id: int):
        super().__init__(timeout=90)
        self.source_message_id = source_message_id
        self.select = ui.UserSelect(placeholder="Wybierz gracza", min_values=1, max_values=1)
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: Interaction):
        ch = interaction.channel
        guild = interaction.guild
        if not isinstance(ch, (discord.TextChannel, discord.Thread)) or not guild:
            return await interaction.response.send_message("Użyj na kanale tekstowym serwera.", ephemeral=True)

        state = active_quizzes.get(self.source_message_id)
        if not state:
            return await interaction.response.send_message("Brak aktywnego pytania na tym kanale.", ephemeral=True)
        if datetime.datetime.utcnow() > state.end_time:
            return await interaction.response.send_message("Czas na to pytanie już minął.", ephemeral=True)

        # Bierzemy ID z payloadu interakcji
        raw_values = (interaction.data or {}).get("values") or []  # type: ignore[attr-defined]
        try:
            uid = int(raw_values[0])
        except Exception:
            return await interaction.response.send_message("Nie udało się odczytać wyboru.", ephemeral=True)

        friend = guild.get_member(uid)
        if friend is None:
            try:
                friend = await guild.fetch_member(uid)
            except Exception:
                pass

        cd, db_error = await safe_lifeline_cooldown(interaction, "telefon")
        if db_error:
            return
        if cd:
            return await interaction.response.send_message(f"„Telefon do przyjaciela” w cooldownie jeszcze {cd}.", ephemeral=True)

        letter = state.answers.get(uid)
        if not letter:
            return await interaction.response.send_message(
                f"📵 Abonent **{friend.display_name if friend else uid}** jeszcze nie odpowiedział(a). "
                f"(Koło **nie** zostało zużyte.)",
                ephemeral=True
            )

        if not await safe_lifeline_mark_use(interaction, "telefon"):
            return
        responses = [
            "Słuchaj, nie jestem pewien, ale wydaje mi się, że to będzie **{answer}**.",
            "Ciężko powiedzieć, ale coś mi mówi, że to **{answer}**.",
            "Hmm... strzelam, że to **{answer}**.",
            "Myślę, że to może być **{answer}**.",
        ]
        msg = random.choice(responses).format(answer=letter)
        await interaction.response.send_message(
            f"📞 Telefon do **{friend.display_name if friend else uid}** → {msg}",
            ephemeral=True
        )
        self.stop()
# --- END LIFELINES FEATURE ---

# -------------- Widok z przyciskami ----------
class QuizPersistentView(ui.View):
    def __init__(self, disabled: bool=False):
        super().__init__(timeout=None)
        self._disabled = disabled
        if disabled:
            for child in self.children:
                try:
                    child.disabled = True
                except Exception:
                    pass

    @ui.button(label="A", custom_id="quiz_A", style=ButtonStyle.secondary, row=0)
    async def _a(self, interaction: Interaction, button: ui.Button):
        await handle_answer_click(interaction, "A")

    @ui.button(label="B", custom_id="quiz_B", style=ButtonStyle.secondary, row=0)
    async def _b(self, interaction: Interaction, button: ui.Button):
        await handle_answer_click(interaction, "B")

    @ui.button(label="C", custom_id="quiz_C", style=ButtonStyle.secondary, row=0)
    async def _c(self, interaction: Interaction, button: ui.Button):
        await handle_answer_click(interaction, "C")

    @ui.button(label="D", custom_id="quiz_D", style=ButtonStyle.secondary, row=0)
    async def _d(self, interaction: Interaction, button: ui.Button):
        await handle_answer_click(interaction, "D")

    # ── Etykieta dla kół ratunkowych (ROW=1) ─────────────────────────────────
    @ui.button(
        label="Koła ratunkowe",
        custom_id="quiz_helpers_label",
        style=ButtonStyle.secondary,
        disabled=True,
        row=1
    )
    async def _lbl_helpers(self, interaction: Interaction, button: ui.Button):
        pass

    # ── KOŁA RATUNKOWE – PRZYCISKI (ROW=1) ──────────────────────────────────
    @ui.button(label="50/50", custom_id="quiz_5050", style=ButtonStyle.primary, row=1)
    async def _btn_5050(self, interaction: Interaction, button: ui.Button):
        ch = interaction.channel
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            if not interaction.response.is_done():
                return await interaction.response.send_message("Użyj na kanale tekstowym.", ephemeral=True)
            return await interaction.followup.send("Użyj na kanale tekstowym.", ephemeral=True)

        state = get_state_for_channel(ch.id)
        if not state:
            if not interaction.response.is_done():
                return await interaction.response.send_message("Brak aktywnego pytania na tym kanale.", ephemeral=True)
            return await interaction.followup.send("Brak aktywnego pytania na tym kanale.", ephemeral=True)

        if datetime.datetime.utcnow() > state.end_time:
            if not interaction.response.is_done():
                return await interaction.response.send_message("Czas na to pytanie już minął.", ephemeral=True)
            return await interaction.followup.send("Czas na to pytanie już minął.", ephemeral=True)

        cd, db_error = await safe_lifeline_cooldown(interaction, "5050")
        if db_error:
            return
        if cd:
            if not interaction.response.is_done():
                return await interaction.response.send_message(f"50/50 w cooldownie jeszcze {cd}.", ephemeral=True)
            return await interaction.followup.send(f"50/50 w cooldownie jeszcze {cd}.", ephemeral=True)

        correct = state.question["answer"]
        wrong = [x for x in ["A","B","C","D"] if x != correct]
        kept = [correct, random.choice(wrong)]
        random.shuffle(kept)
        if not await safe_lifeline_mark_use(interaction, "5050"):
            return

        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"🔔 50/50 → zostały: **{kept[0]}** lub **{kept[1]}**",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"🔔 50/50 → zostały: **{kept[0]}** lub **{kept[1]}**",
                ephemeral=True
            )

    @ui.button(label="Publika", custom_id="quiz_audience", style=ButtonStyle.primary, row=1)
    async def _btn_audience(self, interaction: Interaction, button: ui.Button):
        ch = interaction.channel
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            if not interaction.response.is_done():
                return await interaction.response.send_message("Użyj na kanale tekstowym.", ephemeral=True)
            return await interaction.followup.send("Użyj na kanale tekstowym.", ephemeral=True)

        state = get_state_for_channel(ch.id)
        if not state:
            if not interaction.response.is_done():
                return await interaction.response.send_message("Brak aktywnego pytania na tym kanale.", ephemeral=True)
            return await interaction.followup.send("Brak aktywnego pytania na tym kanale.", ephemeral=True)

        if datetime.datetime.utcnow() > state.end_time:
            if not interaction.response.is_done():
                return await interaction.response.send_message("Czas na to pytanie już minął.", ephemeral=True)
            return await interaction.followup.send("Czas na to pytanie już minął.", ephemeral=True)

        cd, db_error = await safe_lifeline_cooldown(interaction, "publika")
        if db_error:
            return
        if cd:
            if not interaction.response.is_done():
                return await interaction.response.send_message(f"„Pytanie do publiczności” w cooldownie jeszcze {cd}.", ephemeral=True)
            return await interaction.followup.send(f"„Pytanie do publiczności” w cooldownie jeszcze {cd}.", ephemeral=True)

        counts = {k: 0 for k in ["A", "B", "C", "D"]}
        for letter in state.answers.values():
            if letter in counts:
                counts[letter] += 1
        total = sum(counts.values()) or 1
        perc = {k: round(v * 100 / total) for k, v in counts.items()}
        if not await safe_lifeline_mark_use(interaction, "publika"):
            return
        msg = (
            "📊 Głosy do tej pory:\n"
            f"A: {counts['A']} ({perc['A']}%)\n"
            f"B: {counts['B']} ({perc['B']}%)\n"
            f"C: {counts['C']} ({perc['C']}%)\n"
            f"D: {counts['D']} ({perc['D']}%)"
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

    @ui.button(label="Telefon", custom_id="quiz_phone", style=ButtonStyle.primary, row=1)
    async def _btn_phone(self, interaction: Interaction, button: ui.Button):
        msg = interaction.message
        if not msg:
            return await safe_ephemeral(interaction, "Brak powiązanej wiadomości.")
        await safe_ephemeral(
            interaction,
            "Wybierz gracza, do którego dzwonisz:",
            view=PhoneFriendSelectView(source_message_id=msg.id),
        )

    # --- REPORT FEATURE: przycisk otwierający modal (ROW=2) ---
    @ui.button(label="🚩 Zgłoś pytanie", custom_id="quiz_report", style=ButtonStyle.danger, row=2)
    async def _report(self, interaction: Interaction, button: ui.Button):
        msg = interaction.message
        if not msg:
            return await interaction.response.send_message("Brak powiązanej wiadomości.", ephemeral=True)
        try:
            await interaction.response.send_modal(ReportQuestionModal(source_message_id=msg.id))
        except discord.errors.InteractionResponded:
            pass
    # --- END REPORT FEATURE ---

async def handle_answer_click(interaction: Interaction, letter: str):
    mid = interaction.message.id if interaction.message else None
    if not mid:
        return await safe_ephemeral(interaction, "Brak powiązanego pytania.")
    state = active_quizzes.get(mid)
    now = datetime.datetime.utcnow()
    if not state:
        return await safe_ephemeral(interaction, "Ten quiz już nie przyjmuje odpowiedzi.")
    if now > state.end_time:
        return await safe_ephemeral(interaction, "Czas minął. Odpowiedzi po czasie nie są liczone.")

    uid = interaction.user.id
    if uid in state.answers:
        return await safe_ephemeral(interaction, "Masz już zapisaną odpowiedź.")
    state.answers[uid] = letter

    # --- LOG do Render Logs ---
    log.info(
        "Answer saved: guild=%s channel=%s user=%s letter=%s msg=%s",
        getattr(interaction.guild, "id", "?"),
        getattr(interaction.channel, "id", "?"),
        interaction.user.id, letter, mid,
    )
    await safe_ephemeral(interaction, "Zapisano odpowiedź ✅")

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
    if state.message_id in finished_messages:
        return
    finished_messages.add(state.message_id)

    correct = state.question["answer"]
    difficulty = state.question["difficulty"]
    points_for_question = POINTS_BY_DIFFICULTY[difficulty]
    winners: List[int] = [uid for uid, letter in state.answers.items() if letter == correct]

    ranking_saved = True
    try:
        ranking = await db_load_ranking()
        today = datetime.datetime.now(POLAND_TZ).date().isoformat()

        for uid in winners:
            uid_s = str(uid)
            member: Optional[discord.Member] = channel.guild.get_member(uid)
            name = member.display_name if member else f"Użytkownik {uid_s}"

            user_data = ranking.get(uid_s) or {"name": name, "points": 0, "weekly": {}, "monthly": {}}
            user_data["name"] = name
            user_data["points"] = int(user_data.get("points", 0)) + points_for_question

            weekly = dict(user_data.get("weekly") or {})
            monthly = dict(user_data.get("monthly") or {})
            weekly[today] = int(weekly.get(today, 0)) + points_for_question
            monthly[today] = int(monthly.get(today, 0)) + points_for_question
            user_data["weekly"] = weekly
            user_data["monthly"] = monthly
            ranking[uid_s] = user_data

        await db_save_ranking(ranking)
    except SupabaseOperationError:
        ranking_saved = False
        log.error("Nie zapisano punktów dla quizu message_id=%s", state.message_id)

    if winners and ranking_saved:
        mentions = ", ".join(f"<@{uid}>" for uid in winners)
        msg = (
            f"**Koniec czasu!**\n"
            f"Prawidłowa odpowiedź: **{correct}**\n"
            f"Gratulacje dla: {mentions} (+{points_for_question} pkt)"
        )
    elif winners:
        mentions = ", ".join(f"<@{uid}>" for uid in winners)
        msg = (
            f"**Koniec czasu!**\n"
            f"Prawidłowa odpowiedź: **{correct}**\n"
            f"Poprawnie odpowiedzieli: {mentions}\n"
            "⚠️ Nie udało się zapisać punktów — administracja powinna sprawdzić logi."
        )
    else:
        msg = (
            f"**Koniec czasu!**\n"
            f"Prawidłowa odpowiedź: **{correct}**\n"
            f"Nikt nie trafił tym razem."
        )

    try:
        message = await channel.fetch_message(state.message_id)
        try:
            await message.edit(view=QuizPersistentView(disabled=True))
        except Exception:
            pass
        await channel.send(msg)
    except discord.NotFound:
        await channel.send(msg)

    async def _send_ranking_later():
        try:
            await asyncio.sleep(20)  # opóźnienie po zakończeniu quizu
            data = await db_load_ranking()
            pairs = sorted(
                ((v.get("name") or str(uid), int(v.get("points", 0))) for uid, v in data.items()),
                key=lambda x: x[1],
                reverse=True
            )
            embed = _top_embed("Ranking – All time (po tym pytaniu)", pairs)
            await channel.send(embed=embed)
        except Exception as e:
            log.error("Auto-ranking error: %r", e)

    asyncio.create_task(_send_ranking_later())

# -------------- Uruchamianie quizu ------------
def get_quiz_role(guild: discord.Guild) -> Optional[discord.Role]:
    role = None
    if QUIZ_ROLE_ID:
        try:
            role = guild.get_role(int(QUIZ_ROLE_ID))
        except Exception:
            role = None
    if not role:
        role = discord.utils.get(guild.roles, name=QUIZ_ROLE_NAME)
    return role

async def run_quiz(channel: discord.TextChannel):
    async with quiz_lock:
        now_utc = datetime.datetime.utcnow()
        if any(now_utc <= state.end_time for state in active_quizzes.values()):
            raise QuizAlreadyActiveError("Na serwerze trwa już aktywny quiz.")

        questions = load_questions()
        used = await db_get_used_ids()
        available = [q for q in questions if int(q["id"]) not in used]
        if not available:
            log.info("Wszystkie pytania zostały wykorzystane – czyszczę used_questions.")
            await db_clear_used_questions()
            available = questions[:]

        question = random.choice(available)
        qid = int(question["id"])
        content = build_question_message(question)
        view = QuizPersistentView()

        role = get_quiz_role(channel.guild)
        if role:
            msg = await channel.send(
                f"{role.mention} " + content,
                view=view,
                allowed_mentions=discord.AllowedMentions(roles=[role])
            )
        else:
            msg = await channel.send(content, view=view)

        # Pytanie rezerwujemy natychmiast po publikacji. Jeżeli zapis się nie
        # powiedzie, usuwamy wiadomość i nie uruchamiamy quizu w połowie.
        try:
            await db_add_used_id(qid)
        except SupabaseOperationError:
            try:
                await msg.delete()
            except Exception:
                try:
                    await msg.edit(view=QuizPersistentView(disabled=True))
                except Exception:
                    pass
            raise

        last_quiz_id_per_channel[channel.id] = msg.id
        end_time = datetime.datetime.utcnow() + datetime.timedelta(seconds=QUIZ_DURATION_SECONDS)
        state = QuizState(question=question, message_id=msg.id, end_time=end_time)
        active_quizzes[msg.id] = state

        log.info("Quiz wystartował (id=%s). Koniec: %s", qid, end_time.strftime("%H:%M:%S UTC"))

        async def _finish():
            try:
                await asyncio.sleep(QUIZ_DURATION_SECONDS)
                await conclude_quiz(channel, state)
            except Exception as e:
                log.exception("Nie udało się zakończyć quizu message_id=%s: %r", msg.id, e)
                try:
                    await channel.send("⚠️ Nie udało się prawidłowo zakończyć quizu. Administracja została poinformowana w logach.")
                except Exception:
                    pass
            finally:
                active_quizzes.pop(msg.id, None)
                if last_quiz_id_per_channel.get(channel.id) == msg.id:
                    last_quiz_id_per_channel.pop(channel.id, None)
                finished_messages.discard(msg.id)

        asyncio.create_task(_finish())

# -------------- Komendy (prefix) --------------
def _top_embed(title: str, pairs: List[tuple[str, int]]) -> discord.Embed:
    embed = discord.Embed(title=title, colour=0x2b7cff)
    if not pairs:
        embed.description = "Brak wyników."
        return embed
    for i, (name, pts) in enumerate(pairs[:10], start=1):
        embed.add_field(name=f"{i}. {name}", value=f"{pts} pkt", inline=False)
    return embed

@bot.command()
async def ranking(ctx: commands.Context):
    data = await db_load_ranking()
    pairs = sorted(
        ((v.get("name") or str(uid), int(v.get("points",0))) for uid, v in data.items()),
        key=lambda x: x[1], reverse=True
    )
    await ctx.send(embed=_top_embed("Ranking – All time", pairs))

def _sum_period(d: Dict[str, int], days: int) -> int:
    cutoff = datetime.datetime.now(POLAND_TZ).date() - datetime.timedelta(days=days)
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
    d = data.get(str(member.id))
    pts = int(d.get("points",0)) if d else 0
    await ctx.reply(f"{(d.get('name') if d else member.display_name)} ma **{pts}** pkt.")

# RĘCZNY SYNC (tylko owner)
@bot.command(name="sync")
@commands.is_owner()
async def sync_slash(ctx: commands.Context):
    try:
        await bot.tree.sync()
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
        names = [cmd.name for cmd in bot.tree.get_commands()]
        await ctx.reply("✅ Zsynchronizowano slash-komendy.\nDostępne: " + ", ".join(names))
        log.info("Manual sync done. Commands: %s", names)
    except Exception as e:
        await ctx.reply(f"⚠️ Sync error: {e}")
        log.exception("Manual sync error: %r", e)

# -------------- Slash commands (ephemeral) --------------
@bot.tree.command(name="ping", description="Sprawdź, czy slash-komendy działają (ephemeral).")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Działam!", ephemeral=True)

@bot.tree.command(name="polnapol", description="Pół na pół – widoczne tylko dla Ciebie (ephemeral).")
async def slash_polnapol(interaction: discord.Interaction):
    ch = interaction.channel
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return await interaction.response.send_message("Użyj na kanale tekstowym.", ephemeral=True)

    state = get_state_for_channel(ch.id)
    if not state:
        return await interaction.response.send_message("Brak aktywnego pytania na tym kanale.", ephemeral=True)
    if datetime.datetime.utcnow() > state.end_time:
        return await interaction.response.send_message("Czas na to pytanie już minął.", ephemeral=True)

    cd, db_error = await safe_lifeline_cooldown(interaction, "5050")
    if db_error:
        return
    if cd:
        return await interaction.response.send_message(f"50/50 w cooldownie jeszcze {cd}.", ephemeral=True)

    correct = state.question["answer"]
    wrong = [x for x in ["A","B","C","D"] if x != correct]
    kept = [correct, random.choice(wrong)]
    random.shuffle(kept)
    if not await safe_lifeline_mark_use(interaction, "5050"):
        return
    await interaction.response.send_message(
        f"🔔 50/50 → zostały: **{kept[0]}** lub **{kept[1]}**",
        ephemeral=True
    )

@bot.tree.command(name="publika", description="Pytanie do publiczności – procentowy rozkład głosów (ephemeral).")
async def slash_publika(interaction: discord.Interaction):
    ch = interaction.channel
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return await interaction.response.send_message("Użyj na kanale tekstowym.", ephemeral=True)

    state = get_state_for_channel(ch.id)
    if not state:
        return await interaction.response.send_message("Brak aktywnego pytania na tym kanale.", ephemeral=True)
    if datetime.datetime.utcnow() > state.end_time:
        return await interaction.response.send_message("Czas na to pytanie już minął.", ephemeral=True)

    cd, db_error = await safe_lifeline_cooldown(interaction, "publika")
    if db_error:
        return
    if cd:
        return await interaction.response.send_message(f"„Pytanie do publiczności” w cooldownie jeszcze {cd}.", ephemeral=True)

    counts = {k: 0 for k in ["A", "B", "C", "D"]}
    for letter in state.answers.values():
        if letter in counts:
            counts[letter] += 1
    total = sum(counts.values()) or 1
    perc = {k: round(v * 100 / total) for k, v in counts.items()}
    if not await safe_lifeline_mark_use(interaction, "publika"):
        return
    msg = (
        "📊 Głosy do tej pory:\n"
        f"A: {counts['A']} ({perc['A']}%)\n"
        f"B: {counts['B']} ({perc['B']}%)\n"
        f"C: {counts['C']} ({perc['C']}%)\n"
        f"D: {counts['D']} ({perc['D']}%)"
    )
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="telefon", description="Telefon do przyjaciela – pokaż odpowiedź wskazanego gracza (ephemeral).")
@app_commands.describe(friend="Wskaż gracza, którego odpowiedź chcesz podejrzeć")
async def slash_telefon(interaction: discord.Interaction, friend: discord.Member):
    ch = interaction.channel
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return await interaction.response.send_message("Użyj na kanale tekstowym.", ephemeral=True)

    state = get_state_for_channel(ch.id)
    if not state:
        return await interaction.response.send_message("Brak aktywnego pytania na tym kanale.", ephemeral=True)
    if datetime.datetime.utcnow() > state.end_time:
        return await interaction.response.send_message("Czas na to pytanie już minął.", ephemeral=True)

    cd, db_error = await safe_lifeline_cooldown(interaction, "telefon")
    if db_error:
        return
    if cd:
        return await interaction.response.send_message(f"„Telefon do przyjaciela” w cooldownie jeszcze {cd}.", ephemeral=True)

    letter = state.answers.get(friend.id)
    if not letter:
        return await interaction.response.send_message(
            f"📵 Abonent **{friend.display_name}** tymczasowo niedostępny – jeszcze nie odpowiedział(a). "
            f"Spróbuj zadzwonić później lub do kogoś innego. (Koło **nie** zostało zużyte.)",
            ephemeral=True
        )

    if not await safe_lifeline_mark_use(interaction, "telefon"):
        return
    responses = [
        "Słuchaj, nie jestem pewien, ale wydaje mi się, że to będzie odpowiedź **{answer}**.",
        "Ciężko powiedzieć, ale coś mi mówi, że to **{answer}**.",
        "Hmm... strzelam, że to **{answer}**.",
        "Myślę, że to może być **{answer}**.",
        "Nie jestem ekspertem, ale obstawiam **{answer}**.",
        "Nie wiem na 100%, ale wydaje mi się, że chodzi o **{answer}**.",
        "Kurczę... mam przeczucie, że to **{answer}**.",
    ]
    msg = random.choice(responses).format(answer=letter)
    await interaction.response.send_message(
        f"📞 Telefon do **{friend.display_name}** → {msg}",
        ephemeral=True
    )

@bot.tree.command(name="mojekola", description="Pokaż stan swoich kół ratunkowych (cooldowny).")
async def slash_mojekola(interaction: discord.Interaction):
    types = [("5050", "🌓 50/50"), ("publika", "📊 Publika"), ("telefon", "📞 Telefon")]
    lines = []
    for t_key, t_label in types:
        last = await db_lifeline_last_used(interaction.user.id, t_key)
        if not last:
            lines.append(f"{t_label}: **dostępne** ✅")
            continue
        rem = _cooldown_remaining(last, COOLDOWN_HOURS)
        if rem.total_seconds() > 0:
            lines.append(f"{t_label}: cooldown **{_fmt_td(rem)}**")
        else:
            lines.append(f"{t_label}: **dostępne** ✅")
    msg = "🔎 **Twoje koła ratunkowe**\n" + "\n".join(lines)
    await interaction.response.send_message(msg, ephemeral=True)

# -------------- Scheduler ---------------------
def _daily_random_quiz_time(local_date: datetime.date) -> datetime.time:
    """
    Zwraca jedną stabilną, pseudolosową minutę dla danego dnia w przedziale
    10:00–22:00 czasu polskiego. Ten sam dzień zawsze daje tę samą godzinę,
    także po restarcie procesu.
    """
    source = f"{DAILY_QUIZ_RANDOM_SEED}:{local_date.isoformat()}".encode("utf-8")
    random_value = int.from_bytes(hashlib.sha256(source).digest()[:8], "big")
    start_minute = DAILY_QUIZ_START_HOUR * 60
    end_minute = DAILY_QUIZ_END_HOUR * 60
    minute_of_day = start_minute + random_value % (end_minute - start_minute + 1)
    hour, minute = divmod(minute_of_day, 60)
    return datetime.time(hour, minute)

_fired_today: Set[str] = set()
_alerted_today: Set[str] = set()
_last_reset_date: Optional[datetime.date] = None

@tasks.loop(minutes=1)
async def daily_quiz_task():
    """
    Jeden automatyczny quiz dziennie o stabilnej losowej minucie pomiędzy
    10:00 a 22:00 czasu polskiego. Po restarcie nadrabia quiz, jeśli jego
    zaplanowana godzina już minęła i wiadomość nie została wcześniej wysłana.
    """
    global _last_reset_date
    try:
        now = datetime.datetime.now(POLAND_TZ)
        local_date = now.date()
        quiz_time = _daily_random_quiz_time(local_date)
        target = datetime.datetime.combine(local_date, quiz_time, tzinfo=POLAND_TZ)
        daily_key = local_date.isoformat()

        # Reset raz dziennie według daty obowiązującej w Polsce.
        if _last_reset_date != local_date:
            _fired_today.clear()
            _alerted_today.clear()
            _last_reset_date = local_date
            log.info(
                "[diag] Nowy dzień w Polsce: %s | dzisiejszy quiz: %s",
                daily_key,
                target.strftime("%H:%M %Z"),
            )

        ch = await get_quiz_channel()
        mins_to_quiz = int((target - now).total_seconds() // 60)
        log.info(
            "[diag] now=%s | channel=%s | daily_target=%s | fired=%s | za=%sm | alerts=%s",
            now.strftime("%Y-%m-%d %H:%M %Z"),
            f"#{ch.name}" if isinstance(ch, discord.TextChannel) else "NONE",
            target.strftime("%H:%M %Z"),
            daily_key in _fired_today,
            mins_to_quiz,
            QUIZ_ALERTS_ENABLED,
        )

        alert_target = target - datetime.timedelta(minutes=ALERT_MINUTES_BEFORE)
        if (
            QUIZ_ALERTS_ENABLED
            and daily_key not in _alerted_today
            and alert_target <= now < alert_target + datetime.timedelta(minutes=1)
            and ch
        ):
            role = get_quiz_role(ch.guild)
            if role and PING_ROLE_IN_ALERTS:
                await ch.send(
                    f"{role.mention} 🧠 Za {ALERT_MINUTES_BEFORE} minut pojawi się pytanie quizowe!",
                    allowed_mentions=discord.AllowedMentions(roles=[role]),
                )
            else:
                await ch.send(f"🧠 Za {ALERT_MINUTES_BEFORE} minut pojawi się pytanie quizowe!")
            _alerted_today.add(daily_key)

        # Po osiągnięciu zaplanowanej godziny najpierw atomowo rezerwujemy quiz
        # w Supabase. Unikalny indeks (guild_id, date_local) chroni również po
        # restarcie oraz przy chwilowym uruchomieniu dwóch instancji bota.
        if now >= target and daily_key not in _fired_today:
            if ch:
                claimed = await db_claim_daily_quiz(
                    ch.guild.id,
                    ch.id,
                    local_date,
                    target,
                )

                if claimed is False:
                    log.info("[diag] Dzisiejszy quiz jest już zapisany w fired_quizzes – pomijam duplikat.")
                    _fired_today.add(daily_key)
                elif claimed is True:
                    log.info("[diag] Odpalamy dzienny quiz zaplanowany na %s", target.strftime("%H:%M %Z"))
                    try:
                        await run_quiz(ch)
                    except QuizAlreadyActiveError:
                        await db_release_daily_quiz(ch.guild.id, local_date)
                        log.info("[diag] Inny quiz nadal trwa – automat spróbuje ponownie za minutę.")
                    except Exception:
                        # run_quiz zwraca sukces dopiero po wysłaniu wiadomości,
                        # zapisaniu question_id i utworzeniu aktywnego stanu.
                        await db_release_daily_quiz(ch.guild.id, local_date)
                        raise
                    else:
                        _fired_today.add(daily_key)
                else:
                    # Bez potwierdzenia z bazy nie publikujemy w ciemno. Kolejna
                    # iteracja ponowi próbę, więc awaria nie tworzy duplikatów.
                    log.warning("[diag] Supabase niedostępne – odkładam automatyczny quiz o minutę.")
            else:
                log.warning("[diag] Nie znaleziono kanału dla dziennego quizu.")
    except Exception as e:
        log.exception("daily_quiz_task error: %r", e)

# -------------- Health server + watchdog ------
class PingHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path in ("/healthz", "/"):
            self.send_response(200)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        if self.path in ("/healthz", "/"):
            self.send_response(200)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path in ("/healthz", "/"):
            self.send_response(200)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers()
            try:
                self.wfile.write(b"ok")
            except Exception:
                pass
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

# -------------- Self-uptime ping (Render keep-alive) ------------
@tasks.loop(minutes=5)
async def uptime_ping():
    url = "https://naruto-quiz-bot.onrender.com/healthz"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    log.info("Uptime ping OK (%s)", url)
                else:
                    log.warning("Uptime ping FAIL %s (%s)", url, resp.status)
    except Exception as e:
        log.warning("Uptime ping exception: %r", e)

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

    ch = discord.utils.get(_guild_cache.text_channels, name=QUIZ_CHANNEL_NAME)
    if ch:
        _channel_cache = ch
        return ch

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

# -------------- Uprawnienia do !quiz --------------
# Admin (użytkownik) bez limitu, Wspierający – wspólny cooldown 72h na serwer
ADMIN_USER_IDS = {1356372381043523584}
SUPPORTER_ROLE_ID = 1377326388415299777

SUPPORTER_COOLDOWN_HOURS = 72
supporter_manual_quiz_lock = asyncio.Lock()

def _is_real_admin(member: discord.Member) -> bool:
    # admin serwera lub ktoś z silnymi uprawnieniami – rozszerz, jeśli chcesz
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild or perms.manage_roles))

async def db_supporter_last_used(guild_id: int) -> Optional[datetime.datetime]:
    """
    Zwraca ostatnie użycie !quiz przez dowolnego Wspierającego na serwerze
    (UTC, naive). user_id pozostaje w tabeli wyłącznie jako informacja,
    kto uruchomił quiz.
    """
    resp = await _db_call(
        "supporter_manual_quiz_usage.select",
        lambda: supabase.table("supporter_manual_quiz_usage")
        .select("used_at")
        .eq("guild_id", str(guild_id))
        .order("used_at", desc=True)
        .limit(1)
        .execute(),
    )
    data = getattr(resp, "data", None) or []
    if not data:
        return None
    iso = data[0]["used_at"]
    if isinstance(iso, str):
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(iso).astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return None

async def db_supporter_mark_used(guild_id: int, user_id: int) -> int:
    resp = await _db_call(
        "supporter_manual_quiz_usage.insert",
        lambda: supabase.table("supporter_manual_quiz_usage")
        .insert({
            "guild_id": str(guild_id),
            "user_id": str(user_id),
            "used_at": datetime.datetime.utcnow().isoformat() + "Z",
        })
        .execute(),
    )
    data = getattr(resp, "data", None) or []
    if not data or data[0].get("id") is None:
        raise SupabaseOperationError("supporter_manual_quiz_usage.insert_missing_id")
    return int(data[0]["id"])

async def db_supporter_release_used(usage_id: int) -> None:
    await _db_call(
        "supporter_manual_quiz_usage.delete",
        lambda: supabase.table("supporter_manual_quiz_usage")
        .delete()
        .eq("id", usage_id)
        .execute(),
    )

async def _can_use_manual_quiz(ctx: commands.Context) -> tuple[bool, str, bool]:
    """
    Zwraca (can_use, msg_if_denied, is_supporter_flow).

    - Admin (ID w ADMIN_USER_IDS **lub** realny admin Discorda) → pełny bypass, bez cooldownu,
      is_supporter_flow = False (nie zapisujemy użycia).
    - Wspierający → wspólny cooldown 72h na cały serwer; jeśli OK →
      is_supporter_flow = True (zapis użycia przed startem).
    """
    author = ctx.author
    guild = ctx.guild

    # 1) Administrator z jawnej listy ID
    if author.id in ADMIN_USER_IDS:
        return True, "", False

    # 2) Realny admin Discorda po uprawnieniach
    if isinstance(author, discord.Member) and _is_real_admin(author):
        return True, "", False

    # 3) Wspierający na serwerze → podlega wspólnemu cooldownowi 72h
    if not guild or not isinstance(author, discord.Member):
        return False, "Ta komenda działa tylko na serwerze.", False

    role = guild.get_role(SUPPORTER_ROLE_ID)
    if not role or role not in author.roles:
        return False, "Ta komenda jest dostępna tylko dla osób z rangą **Wspierający** lub adminów.", False

    # Sprawdź ostatnie użycie
    try:
        last = await db_supporter_last_used(guild.id)
    except Exception:
        return (
            False,
            "⚠️ Nie udało się sprawdzić wspólnego cooldownu. Spróbuj ponownie później.",
            True,
        )
    if last:
        rem = _cooldown_remaining(last, SUPPORTER_COOLDOWN_HOURS)
        if rem.total_seconds() > 0:
            # czytelny tekst – np. "2d 3h 15m"
            return False, f"⏳ Cooldown **{_fmt_td(rem)}**. Spróbuj ponownie później.", True

    # Wspierający, wspólny cooldown minął → OK
    return True, "", True

# -------------- Events ------------------------
_persistent_view_registered = False
_orphan_cleanup_done = False

async def cancel_orphaned_quizzes_after_restart() -> None:
    """
    Po restarcie nie da się odzyskać odpowiedzi trzymanych wcześniej w RAM.
    Wyłączamy więc przyciski niedawnych, niedokończonych pytań i jasno
    informujemy kanał, że taki quiz został anulowany bez naliczania punktów.
    """
    guild = bot.get_guild(GUILD_ID)
    if not guild or not bot.user:
        return

    after = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=QUIZ_DURATION_SECONDS + 600
    )
    for channel in guild.text_channels:
        member = guild.me
        if member:
            permissions = channel.permissions_for(member)
            if not (permissions.read_messages and permissions.read_message_history):
                continue
        try:
            async for message in channel.history(limit=25, after=after, oldest_first=False):
                if message.author.id != bot.user.id or "**Pytanie:**" not in message.content:
                    continue
                if message.id in active_quizzes:
                    continue
                has_enabled_button = any(
                    not getattr(component, "disabled", True)
                    for row in message.components
                    for component in getattr(row, "children", [])
                )
                if not has_enabled_button:
                    continue
                await message.edit(view=QuizPersistentView(disabled=True))
                await channel.send(
                    f"⚠️ Quiz z wiadomości {message.jump_url} został anulowany po restarcie bota. "
                    "Odpowiedzi i punkty z tego pytania nie są naliczane."
                )
                log.warning("Anulowano osierocony quiz po restarcie: message_id=%s", message.id)
        except Exception as e:
            log.warning("Nie udało się sprawdzić kanału #%s po restarcie: %r", channel.name, e)


@bot.event
async def on_ready():
    global _persistent_view_registered, _orphan_cleanup_done
    log.info("Zalogowano jako %s (%s)", bot.user, bot.user.id if bot.user else "?")
    # Info w logach, czy reminder przed quizem jest włączony.
    log.info(
        "Reminder %s min przed quizem jest %s (ENV QUIZ_ALERTS_ENABLED=%s).",
        ALERT_MINUTES_BEFORE,
        "WŁĄCZONA" if QUIZ_ALERTS_ENABLED else "WYŁĄCZONA",
        QUIZ_ALERTS_ENABLED,
    )

    if not _persistent_view_registered:
        bot.add_view(QuizPersistentView())
        _persistent_view_registered = True

    if not _orphan_cleanup_done:
        try:
            await cancel_orphaned_quizzes_after_restart()
        except Exception as e:
            log.exception("Czyszczenie quizów po restarcie nie powiodło się: %r", e)
        finally:
            _orphan_cleanup_done = True

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

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    try:
        if interaction.response.is_done():
            await interaction.followup.send("⚠️ Wystąpił błąd przy tej komendzie.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Wystąpił błąd przy tej komendzie.", ephemeral=True)
    except Exception:
        pass
    log.exception("Slash command error: %r", error)

@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    original = getattr(error, "original", error)
    if isinstance(original, SupabaseOperationError):
        message = "⚠️ Baza danych jest chwilowo niedostępna. Spróbuj ponownie później."
    elif isinstance(original, commands.NotOwner):
        message = "Nie masz uprawnień do tej komendy."
    else:
        message = "⚠️ Wystąpił błąd podczas wykonywania komendy."
    try:
        await ctx.reply(message)
    except Exception:
        pass
    log.exception("Prefix command error: %r", error)

# -------------------- KOMENDA !quiz --------------------
@bot.command()
async def quiz(ctx: commands.Context):
    if not isinstance(ctx.channel, discord.TextChannel):
        return await ctx.reply("Tylko na kanałach tekstowych.")

    can, msg, supporter_flow = await _can_use_manual_quiz(ctx)
    if not can:
        return await ctx.reply(msg)

    # Dla Wspierających ponawiamy sprawdzenie wewnątrz blokady. Dzięki temu
    # dwie osoby używające komendy niemal jednocześnie nie uruchomią 2 quizów.
    if supporter_flow:
        async with supporter_manual_quiz_lock:
            can, msg, supporter_flow = await _can_use_manual_quiz(ctx)
            if not can:
                return await ctx.reply(msg)

            try:
                usage_id = await db_supporter_mark_used(ctx.guild.id, ctx.author.id)  # type: ignore
            except SupabaseOperationError:
                return await ctx.reply(
                    "⚠️ Nie udało się zapisać wspólnego cooldownu. Spróbuj ponownie później."
                )

            try:
                await run_quiz(ctx.channel)
                return
            except QuizAlreadyActiveError:
                error_message = "⏳ Na serwerze trwa już quiz. Poczekaj na jego zakończenie."
            except Exception as e:
                log.exception("Manualny quiz Wspierającego nie wystartował: %r", e)
                error_message = "⚠️ Nie udało się uruchomić quizu. Cooldown nie został naliczony."

            try:
                await db_supporter_release_used(usage_id)
            except SupabaseOperationError:
                log.error("Nie udało się cofnąć cooldownu Wspierającego usage_id=%s", usage_id)
                error_message += " Skontaktuj się z administracją — wpis cooldownu może wymagać usunięcia."
            return await ctx.reply(error_message)

    try:
        await run_quiz(ctx.channel)
    except QuizAlreadyActiveError:
        await ctx.reply("⏳ Na serwerze trwa już quiz. Poczekaj na jego zakończenie.")
    except SupabaseOperationError:
        await ctx.reply("⚠️ Baza danych jest chwilowo niedostępna. Quiz nie został uruchomiony.")
    except Exception as e:
        log.exception("Manualny quiz administratora nie wystartował: %r", e)
        await ctx.reply("⚠️ Nie udało się uruchomić quizu. Sprawdź logi bota.")
# -------------------------------------------------------

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
