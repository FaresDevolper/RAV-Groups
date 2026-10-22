"""
main.py - بوت توب القروبات (نسخة الأوامر العربية بدون سلاش)

كل الأوامر تنكتب كرسالة عادية بالعربي بدون / وبدون أي علامة.
اكتب "الاوامر" في أي روم وتشوف القائمة الكاملة.

الملفات (3 فقط): main.py, top_image.py, requirements.txt

المتغيرات (على Render تحطها في Environment، وعلى جهازك في ملف .env):
  TOKEN=توكن_البوت
  GUILD_ID=آيدي_السيرفر      (اختياري)
  TIMEZONE=Asia/Riyadh       (اختياري)
"""

import asyncio
import io
import logging
import os
import random
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiosqlite
import discord
from aiohttp import web
from discord.ext import commands, tasks
from dotenv import load_dotenv
from PIL import Image

from top_image import debug_slots, render_top

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("grouptop")

# ============================== الإعدادات ==============================
TOKEN = os.getenv("TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Riyadh"))
DB_PATH = os.getenv("DB_PATH", "data.db")

CHAT_POINTS = 2                 # نقاط كل رسالة محسوبة
CHAT_COOLDOWN = 30              # ثواني بين رسالتين محسوبتين لنفس العضو
MIN_CHAT_LENGTH = 3             # أقل طول للرسالة عشان تنحسب
VOICE_POINTS_PER_MINUTE = 1     # نقاط كل دقيقة فويس
MIN_VOICE_USERS = 2             # لازم أكثر من شخص (غير البوتات) بالروم عشان يحسب
BOARD_REFRESH_MINUTES = 10      # كل كم دقيقة تتحدث اللوحة الثابتة
DEFAULT_PERIOD = "daily"        # الفترة الافتراضية
COMMAND_COOLDOWN = 3            # ثواني بين أمرين لنفس العضو (للأوامر العامة)

PERIODS = {"daily": "يومي", "weekly": "أسبوعي", "monthly": "شهري", "all": "كلي"}
PERIOD_TITLES = {
    "daily": "توب اليوم",
    "weekly": "توب آخر 7 أيام",
    "monthly": "توب آخر 30 يوم",
    "all": "التوب الكلي",
}
MEDALS = ["🥇", "🥈", "🥉"]
EMBED_COLOR = 0x7B61FF


def normalize(text: str) -> str:
    text = text.strip().lower()
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text)


PERIOD_ALIASES = {
    normalize(k): v
    for k, v in {
        "يومي": "daily", "اليوم": "daily", "يوم": "daily",
        "اسبوعي": "weekly", "الاسبوع": "weekly", "اسبوع": "weekly",
        "شهري": "monthly", "الشهر": "monthly", "شهر": "monthly",
        "كلي": "all", "الكل": "all", "الكلي": "all",
    }.items()
}

ROLE_RE = re.compile(r"<@&(\d+)>")
USER_RE = re.compile(r"<@!?(\d+)>")
CHAN_RE = re.compile(r"<#(\d+)>")
ANY_MENTION_RE = re.compile(r"<(@&|@!?|#)\d+>")


def fmt_time(secs: int) -> str:
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}س {m}د" if h else f"{m}د"


def period_clause(period: str, today: date, prefix: str = ""):
    if period == "daily":
        return f"AND {prefix}day = ?", [today.isoformat()]
    if period == "weekly":
        return f"AND {prefix}day >= ?", [(today - timedelta(days=6)).isoformat()]
    if period == "monthly":
        return f"AND {prefix}day >= ?", [(today - timedelta(days=29)).isoformat()]
    return "", []


