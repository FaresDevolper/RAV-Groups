"""
main.py - بوت توب القروبات

كل شي يتحسب تلقائياً:
  - الكتابة في الشات  -> نقاط لكل رسالة (مع كولداون ومنع السبام)
  - الجلوس في الفويس  -> نقاط لكل دقيقة
  - نقاط القروب = مجموع نقاط كل أعضائه
  - النقاط الكلية ما تتصفر أبداً (يومي/أسبوعي/شهري مجرد عرض لنفس النقاط)
  - تكتب "توب جروب" في أي روم -> تجي صورة التوب
  - لوحة توب ثابتة في روم تتحدث كل 10 دقايق
  - إعلانات تلقائية في روم محدد: يومي الساعة 12 بالليل، أسبوعي كل 7 أيام، شهري كل 30 يوم

الملفات (3 فقط): main.py, top_image.py, requirements.txt
القالب والخطوط مدمجين داخل top_image.py فما تحتاج أي ملف ثاني.

المتغيرات (على Render تحطها في Environment، وعلى جهازك في ملف .env):
  GUILD_ID=1425195739591610451      (اختياري - يخلي الأوامر تظهر فوراً)
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
from discord import app_commands
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
DEFAULT_PERIOD = "daily"        # الفترة الافتراضية لما تكتب "توب جروب"

PERIODS = {"daily": "يومي", "weekly": "أسبوعي", "monthly": "شهري", "all": "كلي"}
PERIOD_TITLES = {
    "daily": "توب اليوم",
    "weekly": "توب آخر 7 أيام",
    "monthly": "توب آخر 30 يوم",
    "all": "التوب الكلي",
}
PERIOD_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in PERIODS.items()]
MEDALS = ["🥇", "🥈", "🥉"]


def normalize(text: str) -> str:
    text = text.strip().lower()
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text)


TRIGGERS = {
    normalize(t)
    for t in (
        "توب جروب", "توب جروبات", "توب قروب", "توب قروبات",
        "توب القروبات", "توب الجروبات", "top groups", "top group",
    )
}


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

    async def top_members(self, guild_id, period, today, group_id=None, limit=10):
        clause, params = period_clause(period, today)
        extra = "AND group_id = ?" if group_id else ""
        sql = f"""
            SELECT user_id, SUM(points) AS p, SUM(messages), SUM(voice_secs)
            FROM activity
            WHERE guild_id = ? {extra} {clause}
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
ANNOUNCE_CHOICES = [
    app_commands.Choice(name="يومي", value="daily"),
    app_commands.Choice(name="أسبوعي", value="weekly"),
    app_commands.Choice(name="شهري", value="monthly"),
]


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
        self.last_checked_day: Optional[date] = None

    def today(self) -> date:
        return datetime.now(TZ).date()

    # ---- تهيئة ----
    async def setup_hook(self):
        await self.db.connect()
        self.add_view(PeriodView())
        await self.start_web()

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

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
        groups = [{"name": r[1], "points": int(r[3]), "logo": r[2]} for r in rows]
        footer = f"{PERIOD_TITLES[period]}  •  {today:%Y-%m-%d}"
        buf = await asyncio.to_thread(render_top, groups, footer)
        data = buf.getvalue()
        self.render_cache[(guild.id, period)] = (key, data)
        return io.BytesIO(data)

    # ---- الرسائل ----
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        if normalize(message.content) in TRIGGERS:
            await self.handle_trigger(message)
            return

        await self.count_message(message)

    async def handle_trigger(self, message: discord.Message):
        now = time.monotonic()
        if now - self.trigger_cd.get(message.channel.id, 0) < 10:
            return
        self.trigger_cd[message.channel.id] = now
        try:
            async with message.channel.typing():
                buf = await self.build_image(message.guild, DEFAULT_PERIOD)
            if buf is None:
                await message.reply(
                    "ما فيه قروبات مسجلة بعد. الأدمن يسجل القروبات بأمر `/group_add`",
                    mention_author=False,
                )
                return
            await message.reply(
                file=discord.File(buf, "top_groups.png"),
                view=PeriodView(),
                mention_author=False,
            )
        except discord.HTTPException:
            log.exception("trigger reply failed")

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

    @flush_loop.before_loop
    @voice_tracker.before_loop
    @board_updater.before_loop
    @day_watcher.before_loop
    async def _wait_ready(self):
        await self.wait_until_ready()


bot = GroupTopBot()


# ============================== الأوامر ==============================
def admin_only(func):
    func = app_commands.checks.has_permissions(manage_guild=True)(func)
    func = app_commands.default_permissions(manage_guild=True)(func)
    func = app_commands.guild_only()(func)
    return func


@bot.tree.command(name="group_add", description="تسجيل قروب جديد في التوب")
@app_commands.describe(role="رول القروب", name="الاسم اللي يظهر بالصورة (اختياري)", logo="لوقو القروب (اختياري)")
@admin_only
async def group_add(
    interaction: discord.Interaction,
    role: discord.Role,
    name: Optional[str] = None,
    logo: Optional[discord.Attachment] = None,
):
    if role.is_default():
        await interaction.response.send_message("ما تقدر تختار رول @everyone.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    logo_bytes = None
    if logo:
        logo_bytes = await prepare_logo(logo)
        if logo_bytes is None:
            await interaction.followup.send("الصورة غير صالحة (لازم صورة أقل من 8MB).", ephemeral=True)
            return
    await bot.db.add_group(interaction.guild_id, role.id, (name or role.name)[:30], logo_bytes)
    bot.invalidate(interaction.guild_id)
    await interaction.followup.send(f"تم تسجيل قروب {role.mention} ✅", ephemeral=True)


@bot.tree.command(name="group_remove", description="حذف قروب من التوب (نقاطه تنحفظ)")
@app_commands.describe(role="رول القروب")
@admin_only
async def group_remove(interaction: discord.Interaction, role: discord.Role):
    ok = await bot.db.remove_group(interaction.guild_id, role.id)
    bot.invalidate(interaction.guild_id)
    await interaction.response.send_message(
        "تم حذف القروب من التوب والنقاط محفوظة ✅" if ok else "هذا الرول غير مسجل كقروب.", ephemeral=True
    )


@bot.tree.command(name="group_logo", description="تغيير لوقو قروب")
@app_commands.describe(role="رول القروب", logo="صورة اللوقو")
@admin_only
async def group_logo(interaction: discord.Interaction, role: discord.Role, logo: discord.Attachment):
    await interaction.response.defer(ephemeral=True)
    logo_bytes = await prepare_logo(logo)
    if logo_bytes is None:
        await interaction.followup.send("الصورة غير صالحة (لازم صورة أقل من 8MB).", ephemeral=True)
        return
    ok = await bot.db.set_logo(interaction.guild_id, role.id, logo_bytes)
    bot.invalidate(interaction.guild_id)
    await interaction.followup.send("تم تغيير اللوقو ✅" if ok else "هذا الرول غير مسجل كقروب.", ephemeral=True)


@bot.tree.command(name="group_list", description="عرض القروبات المسجلة")
@admin_only
async def group_list(interaction: discord.Interaction):
    rows = await bot.db.list_groups(interaction.guild_id)
    if not rows:
        await interaction.response.send_message("ما فيه قروبات مسجلة.", ephemeral=True)
        return
    lines = []
    for role_id, name, has_logo in rows:
        role = interaction.guild.get_role(role_id)
        count = len([m for m in role.members if not m.bot]) if role else 0
        lines.append(f"• <@&{role_id}> — {name} • {count} عضو • {'🖼️' if has_logo else 'بدون لوقو'}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="points", description="إضافة أو خصم نقاط لعضو (اكتب رقم سالب للخصم)")
@app_commands.describe(member="العضو", amount="عدد النقاط (يقبل سالب)")
@admin_only
async def points(interaction: discord.Interaction, member: discord.Member, amount: int):
    group_id = bot.pick_group(member, await bot.get_groups(interaction.guild_id))
    if group_id is None:
        await interaction.response.send_message("هذا العضو ما عنده رول قروب مسجل.", ephemeral=True)
        return
    bot.db.add_activity(interaction.guild_id, member.id, group_id, bot.today().isoformat(), pts=amount)
    bot.render_cache.clear()
    await interaction.response.send_message(
        f"تم تعديل نقاط {member.mention} بمقدار **{amount:+,}** ✅", ephemeral=True
    )


@bot.tree.command(name="set_announce", description="تحديد روم الإعلانات التلقائية (يومي وأسبوعي وشهري)")
@app_commands.describe(channel="الروم")
@admin_only
async def set_announce(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.db.set_announce(interaction.guild_id, channel.id, bot.today())
    await interaction.response.send_message(
        f"تم تحديد {channel.mention} للإعلانات ✅\n"
        "يومي عند الساعة 12 بالليل، وأسبوعي كل 7 أيام، وشهري كل 30 يوم",
        ephemeral=True,
    )


@bot.tree.command(name="announce_test", description="تجربة شكل الإعلان (يرسله في روم الإعلانات)")
@app_commands.describe(kind="نوع الإعلان")
@app_commands.choices(kind=ANNOUNCE_CHOICES)
@admin_only
async def announce_test(interaction: discord.Interaction, kind: app_commands.Choice[str]):
    await bot.db.flush()
    found = [r for r in await bot.db.announce_settings() if r["guild_id"] == interaction.guild_id]
    channel = interaction.guild.get_channel(found[0]["announce_channel"]) if found else None
    if channel is None:
        await interaction.response.send_message("حدد روم الإعلانات أولا بأمر `/set_announce`", ephemeral=True)
        return
    days = {"daily": 1, "weekly": 7, "monthly": 30}[kind.value]
    today = bot.today()
    rows = await bot.db.top_groups_between(
        interaction.guild_id, today - timedelta(days=days - 1), today, limit=3
    )
    if not rows or rows[0][3] <= 0:
        await interaction.response.send_message("ما فيه نقاط كافية للتجربة بعد.", ephemeral=True)
        return
    await channel.send(build_announcement(kind.value, rows), allowed_mentions=discord.AllowedMentions.none())
    await interaction.response.send_message(f"تم إرسال التجربة في {channel.mention} ✅", ephemeral=True)


@bot.tree.command(name="exclude_channel", description="استثناء روم (شات أو فويس) من حساب النقاط، وكرره للإلغاء")
@app_commands.describe(channel="الروم")
@admin_only
async def exclude_channel(interaction: discord.Interaction, channel: discord.abc.GuildChannel):
    now_ignored = await bot.db.toggle_ignored(interaction.guild_id, channel.id)
    bot.ignored_cache.pop(interaction.guild_id, None)
    await interaction.response.send_message(
        f"{channel.mention} " + ("صار مستثنى من النقاط ✅" if now_ignored else "رجع يحسب النقاط ✅"),
        ephemeral=True,
    )


@bot.tree.command(name="set_board", description="تحديد روم لوحة التوب الثابتة (تتحدث تلقائياً)")
@app_commands.describe(channel="الروم", period="الفترة اللي تظهر باللوحة")
@app_commands.choices(period=PERIOD_CHOICES)
@admin_only
async def set_board(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    period: app_commands.Choice[str] = None,
):
    await interaction.response.defer(ephemeral=True)
    period_value = period.value if period else DEFAULT_PERIOD
    await bot.db.set_board(interaction.guild_id, channel.id, period_value)
    await bot.update_board(interaction.guild, channel.id, None, period_value)
    await interaction.followup.send(
        f"تم تفعيل اللوحة في {channel.mention} وتتحدث كل {BOARD_REFRESH_MINUTES} دقايق ✅",
        ephemeral=True,
    )


@bot.tree.command(name="top_groups", description="عرض صورة توب القروبات")
@app_commands.describe(period="الفترة")
@app_commands.choices(period=PERIOD_CHOICES)
@app_commands.guild_only()
async def top_groups(interaction: discord.Interaction, period: app_commands.Choice[str] = None):
    await interaction.response.defer()
    buf = await bot.build_image(interaction.guild, period.value if period else DEFAULT_PERIOD)
    if buf is None:
        await interaction.followup.send("ما فيه قروبات مسجلة بعد.")
        return
    await interaction.followup.send(file=discord.File(buf, "top_groups.png"), view=PeriodView())


@bot.tree.command(name="top_members", description="توب الأعضاء (كل السيرفر أو داخل قروب)")
@app_commands.describe(period="الفترة", group="رول قروب معين (اختياري)")
@app_commands.choices(period=PERIOD_CHOICES)
@app_commands.guild_only()
async def top_members(
    interaction: discord.Interaction,
    period: app_commands.Choice[str] = None,
    group: Optional[discord.Role] = None,
):
    await interaction.response.defer()
    await bot.db.flush()
    period_value = period.value if period else DEFAULT_PERIOD
    rows = await bot.db.top_members(
        interaction.guild_id, period_value, bot.today(), group.id if group else None
    )
    if not rows:
        await interaction.followup.send("ما فيه نشاط مسجل لهذي الفترة.")
        return
    lines = []
    for i, (user_id, pts, msgs, secs) in enumerate(rows):
        rank = MEDALS[i] if i < 3 else f"`{i + 1}`"
        lines.append(f"{rank} <@{user_id}> — **{int(pts):,}** نقطة • {int(msgs):,} رسالة • {fmt_time(int(secs))}")
    title = f"{PERIOD_TITLES[period_value]} — الأعضاء" + (f" ({group.name})" if group else "")
    embed = discord.Embed(title=title, description="\n".join(lines), color=0x7B61FF)
    await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="my_stats", description="إحصائياتك أو إحصائيات عضو")
@app_commands.describe(member="العضو (اختياري)")
@app_commands.guild_only()
async def my_stats(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    await bot.db.flush()
    groups = await bot.get_groups(interaction.guild_id)
    group_id = bot.pick_group(member, groups)
    embed = discord.Embed(
        title=f"إحصائيات {member.display_name}",
        description=f"القروب: <@&{group_id}>" if group_id else "ما عنده قروب مسجل.",
        color=0x7B61FF,
    )
    for key, label in PERIODS.items():
        pts, msgs, secs = await bot.db.user_stats(interaction.guild_id, member.id, key, bot.today())
        embed.add_field(
            name=label,
            value=f"⭐ {int(pts):,} نقطة\n💬 {int(msgs):,} رسالة\n🎙️ {fmt_time(int(secs))}",
            inline=True,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="info", description="كيف تنحسب النقاط")
@app_commands.guild_only()
async def info(interaction: discord.Interaction):
    embed = discord.Embed(
        title="كيف يحسب بوت التوب؟",
        description=(
            f" 💬 ** الكتابة :** {CHAT_POINTS} نقطة لكل رسالة (حد أدنى {MIN_CHAT_LENGTH} حروف ، "
            f" ورسالة وحدة كل {CHAT_COOLDOWN} ثانية ، والمكرر ما ينحسب يزاحف )\n"
            f"🎙️ ** الفويس : ** {VOICE_POINTS_PER_2MINUTE} لكل دقيقتين نقطة ( ** لازم تكون مع شخص ثاني بالروم عشان يحسب لك يذات الوزن الكثير **  "
            f"على الأقل ، وما تكون مسوي ديفن) \n"
            "🏆 ** نقاط القروب** : ** مجموع نقاط كل أعضائه** \n"
            " النقاط الكلية ما تتصفر أبدا ، واليومي والأسبوعي (** آخر 7 أيام **) والشهري (** آخر 30 يوم ** ) \n"
            "📣 **تنزل إعلانات تلقائية بأفضل قروب يومي وأسبوعي وشهري** \n\n"
            "  اكتب ** توب قروب ** في أي روم وتشوف الصورة لتوب القروبات يالحشري
            " ** Bot Developer : Eleven ** "
        ),
        color=0x7B61FF,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="debug_slots", description="رسم إطارات المربعات للتأكد من الإحداثيات")
@admin_only
async def debug_slots_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    buf = await asyncio.to_thread(debug_slots)
    await interaction.followup.send(file=discord.File(buf, "debug.png"), ephemeral=True)


@bot.tree.command(name="backup", description="تحميل نسخة احتياطية من قاعدة البيانات")
@admin_only
async def backup(interaction: discord.Interaction):
    await bot.db.flush()
    await interaction.response.send_message(file=discord.File(DB_PATH), ephemeral=True)


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "هذا الأمر للإدارة فقط."
    else:
        log.exception("command error", exc_info=error)
        msg = "صار خطأ ، حاول مرة ثانية."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("ضع التوكن في ملف .env بهذا الشكل: TOKEN=xxxxx")
    bot.run(TOKEN)