def _process_logo(raw: bytes) -> bytes:
    """يقص اللوقو مربع ويصغره 256x256 عشان يتخزن خفيف."""
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((256, 256), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


async def prepare_logo(attachment: discord.Attachment) -> Optional[bytes]:
    if not (attachment.content_type or "").startswith("image/"):
        return None
    if attachment.size > 8 * 1024 * 1024:
        return None
    try:
        raw = await attachment.read()
        return await asyncio.to_thread(_process_logo, raw)
    except Exception:
        log.exception("logo processing failed")
        return None


# ============================== قاعدة البيانات ==============================
SCHEMA = """
CREATE TABLE IF NOT EXISTS crews (
    guild_id INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    logo     BLOB,
    PRIMARY KEY (guild_id, role_id)
);
CREATE TABLE IF NOT EXISTS activity (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    group_id   INTEGER NOT NULL,
    day        TEXT NOT NULL,
    messages   INTEGER NOT NULL DEFAULT 0,
    voice_secs INTEGER NOT NULL DEFAULT 0,
    points     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, group_id, day)
);
CREATE INDEX IF NOT EXISTS idx_activity_day ON activity (guild_id, day);
CREATE TABLE IF NOT EXISTS ignored_channels (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS settings (
    guild_id      INTEGER PRIMARY KEY,
    board_channel INTEGER,
    board_message INTEGER,
    board_period  TEXT DEFAULT 'daily',
    announce_channel INTEGER,
    last_daily    TEXT,
    last_weekly   TEXT,
    last_monthly  TEXT
);
CREATE TABLE IF NOT EXISTS winners (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    start_day TEXT,
    end_day   TEXT,
    role_id   INTEGER,
    name      TEXT,
    points    INTEGER
);
"""

UPSERT = """
INSERT INTO activity (guild_id, user_id, group_id, day, messages, voice_secs, points)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(guild_id, user_id, group_id, day) DO UPDATE SET
    messages   = messages   + excluded.messages,
    voice_secs = voice_secs + excluded.voice_secs,
    points     = points     + excluded.points
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self.buffer: dict = {}

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        # ترقية قواعد بيانات قديمة (لو العمود موجود يتجاهل الخطأ)
        for col in ("announce_channel INTEGER", "last_daily TEXT", "last_weekly TEXT", "last_monthly TEXT"):
            try:
                await self.conn.execute(f"ALTER TABLE settings ADD COLUMN {col}")
            except Exception:
                pass
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def _all(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def _run(self, sql, params=()):
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.rowcount

    # ---- النشاط (يتجمع بالذاكرة ويتكتب كل شوي) ----
    def add_activity(self, guild_id, user_id, group_id, day, msgs=0, secs=0, pts=0):
        key = (guild_id, user_id, group_id, day)
        entry = self.buffer.setdefault(key, [0, 0, 0])
        entry[0] += msgs
        entry[1] += secs
        entry[2] += pts

    async def flush(self):
        if not self.buffer:
            return
        data, self.buffer = self.buffer, {}
        rows = [(g, u, gr, d, m, s, p) for (g, u, gr, d), (m, s, p) in data.items()]
        await self.conn.executemany(UPSERT, rows)
        await self.conn.commit()

    # ---- القروبات ----
    async def add_group(self, guild_id, role_id, name, logo):
        await self._run(
            """INSERT INTO crews (guild_id, role_id, name, logo) VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, role_id) DO UPDATE SET
                   name = excluded.name,
                   logo = COALESCE(excluded.logo, crews.logo)""",
            (guild_id, role_id, name, logo),
        )

    async def remove_group(self, guild_id, role_id) -> bool:
        # النقاط تبقى محفوظة، ولو سجلت نفس الرول مرة ثانية ترجع له
        n = await self._run("DELETE FROM crews WHERE guild_id=? AND role_id=?", (guild_id, role_id))
        return n > 0

    async def rename_group(self, guild_id, role_id, name) -> bool:
        n = await self._run(
            "UPDATE crews SET name=? WHERE guild_id=? AND role_id=?", (name, guild_id, role_id)
        )
        return n > 0

    async def set_logo(self, guild_id, role_id, logo) -> bool:
        n = await self._run(
            "UPDATE crews SET logo=? WHERE guild_id=? AND role_id=?", (logo, guild_id, role_id)
        )
        return n > 0

    async def list_groups(self, guild_id):
        return await self._all(
            "SELECT role_id, name, logo IS NOT NULL FROM crews WHERE guild_id=? ORDER BY name",
            (guild_id,),
        )

    async def group_map(self, guild_id) -> dict:
        rows = await self._all("SELECT role_id, name FROM crews WHERE guild_id=?", (guild_id,))
        return {r[0]: r[1] for r in rows}

    # ---- التوبات ----
    async def top_groups(self, guild_id, period, today, limit=9):
        clause, params = period_clause(period, today, "a.")
        sql = f"""
            SELECT g.role_id, g.name, g.logo, COALESCE(SUM(a.points), 0) AS pts
            FROM crews g
            LEFT JOIN activity a ON a.guild_id = g.guild_id AND a.group_id = g.role_id {clause}
            WHERE g.guild_id = ?
            GROUP BY g.role_id
            ORDER BY pts DESC, g.name
            LIMIT ?"""
        return await self._all(sql, params + [guild_id, limit])

    async def group_standing(self, guild_id, role_id, period, today):
        """يرجع (المركز، النقاط، الفرق عن المركز الأعلى، عدد القروبات) أو None."""
        rows = await self.top_groups(guild_id, period, today, limit=1000)
        for i, r in enumerate(rows):
            if r[0] == role_id:
                gap = int(rows[i - 1][3]) - int(r[3]) if i > 0 else None
                return i + 1, int(r[3]), gap, len(rows)
        return None

    async def top_members(self, guild_id, period, today, group_id=None, limit=10):
        clause, params = period_clause(period, today)
        extra = "AND group_id = ?" if group_id else ""
        # user_id = 0 هو تعديل نقاط القروب من الإدارة، ما يظهر في توب الأعضاء
        sql = f"""
            SELECT user_id, SUM(points) AS p, SUM(messages), SUM(voice_secs)
            FROM activity
            WHERE guild_id = ? AND user_id > 0 {extra} {clause}
            GROUP BY user_id HAVING p > 0
            ORDER BY p DESC LIMIT ?"""
        args = [guild_id] + ([group_id] if group_id else []) + params + [limit]
        return await self._all(sql, args)

    async def user_stats(self, guild_id, user_id, period, today):
        clause, params = period_clause(period, today)
        rows = await self._all(
            f"""SELECT COALESCE(SUM(points),0), COALESCE(SUM(messages),0), COALESCE(SUM(voice_secs),0)
                FROM activity WHERE guild_id=? AND user_id=? {clause}""",
            [guild_id, user_id] + params,
        )
        return rows[0]

    async def top_groups_between(self, guild_id, start: date, end: date, limit=3):
        return await self._all(
            """SELECT g.role_id, g.name, g.logo, COALESCE(SUM(a.points), 0) AS pts
               FROM crews g
               LEFT JOIN activity a ON a.guild_id = g.guild_id AND a.group_id = g.role_id
                    AND a.day BETWEEN ? AND ?
               WHERE g.guild_id = ?
               GROUP BY g.role_id
               ORDER BY pts DESC, g.name
               LIMIT ?""",
            (start.isoformat(), end.isoformat(), guild_id, limit),
        )

    # ---- سجل الفائزين ----
    async def add_winner(self, guild_id, kind, start: date, end: date, role_id, name, points):
        await self._run(
            """INSERT INTO winners (guild_id, kind, start_day, end_day, role_id, name, points)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, kind, start.isoformat(), end.isoformat(), role_id, name, points),
        )

    async def list_winners(self, guild_id, kind=None, limit=10):
        if kind:
            return await self._all(
                "SELECT kind, end_day, role_id, name, points FROM winners "
                "WHERE guild_id=? AND kind=? ORDER BY id DESC LIMIT ?",
                (guild_id, kind, limit),
            )
        return await self._all(
            "SELECT kind, end_day, role_id, name, points FROM winners "
            "WHERE guild_id=? ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        )

    # ---- الإعلانات التلقائية ----
    async def set_announce(self, guild_id, channel_id, today: date):
        base = (today - timedelta(days=1)).isoformat()
        await self._run(
            """INSERT INTO settings (guild_id, announce_channel, last_daily, last_weekly, last_monthly)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   announce_channel = excluded.announce_channel,
                   last_daily   = COALESCE(settings.last_daily, excluded.last_daily),
                   last_weekly  = COALESCE(settings.last_weekly, excluded.last_weekly),
                   last_monthly = COALESCE(settings.last_monthly, excluded.last_monthly)""",
            (guild_id, channel_id, base, base, base),
        )

    async def clear_announce(self, guild_id):
        # نصفر التواريخ عشان لما يتفعل مرة ثانية يبدأ دورات جديدة بدون إعلانات قديمة
        await self._run(
            """UPDATE settings SET announce_channel=NULL, last_daily=NULL,
               last_weekly=NULL, last_monthly=NULL WHERE guild_id=?""",
            (guild_id,),
        )

    async def announce_settings(self):
        rows = await self._all(
            """SELECT guild_id, announce_channel, last_daily, last_weekly, last_monthly
               FROM settings WHERE announce_channel IS NOT NULL"""
        )
        return [dict(r) for r in rows]

    async def mark_announced(self, guild_id, column: str, day: date):
        assert column in ("last_daily", "last_weekly", "last_monthly")
        await self._run(f"UPDATE settings SET {column}=? WHERE guild_id=?", (day.isoformat(), guild_id))

    # ---- الرومات المستثناة ----
    async def toggle_ignored(self, guild_id, channel_id) -> bool:
        rows = await self._all(
            "SELECT 1 FROM ignored_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id)
        )
        if rows:
            await self._run(
                "DELETE FROM ignored_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id)
            )
            return False
        await self._run(
            "INSERT INTO ignored_channels (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id)
        )
        return True

    async def ignored_set(self, guild_id) -> set:
        rows = await self._all("SELECT channel_id FROM ignored_channels WHERE guild_id=?", (guild_id,))
        return {r[0] for r in rows}

    # ---- اللوحة الثابتة ----
    async def set_board(self, guild_id, channel_id, period):
        await self._run(
            """INSERT INTO settings (guild_id, board_channel, board_message, board_period)
               VALUES (?, ?, NULL, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   board_channel = excluded.board_channel,
                   board_message = NULL,
                   board_period  = excluded.board_period""",
            (guild_id, channel_id, period),
        )

    async def clear_board(self, guild_id):
        await self._run(
            "UPDATE settings SET board_channel=NULL, board_message=NULL WHERE guild_id=?", (guild_id,)
        )

    async def set_board_message(self, guild_id, message_id):
        await self._run("UPDATE settings SET board_message=? WHERE guild_id=?", (message_id, guild_id))

    async def all_boards(self):
        return await self._all(
            """SELECT guild_id, board_channel, board_message, board_period
               FROM settings WHERE board_channel IS NOT NULL"""
        )


# ============================== صياغة الإعلانات ==============================
# كل النصوص هنا بدون نقطة بالنهاية وبدون فواصل، فقط كلام
ANNOUNCE_TEXTS = {
    "daily": {
        "header": "أفضل قروب لهذا اليوم",
        "first": "تصدر قروب {g} قائمة اليوم بعد أن جمع {p} نقطة",
        "closing": [
            "شكرا لكل عضو ساهم بتفاعله وننتظركم غدا بمنافسة جديدة",
            "يوم جميل مليء بالحماس وغدا فرصة جديدة لكل القروبات",
            "بارك الله في جهود الجميع ونلتقي غدا بتحدي جديد",
        ],
    },
    "weekly": {
        "header": "أفضل قروب للأسبوع الماضي",
        "first": "تألق قروب {g} على مدار الأسبوع وتوج بالمركز الأول بمجموع {p} نقطة",
        "closing": [
            "أسبوع مليء بالتفاعل والحماس وننتظر منافسة أقوى في الأسبوع القادم",
            "شكرا لكل من ساهم في هذا الأسبوع والفرصة متاحة للجميع من جديد",
            "عمل رائع من الجميع والمنافسة مستمرة بإذن الله",
        ],
    },
    "monthly": {
        "header": "أفضل قروب لهذا الشهر",
        "first": "ختم قروب {g} الشهر بالصدارة بعد جهد كبير وجمع {p} نقطة",
        "closing": [
            "شهر حافل بالإنجازات وشكرا لكل قروب شارك وتنافس",
            "نبارك للفائز ونتمنى للجميع شهرا قادما مليئا بالتفاعل",
            "جهد مميز من الجميع وبداية جديدة تنتظركم في الشهر القادم",
        ],
    },
}


def build_announcement(kind: str, rows) -> str:
    t = ANNOUNCE_TEXTS[kind]
    lines = [
        f"**{t['header']}**",
        "",
        t["first"].format(g=f"<@&{rows[0][0]}>", p=int(rows[0][3])),
    ]
    extra = [
        f"المركز {label} <@&{r[0]}> بمجموع {int(r[3])} نقطة"
        for label, r in zip(("الثاني", "الثالث"), rows[1:3])
        if r[3] > 0
    ]
    if extra:
        lines += [""] + extra
    lines += ["", random.choice(t["closing"])]
    return "\n".join(lines)


# ============================== واجهة اختيار الفترة ==============================
class PeriodSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=f"توب {label}", value=key, description=PERIOD_TITLES[key])
            for key, label in PERIODS.items()
        ]
        super().__init__(custom_id="top:period", placeholder="اختر الفترة", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        period = self.values[0]
        buf = await interaction.client.build_image(interaction.guild, period)
        if buf is None:
            await interaction.followup.send("ما فيه قروبات مسجلة بعد.", ephemeral=True)
            return
        await interaction.followup.send(file=discord.File(buf, "top_groups.png"), ephemeral=True)


class PeriodView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PeriodSelect())


# ============================== أدوات قراءة الأوامر ==============================
async def say(msg: discord.Message, text: Optional[str] = None, **kwargs):
    return await msg.reply(
        text, mention_author=False, allowed_mentions=discord.AllowedMentions.none(), **kwargs
    )


def parse_period(args) -> Optional[str]:
    for t in args:
        key = PERIOD_ALIASES.get(normalize(t))
        if key:
            return key
    return None


def roles_in(guild: discord.Guild, args) -> list:
    out = []
    for t in args:
        m = ROLE_RE.fullmatch(t)
        if m:
            role = guild.get_role(int(m.group(1)))
            if role:
                out.append(role)
    return out


def first_member(guild: discord.Guild, args) -> Optional[discord.Member]:
    for t in args:
        m = USER_RE.fullmatch(t)
        if m:
            member = guild.get_member(int(m.group(1)))
            if member:
                return member
    return None


def first_channel(guild: discord.Guild, args):
    for t in args:
        m = CHAN_RE.fullmatch(t)
        if m:
            ch = guild.get_channel(int(m.group(1)))
            if ch:
                return ch
    return None


def name_text(args) -> str:
    return " ".join(t for t in args if not ANY_MENTION_RE.fullmatch(t)).strip()


def first_number(args) -> Optional[int]:
    for t in args:
        if re.fullmatch(r"\d+", t):
            return int(t)
    return None


# ============================== سجل الأوامر ==============================
COMMANDS: list = []   # (prefix_tokens, func, admin, max_args)


def cmd(*names, admin: bool = False, max_args: Optional[int] = None):
    def deco(func):
        for n in names:
            COMMANDS.append((tuple(normalize(n).split()), func, admin, max_args))
        return func
    return deco


def match_command(content: str):
    tokens = content.split()
    if not tokens:
        return None
    norm = [normalize(t) for t in tokens]
    for prefix, func, admin, max_args in sorted(COMMANDS, key=lambda c: -len(c[0])):
        n = len(prefix)
        if tuple(norm[:n]) != prefix:
            continue
        rest = tokens[n:]
        if max_args is not None and len(rest) > max_args:
            continue
        return func, admin, rest
    return None


# ============================== البوت ==============================
class GroupTopBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True   # لازم تفعله من Developer Portal
        intents.members = True           # لازم تفعله من Developer Portal
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.db = Database(DB_PATH)
        self.group_cache: dict[int, dict[int, str]] = {}
        self.ignored_cache: dict[int, set] = {}
        self.render_cache: dict = {}
        self.last_msg_time: dict = {}
        self.last_msg_text: dict = {}
        self.trigger_cd: dict = {}
        self.cmd_cd: dict = {}
        self.last_checked_day: Optional[date] = None

    def today(self) -> date:
        return datetime.now(TZ).date()

    # ---- تهيئة ----
    async def setup_hook(self):
        await self.db.connect()
        self.add_view(PeriodView())
        await self.start_web()

        # ما عاد فيه أوامر سلاش: نزامن شجرة فاضية عشان تنحذف الأوامر القديمة من ديسكورد
        try:
            if GUILD_ID:
                await self.tree.sync(guild=discord.Object(id=int(GUILD_ID)))
            await self.tree.sync()
        except Exception:
            log.exception("clearing old slash commands failed")

        self.flush_loop.start()
        self.voice_tracker.start()
        self.board_updater.start()
        self.day_watcher.start()

    async def start_web(self):
        """سيرفر صغير يشتغل فقط على Render (لما يكون فيه PORT) عشان UptimeRobot."""
        port = os.environ.get("PORT")
        if not port:
            return
        app = web.Application()
        app.router.add_get("/", lambda request: web.Response(text="Bot is running"))
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(port)).start()
        log.info("web server on port %s", port)

    async def close(self):
        try:
            await self.db.flush()
            await self.db.close()
        finally:
            await super().close()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    # ---- كاشات مساعدة ----
    async def get_groups(self, guild_id) -> dict:
        if guild_id not in self.group_cache:
            self.group_cache[guild_id] = await self.db.group_map(guild_id)
        return self.group_cache[guild_id]

    async def get_ignored(self, guild_id) -> set:
        if guild_id not in self.ignored_cache:
            self.ignored_cache[guild_id] = await self.db.ignored_set(guild_id)
        return self.ignored_cache[guild_id]

    def invalidate(self, guild_id):
        self.group_cache.pop(guild_id, None)
        for key in [k for k in self.render_cache if k[0] == guild_id]:
            self.render_cache.pop(key, None)

    @staticmethod
    def pick_group(member: discord.Member, groups: dict) -> Optional[int]:
        """قروب العضو = أعلى رول قروب عنده."""
        for role in sorted(member.roles, key=lambda r: r.position, reverse=True):
            if role.id in groups:
                return role.id
        return None

    # ---- بناء الصورة ----
    async def build_image(self, guild: discord.Guild, period: str) -> Optional[io.BytesIO]:
        await self.db.flush()
        today = self.today()
        rows = await self.db.top_groups(guild.id, period, today)
        if not rows:
            return None
        key = (today.isoformat(), tuple((r[0], r[1], r[3], len(r[2] or b"")) for r in rows))
        cached = self.render_cache.get((guild.id, period))
        if cached and cached[0] == key:
            return io.BytesIO(cached[1])
        # شريط التقدم: نسبة نقاط القروب إلى نقاط قروب المركز الأول (من 0 إلى 1)
        top_pts = max((int(r[3]) for r in rows), default=0)
        groups = [
            {
                "name": r[1],
                "points": int(r[3]),
                "logo": r[2],
                "progress": (max(int(r[3]), 0) / top_pts) if top_pts > 0 else 0.0,
                "rank": i + 1,
            }
            for i, r in enumerate(rows)
        ]
        footer = f"{PERIOD_TITLES[period]}  •  {today:%Y-%m-%d}"
        buf = await asyncio.to_thread(render_top, groups, footer)
        data = buf.getvalue()
        self.render_cache[(guild.id, period)] = (key, data)
        return io.BytesIO(data)

    # ---- الرسائل ----
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        found = match_command(message.content)
        if found:
            func, admin, args = found
            if admin and not message.author.guild_permissions.manage_guild:
                await say(message, "هذا الأمر للإدارة فقط.")
                return
            if not admin:
                now = time.monotonic()
                if now - self.cmd_cd.get(message.author.id, 0) < COMMAND_COOLDOWN:
                    return
                self.cmd_cd[message.author.id] = now
            try:
                await func(self, message, args)
            except discord.HTTPException:
                log.exception("command reply failed")
            except Exception:
                log.exception("command error")
                try:
                    await say(message, "صار خطأ ، حاول مرة ثانية.")
                except discord.HTTPException:
                    pass
            return

        await self.count_message(message)

    async def count_message(self, message: discord.Message):
        guild = message.guild
        text = message.content.strip()
        if len(text) < MIN_CHAT_LENGTH:
            return
        if message.channel.id in await self.get_ignored(guild.id):
            return
        key = (guild.id, message.author.id)
        now = time.monotonic()
        if now - self.last_msg_time.get(key, 0) < CHAT_COOLDOWN:
            return
        if self.last_msg_text.get(key) == text.lower():
            return
        group_id = self.pick_group(message.author, await self.get_groups(guild.id))
        if group_id is None:
            return
        self.last_msg_time[key] = now
        self.last_msg_text[key] = text.lower()
        self.db.add_activity(
            guild.id, message.author.id, group_id, self.today().isoformat(),
            msgs=1, pts=CHAT_POINTS,
        )

    # ---- المهام الخلفية ----
    @tasks.loop(seconds=15)
    async def flush_loop(self):
        try:
            await self.db.flush()
        except Exception:
            log.exception("flush failed")

    @tasks.loop(seconds=60)
    async def voice_tracker(self):
        day = self.today().isoformat()
        for guild in self.guilds:
            groups = await self.get_groups(guild.id)
            if not groups:
                continue
            ignored = await self.get_ignored(guild.id)
            for ch in guild.channels:
                if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                    continue
                if ch.id in ignored or ch == guild.afk_channel:
                    continue
                humans = [m for m in ch.members if not m.bot]
                if len(humans) < MIN_VOICE_USERS:
                    continue
                for m in humans:
                    if m.voice is None or m.voice.self_deaf or m.voice.deaf:
                        continue
                    group_id = self.pick_group(m, groups)
                    if group_id is None:
                        continue
                    self.db.add_activity(
                        guild.id, m.id, group_id, day, secs=60, pts=VOICE_POINTS_PER_MINUTE
                    )

    @tasks.loop(minutes=BOARD_REFRESH_MINUTES)
    async def board_updater(self):
        for guild_id, channel_id, message_id, period in await self.db.all_boards():
            guild = self.get_guild(guild_id)
            if guild:
                try:
                    await self.update_board(guild, channel_id, message_id, period or DEFAULT_PERIOD)
                except Exception:
                    log.exception("board update failed for guild %s", guild_id)

    async def update_board(self, guild, channel_id, message_id, period):
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        buf = await self.build_image(guild, period)
        if buf is None:
            return
        data = buf.getvalue()

        def new_file():
            return discord.File(io.BytesIO(data), "top_groups.png")

        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(attachments=[new_file()], view=PeriodView())
                return
            except discord.NotFound:
                pass
        msg = await channel.send(file=new_file(), view=PeriodView())
        await self.db.set_board_message(guild.id, msg.id)

    @tasks.loop(seconds=30)
    async def day_watcher(self):
        """عند تغير اليوم (وعند تشغيل البوت) يرسل الإعلانات المستحقة ويحدّث اللوحات."""
        today = self.today()
        if today == self.last_checked_day:
            return
        self.last_checked_day = today
        await self.db.flush()
        for row in await self.db.announce_settings():
            try:
                await self.run_announcements(row, today)
            except Exception:
                log.exception("announcements failed for guild %s", row["guild_id"])
        for guild_id, channel_id, message_id, period in await self.db.all_boards():
            guild = self.get_guild(guild_id)
            if guild:
                try:
                    await self.update_board(guild, channel_id, message_id, period or DEFAULT_PERIOD)
                except Exception:
                    log.exception("board update failed for guild %s", guild_id)

    async def run_announcements(self, row: dict, today: date):
        guild = self.get_guild(row["guild_id"])
        channel = guild.get_channel(row["announce_channel"]) if guild else None
        if channel is None:
            return
        yesterday = today - timedelta(days=1)

        # يومي: كل يوم عند 12 بالليل يعلن فائز اليوم اللي انتهى
        if date.fromisoformat(row["last_daily"]) < yesterday:
            await self.announce(guild, channel, "daily", yesterday, yesterday)
            await self.db.mark_announced(guild.id, "last_daily", yesterday)

        # أسبوعي كل 7 أيام وشهري كل 30 يوم (دورات متتالية تبدأ من يوم تفعيل الإعلانات)
        for kind, column, length in (("weekly", "last_weekly", 7), ("monthly", "last_monthly", 30)):
            last = date.fromisoformat(row[column])
            cycles = (yesterday - last).days // length
            if cycles >= 1:
                end = last + timedelta(days=cycles * length)
                start = end - timedelta(days=length - 1)
                await self.announce(guild, channel, kind, start, end)
                await self.db.mark_announced(guild.id, column, end)

    async def announce(self, guild, channel, kind: str, start: date, end: date):
        rows = await self.db.top_groups_between(guild.id, start, end, limit=3)
        if not rows or rows[0][3] <= 0:
            return
        await channel.send(
            build_announcement(kind, rows),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.db.add_winner(guild.id, kind, start, end, rows[0][0], rows[0][1], int(rows[0][3]))


bot = GroupTopBot()


# ============================== أوامر الأعضاء ==============================
@cmd("توب جروب", "توب جروبات", "توب قروب", "توب قروبات", "توب القروبات", "توب الجروبات")
async def c_top_groups(bot: GroupTopBot, msg: discord.Message, args):
    now = time.monotonic()
    if now - bot.trigger_cd.get(msg.channel.id, 0) < 10:
        return
    bot.trigger_cd[msg.channel.id] = now
    period = parse_period(args) or DEFAULT_PERIOD
    async with msg.channel.typing():
        buf = await bot.build_image(msg.guild, period)
    if buf is None:
        await say(msg, "ما فيه قروبات مسجلة بعد. الأدمن يسجل القروبات بأمر **اضافة قروب**")
        return
    await say(msg, file=discord.File(buf, "top_groups.png"), view=PeriodView())


@cmd("توب اعضاء", "توب الاعضاء", "توب الاشخاص")
async def c_top_members(bot: GroupTopBot, msg: discord.Message, args):
    await bot.db.flush()
    period = parse_period(args) or DEFAULT_PERIOD
    roles = roles_in(msg.guild, args)
    group = roles[0] if roles else None
    rows = await bot.db.top_members(msg.guild.id, period, bot.today(), group.id if group else None)
    if not rows:
        await say(msg, "ما فيه نشاط مسجل لهذي الفترة.")
        return
    lines = []
    for i, (user_id, pts, msgs, secs) in enumerate(rows):
        rank = MEDALS[i] if i < 3 else f"`{i + 1}`"
        lines.append(f"{rank} <@{user_id}> — **{int(pts):,}** نقطة • {int(msgs):,} رسالة • {fmt_time(int(secs))}")
    title = f"{PERIOD_TITLES[period]} — الأعضاء" + (f" ({group.name})" if group else "")
    await say(msg, embed=discord.Embed(title=title, description="\n".join(lines), color=EMBED_COLOR))


@cmd("احصائياتي", "احصائيات", "نشاطي", max_args=1)
async def c_my_stats(bot: GroupTopBot, msg: discord.Message, args):
    member = first_member(msg.guild, args) or msg.author
    await bot.db.flush()
    groups = await bot.get_groups(msg.guild.id)
    group_id = bot.pick_group(member, groups)
    embed = discord.Embed(
        title=f"إحصائيات {member.display_name}",
        description=f"القروب: <@&{group_id}>" if group_id else "ما عنده قروب مسجل.",
        color=EMBED_COLOR,
    )
    for key, label in PERIODS.items():
        pts, msgs, secs = await bot.db.user_stats(msg.guild.id, member.id, key, bot.today())
        embed.add_field(
            name=label,
            value=f"⭐ {int(pts):,} نقطة\n💬 {int(msgs):,} رسالة\n🎙️ {fmt_time(int(secs))}",
            inline=True,
        )
    await say(msg, embed=embed)


async def group_embed(bot: GroupTopBot, guild: discord.Guild, role_id: int) -> discord.Embed:
    await bot.db.flush()
    today = bot.today()
    groups = await bot.get_groups(guild.id)
    embed = discord.Embed(title=f"قروب {groups.get(role_id, 'غير معروف')}", color=EMBED_COLOR)
    role = guild.get_role(role_id)
    if role:
        count = len([m for m in role.members if not m.bot])
        embed.description = f"الرول: {role.mention}\n👥 عدد الأعضاء: {count}"
    for key, label in PERIODS.items():
        st = await bot.db.group_standing(guild.id, role_id, key, today)
        if st is None:
            continue
        rank, pts, gap, total = st
        value = f"🏅 المركز {rank} من {total}\n⭐ {pts:,} نقطة"
        if gap:
            value += f"\n⬆️ ناقص {gap:,} نقطة للمركز الأعلى"
        embed.add_field(name=label, value=value, inline=True)
    rows = await bot.db.top_members(guild.id, "all", today, group_id=role_id, limit=3)
    if rows:
        embed.add_field(
            name="أفضل الأعضاء (كلي)",
            value="\n".join(f"{MEDALS[i]} <@{u}> — {int(p):,} نقطة" for i, (u, p, _, _) in enumerate(rows)),
            inline=False,
        )
    return embed


@cmd("قروبي", "جروبي", max_args=0)
async def c_my_group(bot: GroupTopBot, msg: discord.Message, args):
    groups = await bot.get_groups(msg.guild.id)
    group_id = bot.pick_group(msg.author, groups)
    if group_id is None:
        await say(msg, "ما عندك قروب مسجل.")
        return
    await say(msg, embed=await group_embed(bot, msg.guild, group_id))


@cmd("معلومات قروب", "معلومات جروب", "ترتيب قروب", "ترتيب جروب")
async def c_group_info(bot: GroupTopBot, msg: discord.Message, args):
    roles = roles_in(msg.guild, args)
    if not roles:
        await say(msg, "اذكر رول القروب ، مثال: **معلومات قروب @الرول**")
        return
    groups = await bot.get_groups(msg.guild.id)
    if roles[0].id not in groups:
        await say(msg, "هذا الرول غير مسجل كقروب.")
        return
    await say(msg, embed=await group_embed(bot, msg.guild, roles[0].id))


@cmd("مقارنة")
async def c_compare(bot: GroupTopBot, msg: discord.Message, args):
    roles = roles_in(msg.guild, args)
    if len(roles) < 2:
        await say(msg, "اذكر قروبين ، مثال: **مقارنة @قروب1 @قروب2**")
        return
    a, b = roles[0], roles[1]
    groups = await bot.get_groups(msg.guild.id)
    if a.id not in groups or b.id not in groups:
        await say(msg, "أحد الرولات غير مسجل كقروب.")
        return
    await bot.db.flush()
    today = bot.today()
    embed = discord.Embed(title="مقارنة القروبات", description=f"{a.mention}  ⚔️  {b.mention}", color=EMBED_COLOR)
    for key, label in PERIODS.items():
        sa = await bot.db.group_standing(msg.guild.id, a.id, key, today)
        sb = await bot.db.group_standing(msg.guild.id, b.id, key, today)
        pa, pb = sa[1], sb[1]
        if pa == pb:
            verdict = "تعادل 🤝"
        else:
            leader = a if pa > pb else b
            verdict = f"{leader.mention} متقدم بـ {abs(pa - pb):,} نقطة"
        embed.add_field(
            name=label,
            value=f"{pa:,}  —  {pb:,}\n{verdict}",
            inline=True,
        )
    await say(msg, embed=embed)


@cmd("سجل الفائزين", "الفائزين")
async def c_winners(bot: GroupTopBot, msg: discord.Message, args):
    kind = parse_period(args)
    if kind == "all":
        kind = None
    rows = await bot.db.list_winners(msg.guild.id, kind, limit=10)
    if not rows:
        await say(msg, "ما فيه فائزين مسجلين بعد ، السجل يبدأ مع الإعلانات التلقائية.")
        return
    lines = []
    for k, end_day, role_id, name, pts in rows:
        role = msg.guild.get_role(role_id)
        who = role.mention if role else name
        lines.append(f"🏆 **{PERIODS.get(k, k)}** • {end_day} — {who} ({pts:,} نقطة)")
    await say(msg, embed=discord.Embed(title="سجل الفائزين", description="\n".join(lines), color=EMBED_COLOR))


@cmd("القروبات", "الجروبات", "قائمة القروبات", max_args=0)
async def c_list_groups(bot: GroupTopBot, msg: discord.Message, args):
    rows = await bot.db.list_groups(msg.guild.id)
    if not rows:
        await say(msg, "ما فيه قروبات مسجلة.")
        return
    lines = []
    for role_id, name, has_logo in rows:
        role = msg.guild.get_role(role_id)
        count = len([m for m in role.members if not m.bot]) if role else 0
        lines.append(f"• <@&{role_id}> — {name} • {count} عضو • {'🖼️' if has_logo else 'بدون لوقو'}")
    await say(msg, "\n".join(lines))


@cmd("معلومات", "كيف تنحسب النقاط", max_args=0)
async def c_info(bot: GroupTopBot, msg: discord.Message, args):
    embed = discord.Embed(
        title="كيف يحسب بوت التوب؟",
        description=(
            f"💬 **الكتابة :** {CHAT_POINTS} نقطة لكل رسالة (حد أدنى {MIN_CHAT_LENGTH} حروف ، "
            f"ورسالة وحدة كل {CHAT_COOLDOWN} ثانية ، والمكرر ما ينحسب يزاحف )\n"
            f"🎙️ **الفويس :** {VOICE_POINTS_PER_MINUTE} نقطة لكل دقيقة (**لازم تكون مع شخص ثاني بالروم عشان يحسب لك يذات الوزن الكثير** "
            f"على الأقل ، وما تكون مسوي ديفن)\n"
            "🏆 **نقاط القروب** : **مجموع نقاط كل أعضائه**\n"
            "🔄 النقاط الكلية ما تتصفر أبدا ، واليومي والأسبوعي (**آخر 7 أيام**) والشهري (**آخر 30 يوم**)\n"
            "📣 **تنزل إعلانات تلقائية بأفضل قروب يومي وأسبوعي وشهري**\n\n"
            "اكتب **توب قروب** في أي روم وتشوف الصورة لتوب القروبات يالحشري\n"
            "واكتب **الاوامر** تشوف كل الأوامر\n"
            "**Bot Developer : Eleven** , Store RAV "
        ),
        color=EMBED_COLOR,
    )
    await say(msg, embed=embed)


@cmd("الاوامر", "اوامر", "مساعدة", "قائمة الاوامر", max_args=0)
async def c_help(bot: GroupTopBot, msg: discord.Message, args):
    embed = discord.Embed(title="أوامر بوت التوب", color=EMBED_COLOR)
    embed.add_field(
        name="للجميع",
        value=(
            "• **توب قروب** [يومي / اسبوعي / شهري / كلي]\n"
            "• **توب اعضاء** [الفترة] [@قروب]\n"
            "• **احصائياتي** أو **احصائيات @عضو**\n"
            "• **قروبي** : ترتيب قروبك ونقاطه\n"
            "• **معلومات قروب @قروب** : ترتيب ونقاط أي قروب\n"
            "• **مقارنة @قروب1 @قروب2**\n"
            "• **سجل الفائزين** [الفترة]\n"
            "• **القروبات** : القروبات المسجلة\n"
            "• **معلومات** : كيف تنحسب النقاط"
        ),
        inline=False,
    )
    if msg.author.guild_permissions.manage_guild:
        embed.add_field(
            name="للإدارة",
            value=(
                "• **اضافة قروب @رول** [الاسم] + صورة اللوقو (اختياري)\n"
                "• **حذف قروب @رول**\n"
                "• **تغيير اسم قروب @رول الاسم الجديد**\n"
                "• **تغيير لوقو @رول** + صورة\n"
                "• **اضافة نقاط @عضو 50** أو **@قروب 50**\n"
                "• **خصم نقاط @عضو 50** أو **@قروب 50**\n"
                "• **روم الاعلانات #روم** / **ايقاف الاعلانات**\n"
                "• **تجربة اعلان** يومي / اسبوعي / شهري\n"
                "• **روم اللوحة #روم** [الفترة] / **ايقاف اللوحة**\n"
                "• **استثناء روم #روم** (كررها للإلغاء) / **الرومات المستثناة**\n"
                "• **نسخة احتياطية** (تجيك خاص)\n"
                "• **تجربة الاحداثيات**"
            ),
            inline=False,
        )
    await say(msg, embed=embed)


# ============================== أوامر الإدارة ==============================
@cmd("اضافة قروب", "اضافة جروب", "تسجيل قروب", "تسجيل جروب", admin=True)
async def c_add_group(bot: GroupTopBot, msg: discord.Message, args):
    roles = roles_in(msg.guild, args)
    if not roles:
        await say(msg, "اذكر رول القروب ، مثال: **اضافة قروب @الرول الاسم**")
        return
    role = roles[0]
    if role.is_default():
        await say(msg, "ما تقدر تختار رول @everyone.")
        return
    logo_bytes = None
    if msg.attachments:
        logo_bytes = await prepare_logo(msg.attachments[0])
        if logo_bytes is None:
            await say(msg, "الصورة غير صالحة (لازم صورة أقل من 8MB).")
            return
    name = name_text(args) or role.name
    await bot.db.add_group(msg.guild.id, role.id, name[:30], logo_bytes)
    bot.invalidate(msg.guild.id)
    await say(msg, f"تم تسجيل قروب {role.mention} ✅")


@cmd("حذف قروب", "حذف جروب", admin=True)
async def c_remove_group(bot: GroupTopBot, msg: discord.Message, args):
    roles = roles_in(msg.guild, args)
    if not roles:
        await say(msg, "اذكر رول القروب ، مثال: **حذف قروب @الرول**")
        return
    ok = await bot.db.remove_group(msg.guild.id, roles[0].id)
    bot.invalidate(msg.guild.id)
    await say(msg, "تم حذف القروب من التوب والنقاط محفوظة ✅" if ok else "هذا الرول غير مسجل كقروب.")


@cmd("تغيير اسم قروب", "تغيير اسم جروب", admin=True)
async def c_rename_group(bot: GroupTopBot, msg: discord.Message, args):
    roles = roles_in(msg.guild, args)
    name = name_text(args)
    if not roles or not name:
        await say(msg, "مثال: **تغيير اسم قروب @الرول الاسم الجديد**")
        return
    ok = await bot.db.rename_group(msg.guild.id, roles[0].id, name[:30])
    bot.invalidate(msg.guild.id)
    await say(msg, "تم تغيير الاسم ✅" if ok else "هذا الرول غير مسجل كقروب.")


@cmd("تغيير لوقو", "لوقو قروب", "تغيير شعار", admin=True)
async def c_logo(bot: GroupTopBot, msg: discord.Message, args):
    roles = roles_in(msg.guild, args)
    if not roles or not msg.attachments:
        await say(msg, "اذكر الرول وارفق صورة اللوقو في نفس الرسالة ، مثال: **تغيير لوقو @الرول** + صورة")
        return
    logo_bytes = await prepare_logo(msg.attachments[0])
    if logo_bytes is None:
        await say(msg, "الصورة غير صالحة (لازم صورة أقل من 8MB).")
        return
    ok = await bot.db.set_logo(msg.guild.id, roles[0].id, logo_bytes)
    bot.invalidate(msg.guild.id)
    await say(msg, "تم تغيير اللوقو ✅" if ok else "هذا الرول غير مسجل كقروب.")


async def adjust_points(bot: GroupTopBot, msg: discord.Message, args, sign: int):
    amount = first_number(args)
    if amount is None:
        word = "اضافة" if sign > 0 else "خصم"
        await say(msg, f"مثال: **{word} نقاط @عضو 50** أو **{word} نقاط @قروب 50**")
        return
    amount *= sign
    groups = await bot.get_groups(msg.guild.id)
    member = first_member(msg.guild, args)
    if member:
        group_id = bot.pick_group(member, groups)
        if group_id is None:
            await say(msg, "هذا العضو ما عنده رول قروب مسجل.")
            return
        bot.db.add_activity(msg.guild.id, member.id, group_id, bot.today().isoformat(), pts=amount)
        target = member.mention
    else:
        roles = roles_in(msg.guild, args)
        if not roles:
            await say(msg, "اذكر العضو أو القروب اللي تبي تعدل نقاطه.")
            return
        if roles[0].id not in groups:
            await say(msg, "هذا الرول غير مسجل كقروب.")
            return
        # user_id = 0 يعني تعديل مباشر على نقاط القروب
        bot.db.add_activity(msg.guild.id, 0, roles[0].id, bot.today().isoformat(), pts=amount)
        target = roles[0].mention
    bot.render_cache.clear()
    await say(msg, f"تم تعديل نقاط {target} بمقدار **{amount:+,}** ✅")


@cmd("اضافة نقاط", "زيادة نقاط", admin=True)
async def c_add_points(bot: GroupTopBot, msg: discord.Message, args):
    await adjust_points(bot, msg, args, +1)


@cmd("خصم نقاط", "انقاص نقاط", admin=True)
async def c_sub_points(bot: GroupTopBot, msg: discord.Message, args):
    await adjust_points(bot, msg, args, -1)


@cmd("روم الاعلانات", "تحديد روم الاعلانات", admin=True)
async def c_set_announce(bot: GroupTopBot, msg: discord.Message, args):
    channel = first_channel(msg.guild, args)
    if not isinstance(channel, discord.TextChannel):
        await say(msg, "اذكر الروم الكتابي ، مثال: **روم الاعلانات #الروم**")
        return
    await bot.db.set_announce(msg.guild.id, channel.id, bot.today())
    await say(
        msg,
        f"تم تحديد {channel.mention} للإعلانات ✅\n"
        "يومي عند الساعة 12 بالليل، وأسبوعي كل 7 أيام، وشهري كل 30 يوم",
    )


@cmd("ايقاف الاعلانات", "الغاء الاعلانات", admin=True)
async def c_stop_announce(bot: GroupTopBot, msg: discord.Message, args):
    await bot.db.clear_announce(msg.guild.id)
    await say(msg, "تم إيقاف الإعلانات التلقائية ✅")


@cmd("تجربة اعلان", admin=True)
async def c_test_announce(bot: GroupTopBot, msg: discord.Message, args):
    kind = parse_period(args)
    if kind not in ("daily", "weekly", "monthly"):
        await say(msg, "مثال: **تجربة اعلان يومي** أو اسبوعي أو شهري")
        return
    await bot.db.flush()
    found = [r for r in await bot.db.announce_settings() if r["guild_id"] == msg.guild.id]
    channel = msg.guild.get_channel(found[0]["announce_channel"]) if found else None
    if channel is None:
        await say(msg, "حدد روم الإعلانات أولا بأمر **روم الاعلانات #الروم**")
        return
    days = {"daily": 1, "weekly": 7, "monthly": 30}[kind]
    today = bot.today()
    rows = await bot.db.top_groups_between(msg.guild.id, today - timedelta(days=days - 1), today, limit=3)
    if not rows or rows[0][3] <= 0:
        await say(msg, "ما فيه نقاط كافية للتجربة بعد.")
        return
    await channel.send(build_announcement(kind, rows), allowed_mentions=discord.AllowedMentions.none())
    await say(msg, f"تم إرسال التجربة في {channel.mention} ✅")


@cmd("استثناء روم", "استثناء", admin=True)
async def c_exclude(bot: GroupTopBot, msg: discord.Message, args):
    channel = first_channel(msg.guild, args)
    if channel is None:
        await say(msg, "اذكر الروم ، مثال: **استثناء روم #الروم** (كررها للإلغاء)")
        return
    now_ignored = await bot.db.toggle_ignored(msg.guild.id, channel.id)
    bot.ignored_cache.pop(msg.guild.id, None)
    await say(msg, f"{channel.mention} " + ("صار مستثنى من النقاط ✅" if now_ignored else "رجع يحسب النقاط ✅"))


@cmd("الرومات المستثناة", "المستثنيات", admin=True)
async def c_list_excluded(bot: GroupTopBot, msg: discord.Message, args):
    ids = await bot.get_ignored(msg.guild.id)
    if not ids:
        await say(msg, "ما فيه رومات مستثناة.")
        return
    await say(msg, "الرومات المستثناة:\n" + "\n".join(f"• <#{i}>" for i in ids))


@cmd("روم اللوحة", "تحديد روم اللوحة", admin=True)
async def c_set_board(bot: GroupTopBot, msg: discord.Message, args):
    channel = first_channel(msg.guild, args)
    if not isinstance(channel, discord.TextChannel):
        await say(msg, "اذكر الروم الكتابي ، مثال: **روم اللوحة #الروم** [الفترة]")
        return
    period = parse_period(args) or DEFAULT_PERIOD
    async with msg.channel.typing():
        await bot.db.set_board(msg.guild.id, channel.id, period)
        await bot.update_board(msg.guild, channel.id, None, period)
    await say(msg, f"تم تفعيل اللوحة في {channel.mention} وتتحدث كل {BOARD_REFRESH_MINUTES} دقايق ✅")


@cmd("ايقاف اللوحة", "الغاء اللوحة", admin=True)
async def c_stop_board(bot: GroupTopBot, msg: discord.Message, args):
    await bot.db.clear_board(msg.guild.id)
    await say(msg, "تم إيقاف اللوحة الثابتة ✅ (الرسالة القديمة تقدر تحذفها يدويا)")


@cmd("نسخة احتياطية", "نسخه احتياطيه", admin=True)
async def c_backup(bot: GroupTopBot, msg: discord.Message, args):
    await bot.db.flush()
    try:
        await msg.author.send(file=discord.File(DB_PATH))
        await say(msg, "أرسلت لك النسخة الاحتياطية في الخاص ✅")
    except discord.Forbidden:
        await say(msg, "ما قدرت أرسل لك في الخاص ، افتح الرسائل الخاصة وحاول مرة ثانية.")


@cmd("تجربة الاحداثيات", admin=True)
async def c_debug_slots(bot: GroupTopBot, msg: discord.Message, args):
    buf = await asyncio.to_thread(debug_slots)
    await say(msg, file=discord.File(buf, "debug.png"))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("ضع التوكن في ملف .env بهذا الشكل: TOKEN=xxxxx")
    bot.run(TOKEN)
