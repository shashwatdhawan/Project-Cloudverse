import asyncio
import html
import io
import math
import os
import json
import re
import sqlite3
import time
import aiohttp
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
import uvicorn
from discord.ext import commands
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except Exception:
    Image = None
    ImageDraw = None
    ImageFont = None
    ImageFilter = None


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("TOKEN")

TRIGGER_FILE = "triggers.json"

if os.path.exists(TRIGGER_FILE):
    with open(TRIGGER_FILE, "r") as f:
        triggers = json.load(f)
else:
    triggers = {}

def save_triggers():
    with open(TRIGGER_FILE, "w") as f:
        json.dump(triggers, f, indent=4)

# You can hardcode normal Discord IDs safely.
# Keep TOKEN and WEBSITE_TICKET_SECRET in Railway Variables.
GUILD_ID = 1502695100902277171
STAFF_ROLE_ID = 1502694829824540838
PANEL_CHANNEL_ID = 1502695101716238502
PURCHASE_LOG_CHANNEL_ID = 1533123496971079680
TRANSCRIPT_CATEGORY_ID = 1533160992224186468
DISCORD_INVITE_URL = "https://discord.gg/ZduZMYP6Cc"

GENERAL_SUPPORT_CATEGORY_ID = 1502695104551583848
PUNISHMENT_APPEALS_CATEGORY_ID = 1502695104551583849
BUG_REPORTS_CATEGORY_ID = 1502695104551583850
OTHER_TICKETS_CATEGORY_ID = 1502695104551583851
PLAYER_REPORTS_CATEGORY_ID = 1502695104551583852
STAFF_REPORTS_CATEGORY_ID = 1533153255129878588
PURCHASE_TICKETS_CATEGORY_ID = 1533143482267340852

TICKET_CATEGORY_IDS = {
    "general support": GENERAL_SUPPORT_CATEGORY_ID,
    "punishment appeal": PUNISHMENT_APPEALS_CATEGORY_ID,
    "punishment appeals": PUNISHMENT_APPEALS_CATEGORY_ID,
    "bug report": BUG_REPORTS_CATEGORY_ID,
    "bug reports": BUG_REPORTS_CATEGORY_ID,
    "other": OTHER_TICKETS_CATEGORY_ID,
    "other tickets": OTHER_TICKETS_CATEGORY_ID,
    "player report": PLAYER_REPORTS_CATEGORY_ID,
    "player reports": PLAYER_REPORTS_CATEGORY_ID,
    "staff report": STAFF_REPORTS_CATEGORY_ID,
    "staff reports": STAFF_REPORTS_CATEGORY_ID,
    "billing support": PURCHASE_TICKETS_CATEGORY_ID,
    "website purchase": PURCHASE_TICKETS_CATEGORY_ID,
    "website purchases": PURCHASE_TICKETS_CATEGORY_ID,
}

WEBSITE_TICKET_SECRET = os.getenv("WEBSITE_TICKET_SECRET", "")
PORT = int(os.getenv("PORT", "8001"))
LEVEL_DB_PATH = os.getenv("LEVEL_DB_PATH", "cloudverse_levels.sqlite3")
LEVEL_UP_CHANNEL_ID = 1502694830437040257
XP_PER_MESSAGE = 1
XP_COOLDOWN_SECONDS = 60
VOICE_XP_PER_MINUTE = 1
VOICE_TRACK_SECONDS = 60
LEVEL_MILESTONES = {
    0: 0,
    5: 5000,
    10: 11000,
    15: 16000,
    20: 20000,
    30: 22000,
    40: 30000,
}
LEVEL_REWARD_ROLES = {
    5: 1502694829774078061,
    10: 1502694829774078059,
    15: 1502694829774078058,
    20: 1502694829774078056,
    30: 1502694829774078055,
    40: 1502694829774078054,
}

CLOUDVERSE_BANNER_URL = "https://cdn.discordapp.com/banners/1527932373428076655/961db732e8b3829ad84458d863cdab00.png?size=4096"

CLOUDVERSE_THUMBNAIL_URL = "https://cdn.discordapp.com/avatars/1527932373428076655/5bb6065be3301ce20e01dd6cc06a14ae.png?size=1024"


# =========================================================
# BOT + API
# =========================================================

intents = discord.Intents.all()

bot = commands.Bot(
    command_prefix=",",
    intents=intents,
    help_command=None,
)

trigger = bot.create_group(
    "trigger",
    "Manage message triggers"
)

app = FastAPI(title="Cloudverse Bot Internal API")
ticket_claims: dict[int, dict[str, Any]] = {}
level_db: sqlite3.Connection | None = None
level_db_lock = asyncio.Lock()
level_cache: dict[int, dict[str, int]] = {}
voice_sessions: dict[int, dict[str, int]] = {}
voice_xp_task: asyncio.Task | None = None


# =========================================================
# HELPERS
# =========================================================

def clean_channel_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9-]", "-", value.lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned[:80] or "ticket"


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.id == STAFF_ROLE_ID for role in member.roles)


def get_staff_role(guild: discord.Guild) -> discord.Role | None:
    return guild.get_role(STAFF_ROLE_ID)


def get_ticket_category(guild: discord.Guild, reason: str) -> discord.CategoryChannel | None:
    category_id = TICKET_CATEGORY_IDS.get(reason.strip().lower(), OTHER_TICKETS_CATEGORY_ID)
    category = guild.get_channel(category_id)
    return category if isinstance(category, discord.CategoryChannel) else None


def build_ticket_overwrites(guild: discord.Guild, user: discord.Member) -> dict:
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            attach_files=True,
            embed_links=True,
            read_message_history=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
            read_message_history=True,
        ),
    }

    staff_role = get_staff_role(guild)
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            attach_files=True,
            embed_links=True,
            read_message_history=True,
        )

    return overwrites


async def get_member(guild: discord.Guild, discord_id: str) -> discord.Member:
    try:
        member = guild.get_member(int(discord_id))
        if member:
            return member
        return await guild.fetch_member(int(discord_id))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Discord member not found in the server.") from exc


async def find_member_by_text(guild: discord.Guild, value: str) -> discord.Member | None:
    cleaned = value.strip()
    match = re.search(r"\d{15,25}", cleaned)
    if match:
        member_id = int(match.group(0))
        try:
            return guild.get_member(member_id) or await guild.fetch_member(member_id)
        except Exception:
            return None

    lowered = cleaned.lower().lstrip("@")
    for member in guild.members:
        names = {
            member.name.lower(),
            member.display_name.lower(),
            str(member).lower(),
        }
        if lowered in names:
            return member
    return None


def product_lines(products: list[dict[str, Any]]) -> str:
    if not products:
        return "No products found."

    lines = []
    for item in products:
        name = item.get("name", "Unknown Product")
        quantity = item.get("quantity", 1)
        price = item.get("price") or item.get("unitPrice") or 0
        lines.append(f"- {name} x{quantity} | Rs. {price}")

    return "\n".join(lines)


def now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def set_embed_field(embed: discord.Embed, name: str, value: str, inline: bool = True) -> None:
    for index, field in enumerate(embed.fields):
        if field.name.lower() == name.lower():
            embed.set_field_at(index, name=name, value=value, inline=inline)
            return
    embed.add_field(name=name, value=value, inline=inline)


async def edit_ticket_status_message(
    interaction: discord.Interaction,
    status: str,
    extra_fields: dict[str, str] | None = None,
    message: discord.Message | None = None,
    view: discord.ui.View | None = None,
) -> None:
    message = message or interaction.message
    if not message or not message.embeds:
        return

    embed = message.embeds[0]
    set_embed_field(embed, "Status", status, inline=True)
    if extra_fields:
        for name, value in extra_fields.items():
            set_embed_field(embed, name, value, inline=True)

    if view is None:
        await message.edit(embed=embed)
    else:
        await message.edit(embed=embed, view=view)


async def send_purchase_log(embed: discord.Embed) -> None:
    if not PURCHASE_LOG_CHANNEL_ID:
        return

    channel = bot.get_channel(PURCHASE_LOG_CHANNEL_ID)
    if channel:
        await channel.send(embed=embed)


def get_target_guild() -> discord.Guild | None:
    if GUILD_ID:
        return bot.get_guild(GUILD_ID)
    return bot.guilds[0] if bot.guilds else None


def jump_url(guild_id: int, channel_id: int, message_id: int | None = None) -> str:
    if message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


def user_avatar_url(user: discord.abc.User) -> str:
    avatar = getattr(user, "display_avatar", None) or getattr(user, "avatar", None)
    return avatar.url if avatar else CLOUDVERSE_THUMBNAIL_URL


def format_member_value(guild: discord.Guild, user_id: int | str | None, fallback: str = "Unknown") -> str:
    if not user_id:
        return fallback
    try:
        member_id = int(user_id)
    except (TypeError, ValueError):
        return fallback
    member = guild.get_member(member_id)
    return member.mention if member else f"<@{member_id}>"


# =========================================================
# LEVEL SYSTEM
# =========================================================

def xp_for_level(level: int) -> int:
    level = max(0, int(level))
    if level in LEVEL_MILESTONES:
        return LEVEL_MILESTONES[level]

    points = sorted(LEVEL_MILESTONES.items())
    for (level_a, xp_a), (level_b, xp_b) in zip(points, points[1:]):
        if level_a <= level <= level_b:
            progress = (level - level_a) / (level_b - level_a)
            eased = progress * progress * (3 - 2 * progress)
            return round(xp_a + (xp_b - xp_a) * eased)

    return LEVEL_MILESTONES[40] + ((level - 40) * 2500)


def level_from_xp(xp: int) -> int:
    xp = max(0, int(xp))
    level = 0
    while xp_for_level(level + 1) <= xp:
        level += 1
        if level > 10000:
            break
    return level


def progress_bar(current: int, needed: int, width: int = 18) -> str:
    if needed <= 0:
        filled = width
    else:
        filled = min(width, max(0, round((current / needed) * width)))
    return "█" * filled + "░" * (width - filled)


def current_rank_name(level: int) -> str:
    unlocked = [milestone for milestone in LEVEL_REWARD_ROLES if level >= milestone]
    if not unlocked:
        return "Member"
    return f"Level {max(unlocked)} Reward"


def next_reward_role_id(level: int) -> int | None:
    for milestone in sorted(LEVEL_REWARD_ROLES):
        if level < milestone:
            return LEVEL_REWARD_ROLES[milestone]
    return None


def blank_level_record(user_id: int) -> dict[str, int]:
    return {
        "user_id": int(user_id),
        "xp": 0,
        "level": 0,
        "messages": 0,
        "last_xp_ts": 0,
        "voice_seconds": 0,
        "voice_xp": 0,
        "text_xp": 0,
    }


async def init_level_database() -> None:
    global level_db
    level_db = sqlite3.connect(LEVEL_DB_PATH, check_same_thread=False)
    level_db.row_factory = sqlite3.Row
    async with level_db_lock:
        level_db.execute(
            """
            CREATE TABLE IF NOT EXISTS levels (
                user_id INTEGER PRIMARY KEY,
                xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 0,
                messages INTEGER NOT NULL DEFAULT 0,
                last_xp_ts INTEGER NOT NULL DEFAULT 0,
                voice_seconds INTEGER NOT NULL DEFAULT 0,
                voice_xp INTEGER NOT NULL DEFAULT 0,
                text_xp INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in level_db.execute("PRAGMA table_info(levels)").fetchall()
        }
        migrations = {
            "voice_seconds": "ALTER TABLE levels ADD COLUMN voice_seconds INTEGER NOT NULL DEFAULT 0",
            "voice_xp": "ALTER TABLE levels ADD COLUMN voice_xp INTEGER NOT NULL DEFAULT 0",
            "text_xp": "ALTER TABLE levels ADD COLUMN text_xp INTEGER NOT NULL DEFAULT 0",
        }
        for column, sql in migrations.items():
            if column not in existing_columns:
                level_db.execute(sql)
        level_db.commit()


async def get_level_record(user_id: int) -> dict[str, int]:
    if user_id in level_cache:
        return level_cache[user_id]
    if level_db is None:
        await init_level_database()
    async with level_db_lock:
        row = level_db.execute("SELECT * FROM levels WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            record = blank_level_record(user_id)
            level_db.execute(
                "INSERT OR IGNORE INTO levels (user_id, xp, level, messages, last_xp_ts, voice_seconds, voice_xp, text_xp) VALUES (?, 0, 0, 0, 0, 0, 0, 0)",
                (user_id,),
            )
            level_db.commit()
        else:
            record = {
                "user_id": int(row["user_id"]),
                "xp": int(row["xp"]),
                "level": int(row["level"]),
                "messages": int(row["messages"]),
                "last_xp_ts": int(row["last_xp_ts"]),
                "voice_seconds": int(row["voice_seconds"]),
                "voice_xp": int(row["voice_xp"]),
                "text_xp": int(row["text_xp"]),
            }
    level_cache[user_id] = record
    return record


async def save_level_record(record: dict[str, int]) -> None:
    if level_db is None:
        await init_level_database()
    level_cache[int(record["user_id"])] = record
    async with level_db_lock:
        level_db.execute(
            """
            INSERT INTO levels (user_id, xp, level, messages, last_xp_ts, voice_seconds, voice_xp, text_xp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                xp = excluded.xp,
                level = excluded.level,
                messages = excluded.messages,
                last_xp_ts = excluded.last_xp_ts,
                voice_seconds = excluded.voice_seconds,
                voice_xp = excluded.voice_xp,
                text_xp = excluded.text_xp
            """,
            (
                int(record["user_id"]),
                int(record["xp"]),
                int(record["level"]),
                int(record["messages"]),
                int(record["last_xp_ts"]),
                int(record.get("voice_seconds", 0)),
                int(record.get("voice_xp", 0)),
                int(record.get("text_xp", 0)),
            ),
        )
        level_db.commit()


async def set_user_xp(member: discord.Member, xp: int, preserve_cooldown: bool = True) -> dict[str, int]:
    record = await get_level_record(member.id)
    record["xp"] = max(0, int(xp))
    record["level"] = level_from_xp(record["xp"])
    if not preserve_cooldown:
        record["last_xp_ts"] = 0
    await save_level_record(record)
    await apply_level_roles(member, record["level"])
    return record


async def apply_level_roles(member: discord.Member, level: int) -> None:
    roles_to_add = []
    for milestone, role_id in LEVEL_REWARD_ROLES.items():
        if level >= milestone:
            role = member.guild.get_role(role_id)
            if role and role not in member.roles:
                roles_to_add.append(role)
    if roles_to_add:
        try:
            await member.add_roles(*roles_to_add, reason=f"Cloudverse level reward: Level {level}")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass


def load_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    candidates = [
        "arialbd.ttf" if bold else "arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def rounded_avatar(avatar: Any, size: int) -> Any:
    avatar = avatar.convert("RGBA").resize((size, size))
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(avatar, (0, 0), mask)
    return out


def make_cloudverse_background(width: int, height: int) -> Any:
    image = Image.new("RGB", (width, height), "#07111f")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = round(7 + 20 * ratio)
        g = round(17 + 22 * ratio)
        b = round(31 + 70 * ratio)
        draw.line((0, y, width, y), fill=(r, g, b))
    for i in range(18):
        x = (i * 97) % width
        y = 20 + ((i * 53) % max(1, height - 80))
        color = (45, 160, 255, 38) if i % 2 else (143, 92, 255, 32)
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.ellipse((x - 90, y - 34, x + 160, y + 70), fill=color)
        overlay = overlay.filter(ImageFilter.GaussianBlur(28))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    return image.convert("RGBA")


async def avatar_image(member: discord.Member, size: int = 256) -> Any:
    try:
        data = await member.display_avatar.with_size(size).read()
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        fallback = Image.new("RGBA", (size, size), (40, 120, 220, 255))
        draw = ImageDraw.Draw(fallback)
        draw.text((size // 2 - 28, size // 2 - 18), "CV", font=load_font(42, True), fill="white")
        return fallback


async def generate_level_card(member: discord.Member, record: dict[str, int], level_up: tuple[int, int] | None = None) -> discord.File | None:
    if Image is None or ImageDraw is None:
        return None

    width, height = (1000, 360) if not level_up else (1100, 420)
    bg_path = "background.png"
    if os.path.exists(bg_path):
        try:
            base = Image.open(bg_path).convert("RGBA").resize((width, height))
        except Exception:
            base = make_cloudverse_background(width, height)
    else:
        base = make_cloudverse_background(width, height)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 70))
    base = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(base)
    draw.rounded_rectangle((28, 28, width - 28, height - 28), radius=28, fill=(8, 14, 30, 190), outline=(70, 180, 255, 170), width=3)

    avatar = rounded_avatar(await avatar_image(member), 190 if not level_up else 210)
    avatar_x = 70
    avatar_y = (height - avatar.height) // 2
    draw.ellipse((avatar_x - 8, avatar_y - 8, avatar_x + avatar.width + 8, avatar_y + avatar.height + 8), fill=(45, 190, 255, 80), outline=(120, 220, 255, 220), width=4)
    base.alpha_composite(avatar, (avatar_x, avatar_y))

    title_font = load_font(62 if level_up else 46, True)
    big_font = load_font(52, True)
    mid_font = load_font(34, True)
    small_font = load_font(25, False)

    left = avatar_x + avatar.width + 50
    username = member.display_name[:26]
    if level_up:
        old_level, new_level = level_up
        draw.text((left, 70), "LEVEL UP", font=title_font, fill=(135, 220, 255))
        draw.text((left, 148), username, font=mid_font, fill=(255, 255, 255))
        draw.text((left, 204), f"Level {old_level}  →  Level {new_level}", font=big_font, fill=(190, 150, 255))
    else:
        draw.text((left, 72), username, font=title_font, fill=(255, 255, 255))
        draw.text((left, 138), f"Level {record['level']} • {current_rank_name(record['level'])}", font=mid_font, fill=(135, 220, 255))

    current_level_xp = xp_for_level(record["level"])
    next_level_xp = xp_for_level(record["level"] + 1)
    progress_current = max(0, record["xp"] - current_level_xp)
    progress_needed = max(1, next_level_xp - current_level_xp)
    percent = min(1, progress_current / progress_needed)

    bar_x, bar_y = left, height - 105
    bar_w, bar_h = width - left - 80, 36
    draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=18, fill=(16, 28, 50, 255))
    draw.rounded_rectangle((bar_x, bar_y, bar_x + int(bar_w * percent), bar_y + bar_h), radius=18, fill=(40, 180, 255, 255))
    draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=18, outline=(140, 220, 255, 180), width=2)
    draw.text((bar_x, bar_y - 36), f"XP {record['xp']:,} / {next_level_xp:,}", font=small_font, fill=(225, 240, 255))
    draw.text((width - 170, 44), "CLOUDVERSE", font=small_font, fill=(135, 220, 255))

    buffer = io.BytesIO()
    base.save(buffer, format="PNG")
    buffer.seek(0)
    filename = "cloudverse-level-up.png" if level_up else "cloudverse-rank.png"
    return discord.File(buffer, filename=filename)


async def generate_stats_card(member: discord.Member, record: dict[str, int]) -> discord.File | None:
    if Image is None or ImageDraw is None:
        return None

    width, height = 1000, 520
    base = make_cloudverse_background(width, height)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 85))
    base = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(base)

    draw.rounded_rectangle((28, 28, width - 28, height - 28), radius=30, fill=(7, 13, 28, 205), outline=(70, 180, 255, 180), width=3)
    draw.rounded_rectangle((54, 54, 330, height - 54), radius=26, fill=(12, 22, 44, 230), outline=(120, 90, 255, 150), width=2)

    avatar = rounded_avatar(await avatar_image(member), 190)
    base.alpha_composite(avatar, (97, 86))

    title_font = load_font(46, True)
    mid_font = load_font(31, True)
    small_font = load_font(23, False)
    label_font = load_font(20, True)

    draw.text((82, 300), member.display_name[:18], font=mid_font, fill=(255, 255, 255))
    draw.text((82, 340), f"Level {record['level']}", font=mid_font, fill=(125, 220, 255))
    draw.text((82, 382), current_rank_name(record["level"]), font=small_font, fill=(196, 181, 253))

    left = 380
    draw.text((left, 70), "Cloudverse Player Stats", font=title_font, fill=(255, 255, 255))
    draw.text((left, 122), "Text activity, voice activity, XP progress and rewards", font=small_font, fill=(190, 210, 255))

    current_level_xp = xp_for_level(record["level"])
    next_level_xp = xp_for_level(record["level"] + 1)
    current_progress = max(0, record["xp"] - current_level_xp)
    needed_progress = max(1, next_level_xp - current_level_xp)
    percent = min(1, current_progress / needed_progress)

    bar_x, bar_y = left, 172
    bar_w, bar_h = 540, 34
    draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=17, fill=(16, 28, 50, 255))
    draw.rounded_rectangle((bar_x, bar_y, bar_x + int(bar_w * percent), bar_y + bar_h), radius=17, fill=(40, 180, 255, 255))
    draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=17, outline=(140, 220, 255, 180), width=2)
    draw.text((bar_x, bar_y - 31), f"{record['xp']:,} XP / {next_level_xp:,} XP", font=small_font, fill=(225, 240, 255))

    stats = [
        ("Messages", f"{record.get('messages', 0):,}"),
        ("Voice Time", format_duration(record.get("voice_seconds", 0))),
        ("Text XP", f"{record.get('text_xp', 0):,}"),
        ("Voice XP", f"{record.get('voice_xp', 0):,}"),
        ("Total XP", f"{record.get('xp', 0):,}"),
        ("Next Reward", f"Level {min([m for m in LEVEL_REWARD_ROLES if m > record['level']], default=40)}" if next_reward_role_id(record["level"]) else "Unlocked"),
    ]
    card_w, card_h = 255, 86
    for index, (label, value) in enumerate(stats):
        col = index % 2
        row = index // 2
        x = left + col * 282
        y = 242 + row * 104
        draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=18, fill=(13, 25, 50, 230), outline=(75, 130, 255, 90), width=2)
        draw.text((x + 22, y + 16), label.upper(), font=label_font, fill=(135, 220, 255))
        draw.text((x + 22, y + 44), value, font=mid_font, fill=(255, 255, 255))

    draw.text((width - 178, 42), "CLOUDVERSE", font=small_font, fill=(135, 220, 255))

    buffer = io.BytesIO()
    base.save(buffer, format="PNG")
    buffer.seek(0)
    return discord.File(buffer, filename="cloudverse-stats.png")


async def send_level_up_message(member: discord.Member, old_level: int, new_level: int, record: dict[str, int]) -> None:
    await apply_level_roles(member, new_level)
    channel = member.guild.get_channel(LEVEL_UP_CHANNEL_ID)
    if not channel:
        return

    embed = discord.Embed(
        title=f"Congratulations {member.display_name}!",
        description=(
            f"☁️ Congratulations {member.mention}!\n\n"
            f"You reached **Level {new_level}**!\n\n"
            "Thank you for being an active member of Cloudverse.\n"
            "Keep chatting to unlock more rewards!"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name="Cloudverse Level System")
    embed.set_thumbnail(url=user_avatar_url(member))
    embed.add_field(name="Previous Level", value=str(old_level), inline=True)
    embed.add_field(name="New Level", value=str(new_level), inline=True)
    embed.add_field(name="XP Progress", value=f"{record['xp']:,} XP", inline=True)
    file = await generate_level_card(member, record, level_up=(old_level, new_level))
    if file:
        embed.set_image(url="attachment://cloudverse-level-up.png")
        await channel.send(content=member.mention, embed=embed, file=file)
    else:
        await channel.send(content=member.mention, embed=embed)


async def handle_level_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    if message.content.startswith(tuple(bot.command_prefix if isinstance(bot.command_prefix, (list, tuple)) else [bot.command_prefix])):
        return

    record = await get_level_record(message.author.id)
    now = int(time.time())
    record["messages"] += 1
    old_level = record["level"]

    if now - record["last_xp_ts"] >= XP_COOLDOWN_SECONDS:
        record["xp"] += XP_PER_MESSAGE
        record["text_xp"] = record.get("text_xp", 0) + XP_PER_MESSAGE
        record["last_xp_ts"] = now
        record["level"] = level_from_xp(record["xp"])

    await save_level_record(record)

    if record["level"] > old_level:
        await send_level_up_message(message.author, old_level, record["level"], record)


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def voice_session_allowed(member: discord.Member, state: discord.VoiceState | None = None) -> bool:
    if member.bot:
        return False
    voice_state = state or member.voice
    return bool(voice_state and voice_state.channel)


async def start_voice_session(member: discord.Member) -> None:
    if not voice_session_allowed(member):
        return
    now = int(time.time())
    voice_sessions[member.id] = {
        "guild_id": member.guild.id,
        "channel_id": member.voice.channel.id,
        "started_at": now,
        "last_tick": now,
    }


async def flush_voice_session(member: discord.Member, final: bool = False) -> None:
    session = voice_sessions.get(member.id)
    if not session:
        return

    now = int(time.time())
    elapsed = max(0, now - int(session.get("last_tick", now)))
    if elapsed < VOICE_TRACK_SECONDS and not final:
        return

    awardable_seconds = elapsed if final else (elapsed // VOICE_TRACK_SECONDS) * VOICE_TRACK_SECONDS
    if awardable_seconds <= 0:
        return

    record = await get_level_record(member.id)
    old_level = record["level"]
    voice_xp = (awardable_seconds // 60) * VOICE_XP_PER_MINUTE
    record["voice_seconds"] = record.get("voice_seconds", 0) + awardable_seconds
    record["voice_xp"] = record.get("voice_xp", 0) + voice_xp
    record["xp"] += voice_xp
    record["level"] = level_from_xp(record["xp"])
    session["last_tick"] = int(session.get("last_tick", now)) + awardable_seconds
    await save_level_record(record)

    if record["level"] > old_level:
        await send_level_up_message(member, old_level, record["level"], record)

    if final:
        voice_sessions.pop(member.id, None)


async def voice_xp_loop() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(VOICE_TRACK_SECONDS)
        for guild in list(bot.guilds):
            for voice_channel in guild.voice_channels:
                for member in voice_channel.members:
                    if member.bot:
                        continue
                    if member.id not in voice_sessions:
                        await start_voice_session(member)
                    await flush_voice_session(member, final=False)


async def restore_active_voice_sessions() -> None:
    for guild in bot.guilds:
        for voice_channel in guild.voice_channels:
            for member in voice_channel.members:
                if not member.bot and member.id not in voice_sessions:
                    await start_voice_session(member)


# =========================================================
# WEBSITE ORDER MODEL
# =========================================================

class WebsiteOrder(BaseModel):
    order_id: str = Field(min_length=1)
    discord_id: str = Field(min_length=5)
    discord_username: str | None = None
    minecraft_username: str | None = None
    minecraft_type: str | None = None
    products: list[dict[str, Any]] = []
    coupon: str | None = None
    discount: int = 0
    subtotal: int = 0
    total: int = 0
    final_total: int | None = None
    created_at: str | None = None
    status: str = "Pending"

    @property
    def amount(self) -> int:
        return self.final_total if self.final_total is not None else self.total


# =========================================================
# TICKET STATE + TRANSCRIPTS
# =========================================================

def default_ticket_state(
    channel: discord.TextChannel,
    user: discord.Member,
    reason: str,
    order_data: WebsiteOrder | None = None,
    form_data: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "creator_id": user.id,
        "creator_name": str(user),
        "ticket_type": reason,
        "claimed_by_id": None,
        "claimed_by_name": None,
        "claimed_at": None,
        "closed_by_id": None,
        "closed_by_name": None,
        "closed_at": None,
        "status": "Pending",
        "order_data": order_data.model_dump() if order_data else None,
        "form_data": form_data or {},
        "original_name": channel.name,
        "opened_at": now_text(),
        "channel_id": channel.id,
    }


def get_state_by_channel(channel_id: int) -> dict[str, Any] | None:
    for state in ticket_claims.values():
        if state.get("channel_id") == channel_id:
            return state
    return None


def get_state(source_message: discord.Message | None, channel: discord.TextChannel | None = None) -> dict[str, Any]:
    if source_message:
        return ticket_claims.setdefault(source_message.id, {})
    if channel:
        state = get_state_by_channel(channel.id)
        if state is not None:
            return state
    return {}


async def find_ticket_creator(channel: discord.TextChannel, state: dict[str, Any]) -> discord.Member | None:
    creator_id = state.get("creator_id")
    if creator_id:
        try:
            return channel.guild.get_member(int(creator_id)) or await channel.guild.fetch_member(int(creator_id))
        except Exception:
            return None

    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Member) and not is_staff(target) and overwrite.view_channel:
            state["creator_id"] = target.id
            state["creator_name"] = str(target)
            return target
    return None


def embed_to_html(embed: discord.Embed) -> str:
    parts = ['<div class="embed">']
    if embed.title:
        parts.append(f'<div class="embed-title">{html.escape(embed.title)}</div>')
    if embed.description:
        parts.append(f'<div class="embed-description">{html.escape(embed.description)}</div>')
    for field in embed.fields:
        parts.append(
            '<div class="embed-field">'
            f'<strong>{html.escape(field.name)}</strong>'
            f'<p>{html.escape(str(field.value))}</p>'
            '</div>'
        )
    image = embed.image.url if embed.image else None
    thumbnail = embed.thumbnail.url if embed.thumbnail else None
    if thumbnail:
        parts.append(f'<a href="{html.escape(thumbnail)}">Thumbnail</a>')
    if image:
        parts.append(f'<a href="{html.escape(image)}">Image</a>')
    parts.append("</div>")
    return "".join(parts)


async def build_transcript_html(channel: discord.TextChannel, state: dict[str, Any]) -> bytes:
    rows = []
    async for message in channel.history(limit=None, oldest_first=True):
        timestamp = message.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        author = html.escape(str(message.author))
        content = html.escape(message.content or "")
        attachments = []
        for attachment in message.attachments:
            url = html.escape(attachment.url)
            filename = html.escape(attachment.filename)
            if (attachment.content_type or "").startswith("image/"):
                attachments.append(f'<a href="{url}">{filename}</a><br><img src="{url}" alt="{filename}">')
            else:
                attachments.append(f'<a href="{url}">{filename}</a>')
        embeds = "".join(embed_to_html(embed) for embed in message.embeds)
        rows.append(
            '<article class="message">'
            f'<div class="meta"><strong>{author}</strong><span>{timestamp}</span></div>'
            f'<div class="content">{content}</div>'
            f'{"".join(attachments)}'
            f'{embeds}'
            '</article>'
        )

    title = html.escape(channel.name)
    ticket_type = html.escape(str(state.get("ticket_type") or "Unknown"))
    created_by = html.escape(str(state.get("creator_name") or state.get("creator_id") or "Unknown"))
    claimed_by = html.escape(str(state.get("claimed_by_name") or state.get("claimed_by_id") or "Unclaimed"))
    closed_by = html.escape(str(state.get("closed_by_name") or state.get("closed_by_id") or "Unknown"))
    reason = html.escape(str(state.get("close_reason") or "No reason provided."))
    opened_at = html.escape(str(state.get("opened_at") or "Unknown"))
    closed_at = html.escape(str(state.get("closed_at") or now_text()))

    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cloudverse Transcript - {title}</title>
  <style>
    body {{ margin: 0; background: #0b1020; color: #e8eefc; font-family: Arial, sans-serif; }}
    header {{ padding: 28px; background: linear-gradient(135deg, #101935, #2b1458); border-bottom: 1px solid #5060ff55; }}
    h1 {{ margin: 0 0 10px; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; color: #c7d2ff; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 24px; }}
    .message {{ background: #11182c; border: 1px solid #27324f; border-radius: 12px; padding: 14px; margin-bottom: 12px; }}
    .meta {{ display: flex; gap: 12px; justify-content: space-between; color: #93c5fd; font-size: 14px; margin-bottom: 8px; }}
    .content {{ white-space: pre-wrap; line-height: 1.45; }}
    img {{ max-width: 420px; max-height: 320px; display: block; margin: 8px 0; border-radius: 8px; }}
    a {{ color: #8b5cf6; }}
    .embed {{ border-left: 4px solid #8b5cf6; background: #151026; padding: 10px; margin-top: 8px; border-radius: 8px; }}
    .embed-title {{ font-weight: bold; color: #c084fc; margin-bottom: 6px; }}
    .embed-field {{ margin-top: 8px; }}
    .embed-field p {{ margin: 4px 0 0; white-space: pre-wrap; }}
  </style>
</head>
<body>
  <header>
    <h1>Cloudverse Ticket Transcript</h1>
    <div class="summary">
      <div><strong>Channel:</strong> {title}</div>
      <div><strong>Ticket Type:</strong> {ticket_type}</div>
      <div><strong>Created By:</strong> {created_by}</div>
      <div><strong>Claimed By:</strong> {claimed_by}</div>
      <div><strong>Closed By:</strong> {closed_by}</div>
      <div><strong>Reason:</strong> {reason}</div>
      <div><strong>Opened At:</strong> {opened_at}</div>
      <div><strong>Closed At:</strong> {closed_at}</div>
    </div>
  </header>
  <main>
    {''.join(rows)}
  </main>
</body>
</html>"""
    return doc.encode("utf-8")


async def create_transcript_channel_and_send(
    ticket_channel: discord.TextChannel,
    state: dict[str, Any],
    transcript_bytes: bytes,
) -> discord.TextChannel | None:
    category = ticket_channel.guild.get_channel(TRANSCRIPT_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        return None

    transcript_name = clean_channel_name(f"transcript-{ticket_channel.name}")
    transcript_channel = await ticket_channel.guild.create_text_channel(
        name=transcript_name,
        category=category,
        reason=f"Transcript for {ticket_channel.name}",
    )

    filename = f"{transcript_name}.html"
    file = discord.File(io.BytesIO(transcript_bytes), filename=filename)
    embed = discord.Embed(
        title="Cloudverse Ticket Transcript",
        description=f"Transcript generated for {ticket_channel.mention}.",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Created By", value=format_member_value(ticket_channel.guild, state.get("creator_id"), str(state.get("creator_name") or "Unknown")), inline=True)
    embed.add_field(name="Claimed By", value=format_member_value(ticket_channel.guild, state.get("claimed_by_id"), "Unclaimed"), inline=True)
    embed.add_field(name="Closed By", value=format_member_value(ticket_channel.guild, state.get("closed_by_id"), str(state.get("closed_by_name") or "Unknown")), inline=True)
    embed.add_field(name="Reason", value=str(state.get("close_reason") or "No reason provided.")[:1024], inline=False)
    embed.add_field(name="Ticket Type", value=str(state.get("ticket_type") or "Unknown"), inline=True)
    embed.add_field(name="Opened At", value=str(state.get("opened_at") or "Unknown"), inline=True)
    embed.add_field(name="Closed At", value=str(state.get("closed_at") or now_text()), inline=True)
    embed.set_thumbnail(url=CLOUDVERSE_THUMBNAIL_URL)
    await transcript_channel.send(embed=embed, file=file)
    return transcript_channel


async def dm_ticket_creator(member: discord.Member | None, transcript_bytes: bytes, filename: str) -> None:
    if not member:
        return
    try:
        await member.send(
            "Your Cloudverse support ticket has been closed.\n\nThank you for contacting Cloudverse.",
            file=discord.File(io.BytesIO(transcript_bytes), filename=filename),
        )
    except Exception:
        pass


def build_purchase_log_embed(
    *,
    title: str,
    color: discord.Color,
    staff: discord.Member,
    channel: discord.TextChannel,
    order_data: dict[str, Any] | None,
    minecraft_ign: str,
    amount: str,
    items: str,
    status_label: str,
    player: discord.Member | None = None,
) -> discord.Embed:
    order_data = order_data or {}
    player_discord = player.mention if player else (
        f"<@{order_data.get('discord_id')}>" if order_data.get("discord_id") else str(order_data.get("discord_username") or "Unknown")
    )
    order_id = str(order_data.get("order_id") or "Manual / Unknown")
    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Player Discord", value=player_discord, inline=True)
    embed.add_field(name="Minecraft IGN", value=minecraft_ign or str(order_data.get("minecraft_username") or "Not linked"), inline=True)
    embed.add_field(name="Amount", value=f"Rs. {amount}".replace("Rs. Rs.", "Rs."), inline=True)
    embed.add_field(name="Items Purchased", value=(items or product_lines(order_data.get("products", [])))[:1024], inline=False)
    embed.add_field(name="Order ID", value=order_id, inline=True)
    embed.add_field(name="Confirmed By", value=staff.mention, inline=True)
    embed.add_field(name="Time", value=now_text(), inline=True)
    embed.add_field(name="Server", value=channel.guild.name, inline=True)
    embed.add_field(name="Status", value=status_label, inline=True)
    embed.add_field(name="Ticket Link", value=f"[Open Ticket]({jump_url(channel.guild.id, channel.id)})", inline=False)
    embed.set_thumbnail(url=user_avatar_url(player or staff))
    return embed


# =========================================================
# TICKET CREATION
# =========================================================

async def create_ticket_channel(
    guild: discord.Guild,
    user: discord.Member,
    reason: str,
    order_data: WebsiteOrder | None = None,
    form_data: dict[str, str] | None = None,
) -> discord.TextChannel:
    category = get_ticket_category(guild, reason)

    if order_data:
        channel_name = clean_channel_name(f"order-{order_data.order_id}-{user.name}")
    else:
        channel_name = clean_channel_name(f"ticket-{reason}-{user.name}")

    channel = await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=build_ticket_overwrites(guild, user),
        reason=f"Cloudverse ticket created for {user}",
    )

    staff_role = get_staff_role(guild)
    staff_ping = staff_role.mention if staff_role else "@staff"

    if order_data:
        embed = discord.Embed(
            title="Cloudverse Purchase Ticket",
            description="A website order has created this purchase ticket.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Order ID", value=order_data.order_id, inline=True)
        embed.add_field(name="Customer", value=user.mention, inline=True)
        embed.add_field(name="Discord Username", value=order_data.discord_username or user.name, inline=True)
        embed.add_field(name="Minecraft IGN", value=order_data.minecraft_username or "Not linked", inline=True)
        embed.add_field(name="Minecraft Type", value=order_data.minecraft_type or "Unknown", inline=True)
        embed.add_field(name="Status", value="Pending", inline=True)
        embed.add_field(name="Products", value=product_lines(order_data.products), inline=False)
        embed.add_field(name="Coupon", value=order_data.coupon or "None", inline=True)
        embed.add_field(name="Discount", value=f"Rs. {order_data.discount}", inline=True)
        embed.add_field(name="Total", value=f"Rs. {order_data.amount}", inline=True)
        embed.set_footer(text="Staff: claim the ticket, then use Staff Tools.")
    else:
        embed = discord.Embed(
            title="Cloudverse Support Ticket",
            description=f"Welcome {user.mention}. Staff will help you soon.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Status", value="Pending", inline=True)
        if form_data:
            for name, value in form_data.items():
                embed.add_field(name=name, value=value or "Not provided", inline=False)
        embed.set_footer(text="Staff: claim the ticket, then use Staff Tools.")

    embed.set_thumbnail(url=CLOUDVERSE_THUMBNAIL_URL)
    embed.set_image(url=CLOUDVERSE_BANNER_URL)

    message = await channel.send(
        content=f"{user.mention} {staff_ping}",
        embed=embed,
        view=TicketStaffView(order_data=order_data.model_dump() if order_data else None),
    )

    state = default_ticket_state(channel, user, reason, order_data, form_data)
    state["message_id"] = message.id
    ticket_claims[message.id] = state

    return channel


# =========================================================
# NORMAL TICKET PANEL
# =========================================================

class TicketFormModal(discord.ui.Modal):
    def __init__(self, reason: str):
        super().__init__(title=reason)
        self.reason = reason

        self.ign = None
        self.target = None
        self.issue = None

        if reason != "Other":
            self.ign = discord.ui.InputText(
                label="What is your In-Game-Name?",
                placeholder="Your Minecraft IGN",
                max_length=32,
            )
            self.add_item(self.ign)

        if reason == "General Support":
            self.issue = discord.ui.InputText(
                label="What do you need help with in-game?",
                placeholder="Describe your issue clearly.",
                style=discord.InputTextStyle.long,
                max_length=1000,
            )
        elif reason == "Billing Support":
            self.issue = discord.ui.InputText(
                label="What rank or item did you not receive?",
                placeholder="Tell us what you bought and what went wrong.",
                style=discord.InputTextStyle.long,
                max_length=1000,
            )
        elif reason == "Punishment Appeal":
            self.issue = discord.ui.InputText(
                label="Why should we consider your appeal?",
                placeholder="Explain your appeal clearly.",
                style=discord.InputTextStyle.long,
                max_length=1000,
            )
        elif reason == "Player Reports":
            self.target = discord.ui.InputText(
                label="Whom do you want to report? (IGN)",
                placeholder="Player IGN",
                max_length=32,
            )
            self.add_item(self.target)
            self.issue = discord.ui.InputText(
                label="Why do you want to report that player?",
                placeholder="Describe your issue clearly.",
                style=discord.InputTextStyle.long,
                max_length=1000,
            )
        elif reason == "Bug Report":
            self.issue = discord.ui.InputText(
                label="Describe the bug and where it occurred.",
                placeholder="Describe your issue clearly.",
                style=discord.InputTextStyle.long,
                max_length=1000,
            )
        elif reason == "Staff Report":
            self.issue = discord.ui.InputText(
                label="Mention staff and reason for complaint.",
                placeholder="Describe your issue clearly.",
                style=discord.InputTextStyle.long,
                max_length=1000,
            )
        else:
            self.issue = discord.ui.InputText(
                label="Briefly explain your issue.",
                placeholder="Describe your issue clearly.",
                style=discord.InputTextStyle.long,
                max_length=1000,
            )

        self.add_item(self.issue)

    async def callback(self, interaction: discord.Interaction):
        form_data = {}
        if self.ign:
            form_data["Minecraft IGN"] = self.ign.value
        if self.target:
            form_data["Reported Player"] = self.target.value
        if self.issue:
            form_data["Issue"] = self.issue.value

        channel = await create_ticket_channel(
            guild=interaction.guild,
            user=interaction.user,
            reason=self.reason,
            form_data=form_data,
        )
        await interaction.response.send_message(
            f"Your Cloudverse ticket has been created: {channel.mention}",
            ephemeral=True,
        )


class TicketReasonSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="General Support", description="General Cloudverse server help"),
            discord.SelectOption(label="Billing Support", description="Purchase, rank, key or coins support"),
            discord.SelectOption(label="Punishment Appeal", description="Appeal a mute, kick or ban"),
            discord.SelectOption(label="Player Reports", description="Report a Cloudverse player"),
            discord.SelectOption(label="Bug Report", description="Report a website, bot or server bug"),
            discord.SelectOption(label="Staff Report", description="Report a staff-related issue"),
            discord.SelectOption(label="Other", description="Anything else"),
        ]

        super().__init__(
            placeholder="Select a ticket reason",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="cloudverse_ticket_reason",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketFormModal(self.values[0]))


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketReasonSelect())


@bot.slash_command(name="sendpanel", description="Send the Cloudverse ticket panel")
@commands.has_permissions(administrator=True)
async def sendpanel(ctx: discord.ApplicationContext):
    if ctx.channel.id != PANEL_CHANNEL_ID:
        await ctx.respond(f"Use this command in <#{PANEL_CHANNEL_ID}> only.", ephemeral=True)
        return

    channel = ctx.channel

    if channel is None:
        await ctx.respond("Panel channel not found. Check PANEL_CHANNEL_ID.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Cloudverse Support",
        description="Select the reason that matches your issue and a private ticket will be created.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Ticket Reasons",
        value=(
            "- General Support\n"
            "- Billing Support\n"
            "- Punishment Appeal\n"
            "- Player Reports\n"
            "- Bug Report\n"
            "- Staff Report\n"
            "- Other"
        ),
        inline=False,
    )
    embed.set_thumbnail(url=CLOUDVERSE_THUMBNAIL_URL)
    embed.set_image(url=CLOUDVERSE_BANNER_URL)

    await channel.send(embed=embed, view=TicketPanelView())
    await ctx.respond("Cloudverse ticket panel sent.", ephemeral=True)


# =========================================================
# STAFF TICKET ACTIONS
# =========================================================

class PurchaseConfirmModal(discord.ui.Modal):
    def __init__(self, order_data: dict[str, Any] | None = None, source_message: discord.Message | None = None):
        super().__init__(title="Confirm Purchase")
        self.order_data = order_data or {}
        self.source_message = source_message

        default_ign = str(self.order_data.get("minecraft_username") or "")
        default_amount = str(self.order_data.get("total") or self.order_data.get("final_total") or "")
        default_products = ", ".join(item.get("name", "Unknown") for item in self.order_data.get("products", []))

        self.ign = discord.ui.InputText(label="Minecraft IGN", placeholder="Player IGN", value=default_ign[:100])
        self.amount = discord.ui.InputText(label="Purchase Amount", placeholder="499", value=default_amount[:100])
        self.bought = discord.ui.InputText(
            label="What did they buy?",
            placeholder="Champion Rank / Keys / Coins",
            style=discord.InputTextStyle.long,
            value=default_products[:1000],
        )

        self.add_item(self.ign)
        self.add_item(self.amount)
        self.add_item(self.bought)

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can confirm purchases.", ephemeral=True)
            return

        state = get_state(self.source_message, interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None)
        player = await find_ticket_creator(interaction.channel, state) if isinstance(interaction.channel, discord.TextChannel) else None
        embed = build_purchase_log_embed(
            title="Purchase Confirmed",
            color=discord.Color.green(),
            staff=interaction.user,
            channel=interaction.channel,
            order_data=self.order_data,
            minecraft_ign=self.ign.value,
            amount=self.amount.value,
            items=self.bought.value,
            status_label="Confirmed",
            player=player,
        )

        await send_purchase_log(embed)
        if self.source_message and self.source_message.embeds:
            ticket_embed = self.source_message.embeds[0]
            set_embed_field(ticket_embed, "Status", "Confirmed", inline=True)
            set_embed_field(ticket_embed, "Confirmed By", interaction.user.mention, inline=True)
            set_embed_field(ticket_embed, "Confirmed At", now_text(), inline=True)
            await self.source_message.edit(embed=ticket_embed)
            state = ticket_claims.setdefault(self.source_message.id, state)
            state["status"] = "Confirmed"
            state["confirmed_by_id"] = interaction.user.id
            state["confirmed_by_name"] = interaction.user.display_name
            state["confirmed_at"] = now_text()
        await interaction.response.send_message(embed=embed, ephemeral=False)


class CloseReasonModal(discord.ui.Modal):
    def __init__(self, source_message: discord.Message | None = None):
        super().__init__(title="Close Ticket")
        self.source_message = source_message
        self.reason = discord.ui.InputText(
            label="Close Reason",
            placeholder="Payment completed / issue resolved / customer inactive",
            style=discord.InputTextStyle.long,
        )
        self.add_item(self.reason)

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
            return
        await close_ticket(interaction, self.reason.value, self.source_message)


class RenameTicketModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Rename Ticket")
        self.name = discord.ui.InputText(label="New Ticket Name", placeholder="order-cv-123-player")
        self.add_item(self.name)

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can rename tickets.", ephemeral=True)
            return
        await interaction.channel.edit(name=clean_channel_name(self.name.value))
        await interaction.response.send_message("Ticket renamed.", ephemeral=True)


class UserPermissionModal(discord.ui.Modal):
    def __init__(self, mode: str):
        super().__init__(title="Add User" if mode == "add" else "Remove User")
        self.mode = mode
        self.user_value = discord.ui.InputText(label="Username, Display Name, ID or Mention", placeholder="PlayerName")
        self.add_item(self.user_value)

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can manage ticket users.", ephemeral=True)
            return

        member = await find_member_by_text(interaction.guild, str(self.user_value.value))
        if not member:
            await interaction.response.send_message("I could not find that user in this server.", ephemeral=True)
            return

        if self.mode == "add":
            await interaction.channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
            await interaction.response.send_message(f"Added {member.mention} to this ticket.", ephemeral=True)
        else:
            await interaction.channel.set_permissions(member, overwrite=None)
            await interaction.response.send_message(f"Removed {member.mention} from this ticket.", ephemeral=True)


async def lock_ticket_channel(channel: discord.TextChannel) -> None:
    for target, overwrite in list(channel.overwrites.items()):
        if isinstance(target, discord.Member) and not is_staff(target):
            overwrite.send_messages = False
            await channel.set_permissions(target, overwrite=overwrite)


async def unlock_ticket_channel(channel: discord.TextChannel) -> None:
    for target, overwrite in list(channel.overwrites.items()):
        if isinstance(target, discord.Member) and not is_staff(target):
            overwrite.send_messages = True
            await channel.set_permissions(target, overwrite=overwrite)


async def close_ticket(interaction: discord.Interaction, reason: str, source_message: discord.Message | None = None) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("This can only be used inside a ticket channel.", ephemeral=True)
        return

    if not interaction.response.is_done():
        await interaction.response.defer()

    state = get_state(source_message, interaction.channel)
    state["closed_by_id"] = interaction.user.id
    state["closed_by_name"] = interaction.user.display_name
    state["closed_at"] = now_text()
    state["close_reason"] = reason or "No reason provided."
    state["status"] = "Closed"

    creator = await find_ticket_creator(interaction.channel, state)
    transcript_bytes = await build_transcript_html(interaction.channel, state)
    transcript_name = f"transcript-{clean_channel_name(interaction.channel.name)}.html"
    transcript_channel = await create_transcript_channel_and_send(interaction.channel, state, transcript_bytes)
    await dm_ticket_creator(creator, transcript_bytes, transcript_name)

    if not interaction.channel.name.startswith("closed-"):
        await interaction.channel.edit(name=clean_channel_name(f"closed-{interaction.channel.name}"))
    await lock_ticket_channel(interaction.channel)

    embed = discord.Embed(title="Ticket Closed", color=discord.Color.red())
    embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
    embed.add_field(name="Time", value=state["closed_at"], inline=True)
    embed.add_field(name="Reason", value=reason or "No reason provided.", inline=False)
    if transcript_channel:
        embed.add_field(name="Transcript", value=transcript_channel.mention, inline=True)

    if source_message and source_message.embeds:
        ticket_embed = source_message.embeds[0]
        set_embed_field(ticket_embed, "Status", "Closed", inline=True)
        set_embed_field(ticket_embed, "Closed By", interaction.user.mention, inline=True)
        set_embed_field(ticket_embed, "Closed At", state["closed_at"], inline=True)
        await source_message.edit(embed=ticket_embed, view=ClosedTicketView())
        ticket_claims.setdefault(source_message.id, state)["status"] = "Closed"

    await interaction.followup.send(embed=embed)
    await asyncio.sleep(5)
    await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}: {reason or 'No reason provided.'}")


async def reopen_ticket(interaction: discord.Interaction, source_message: discord.Message | None = None) -> None:
    current_name = interaction.channel.name
    if current_name.startswith("closed-"):
        await interaction.channel.edit(name=clean_channel_name(current_name.removeprefix("closed-")))
    await unlock_ticket_channel(interaction.channel)
    message = source_message or interaction.message
    await edit_ticket_status_message(
        interaction,
        "Claimed" if ticket_claims.get(message.id, {}).get("claimed_by_id") else "Pending",
        message=message,
        view=TicketStaffView(
            order_data=ticket_claims.get(message.id, {}).get("order_data"),
            claimed=bool(ticket_claims.get(message.id, {}).get("claimed_by_id")),
        ),
    )
    await interaction.response.send_message("Ticket reopened.", ephemeral=True)


class StaffToolsSelect(discord.ui.Select):
    def __init__(self, source_message: discord.Message):
        self.source_message = source_message
        options = [
            discord.SelectOption(label="Confirm Purchase", value="confirm_purchase"),
            discord.SelectOption(label="Not Bought", value="not_bought"),
            discord.SelectOption(label="Close Ticket", value="close_ticket"),
            discord.SelectOption(label="Reopen Ticket", value="reopen_ticket"),
            discord.SelectOption(label="Delete Ticket", value="delete_ticket"),
            discord.SelectOption(label="Add User", value="add_user"),
            discord.SelectOption(label="Remove User", value="remove_user"),
            discord.SelectOption(label="Rename Ticket", value="rename_ticket"),
        ]
        super().__init__(
            placeholder="Staff Tools",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="cloudverse_staff_tools",
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use ticket tools.", ephemeral=True)
            return

        action = self.values[0]
        state = ticket_claims.setdefault(self.source_message.id, {})
        order_data = state.get("order_data")

        if action == "confirm_purchase":
            await interaction.response.send_modal(PurchaseConfirmModal(order_data, self.source_message))
        elif action == "not_bought":
            player = await find_ticket_creator(interaction.channel, state) if isinstance(interaction.channel, discord.TextChannel) else None
            embed = build_purchase_log_embed(
                title="Purchase Marked Not Bought",
                color=discord.Color.red(),
                staff=interaction.user,
                channel=interaction.channel,
                order_data=order_data,
                minecraft_ign=str((order_data or {}).get("minecraft_username") or "Not linked"),
                amount=str((order_data or {}).get("final_total") or (order_data or {}).get("total") or 0),
                items=product_lines((order_data or {}).get("products", [])),
                status_label="Not Bought",
                player=player,
            )
            await send_purchase_log(embed)
            await edit_ticket_status_message(interaction, "Cancelled", message=self.source_message)
            state["status"] = "Not Bought"
            state["marked_not_bought_by_id"] = interaction.user.id
            state["marked_not_bought_at"] = now_text()
            await interaction.response.send_message(embed=embed)
        elif action == "close_ticket":
            await interaction.response.send_modal(CloseReasonModal(self.source_message))
        elif action == "reopen_ticket":
            await reopen_ticket(interaction, self.source_message)
        elif action == "delete_ticket":
            await interaction.response.send_message("Delete confirmation:", view=DeleteConfirmView(), ephemeral=True)
        elif action == "add_user":
            await interaction.response.send_modal(UserPermissionModal("add"))
        elif action == "remove_user":
            await interaction.response.send_modal(UserPermissionModal("remove"))
        elif action == "rename_ticket":
            await interaction.response.send_modal(RenameTicketModal())


class StaffToolsPrivateView(discord.ui.View):
    def __init__(self, source_message: discord.Message):
        super().__init__(timeout=180)
        self.add_item(StaffToolsSelect(source_message))


class TicketStaffView(discord.ui.View):
    def __init__(self, order_data: dict[str, Any] | None = None, claimed: bool = False):
        super().__init__(timeout=None)
        self.order_data = order_data
        for child in self.children:
            if getattr(child, "custom_id", "") == "cloudverse_ticket_claimed":
                child.disabled = claimed
                child.label = "Claimed" if claimed else "Claim"

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.green, custom_id="cloudverse_ticket_claimed")
    async def claimed_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use this button.", ephemeral=True)
            return

        state = ticket_claims.setdefault(interaction.message.id, {"order_data": self.order_data})
        if state.get("claimed_by_id"):
            await interaction.response.send_message(f"This ticket is already claimed by <@{state['claimed_by_id']}>.", ephemeral=True)
            return

        state["claimed_by_id"] = interaction.user.id
        state["claimed_by_name"] = interaction.user.display_name
        state["claimed_at"] = now_text()
        state["status"] = "Claimed"
        state["order_data"] = state.get("order_data") or self.order_data
        state["channel_id"] = interaction.channel.id if interaction.channel else state.get("channel_id")
        state["message_id"] = interaction.message.id

        button.label = "Claimed"
        button.disabled = True
        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0]
            set_embed_field(embed, "Status", "Claimed", inline=True)
            set_embed_field(embed, "Claimed By", interaction.user.mention, inline=True)
            set_embed_field(embed, "Claimed At", state["claimed_at"], inline=True)
            await interaction.message.edit(embed=embed, view=self)

        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.", ephemeral=True)

    @discord.ui.button(label="Staff Tools", style=discord.ButtonStyle.blurple, custom_id="cloudverse_ticket_staff_tools")
    async def staff_tools_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use ticket tools.", ephemeral=True)
            return
        await interaction.response.send_message("Choose a staff action:", view=StaffToolsPrivateView(interaction.message), ephemeral=True)


class ClosedToolsSelect(discord.ui.Select):
    def __init__(self, source_message: discord.Message):
        self.source_message = source_message
        options = [
            discord.SelectOption(label="Reopen Ticket", value="reopen_ticket"),
            discord.SelectOption(label="Delete Ticket", value="delete_ticket"),
        ]
        super().__init__(
            placeholder="Closed Ticket Tools",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="cloudverse_closed_ticket_tools",
        )

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use ticket tools.", ephemeral=True)
            return
        if self.values[0] == "reopen_ticket":
            await reopen_ticket(interaction, self.source_message)
        else:
            await interaction.response.send_message("Delete confirmation:", view=DeleteConfirmView(), ephemeral=True)


class ClosedToolsPrivateView(discord.ui.View):
    def __init__(self, source_message: discord.Message):
        super().__init__(timeout=180)
        self.add_item(ClosedToolsSelect(source_message))


class ClosedTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Closed Tools", style=discord.ButtonStyle.blurple, custom_id="cloudverse_closed_ticket_tools_button")
    async def closed_tools_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use closed ticket tools.", ephemeral=True)
            return
        await interaction.response.send_message("Choose a closed-ticket action:", view=ClosedToolsPrivateView(interaction.message), ephemeral=True)


class DeleteConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger, custom_id="cloudverse_ticket_confirm_delete")
    async def confirm_delete(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can delete tickets.", ephemeral=True)
            return

        await interaction.response.send_message("Deleting this ticket in 5 seconds.")
        await asyncio.sleep(5)
        await interaction.channel.delete(reason=f"Deleted by {interaction.user}")


# =========================================================
# WEBSITE -> BOT API
# =========================================================

@app.get("/")
async def api_home():
    return {"ok": True, "service": "Cloudverse Discord Bot API"}


@app.post("/website/order-ticket")
async def website_order_ticket(
    order: WebsiteOrder,
    x_bot_api_secret: str | None = Header(default=None),
):
    if not WEBSITE_TICKET_SECRET:
        raise HTTPException(status_code=500, detail="WEBSITE_TICKET_SECRET is not configured on the bot.")

    if x_bot_api_secret != WEBSITE_TICKET_SECRET:
        raise HTTPException(status_code=401, detail="Invalid website ticket secret.")

    guild = get_target_guild()
    if guild is None:
        raise HTTPException(status_code=500, detail="Guild not found. Invite the bot to your server or set GUILD_ID.")

    try:
        member = await get_member(guild, order.discord_id)
    except HTTPException:
        return {
            "ok": False,
            "requires_join": True,
            "join_url": DISCORD_INVITE_URL,
            "ticket_channel_url": None,
            "message": "Customer is not inside the Discord server yet.",
        }

    channel = await create_ticket_channel(
        guild=guild,
        user=member,
        reason="Website Purchase",
        order_data=order,
    )

    return {
        "ok": True,
        "ticket_channel_id": str(channel.id),
        "ticket_channel_name": channel.name,
        "ticket_channel_url": f"https://discord.com/channels/{guild.id}/{channel.id}",
    }


async def start_api():
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


# =========================================================
# MODERATION COMMANDS
# =========================================================

@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f"Pong! {latency}ms")


@bot.slash_command(name="ping", description="Check bot ping")
async def slash_ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.respond(f"Pong! {latency}ms")


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason"):
    await member.ban(reason=reason)
    embed = discord.Embed(title="User Banned", color=discord.Color.red())
    embed.add_field(name="User", value=member.mention)
    embed.add_field(name="Moderator", value=ctx.author.mention)
    embed.add_field(name="Reason", value=reason)
    await ctx.send(embed=embed)


@bot.slash_command(name="ban", description="Ban a member")
@commands.has_permissions(ban_members=True)
async def slash_ban(ctx, member: discord.Member, reason: str = "No reason"):
    await member.ban(reason=reason)
    embed = discord.Embed(title="User Banned", color=discord.Color.red())
    embed.add_field(name="User", value=member.mention)
    embed.add_field(name="Moderator", value=ctx.author.mention)
    embed.add_field(name="Reason", value=reason)
    await ctx.respond(embed=embed)


@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int):
    user = await bot.fetch_user(user_id)
    await ctx.guild.unban(user)
    await ctx.send(f"Unbanned {user}")


@bot.slash_command(name="unban", description="Unban a member")
@commands.has_permissions(ban_members=True)
async def slash_unban(ctx, user_id: str):
    user = await bot.fetch_user(int(user_id))
    await ctx.guild.unban(user)
    await ctx.respond(f"Unbanned {user}")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def timeout(ctx, member: discord.Member, minutes: int):
    await member.timeout(timedelta(minutes=minutes))
    await ctx.send(f"Timed out {member.mention} for {minutes} minutes")


@bot.slash_command(name="timeout", description="Timeout a member")
@commands.has_permissions(moderate_members=True)
async def slash_timeout(ctx, member: discord.Member, minutes: int):
    await member.timeout(timedelta(minutes=minutes))
    await ctx.respond(f"Timed out {member.mention} for {minutes} minutes")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def removetimeout(ctx, member: discord.Member):
    await member.timeout(None)
    await ctx.send(f"Removed timeout from {member.mention}")


@bot.slash_command(name="removetimeout", description="Remove timeout")
@commands.has_permissions(moderate_members=True)
async def slash_removetimeout(ctx, member: discord.Member):
    await member.timeout(None)
    await ctx.respond(f"Removed timeout from {member.mention}")


@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason"):
    await member.kick(reason=reason)
    await ctx.send(f"Kicked {member.mention}")


@bot.slash_command(name="kick", description="Kick a member")
@commands.has_permissions(kick_members=True)
async def slash_kick(ctx, member: discord.Member, reason: str = "No reason"):
    await member.kick(reason=reason)
    await ctx.respond(f"Kicked {member.mention}")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    await ctx.channel.purge(limit=amount + 1)
    msg = await ctx.send(f"Deleted {amount} messages")
    await msg.delete(delay=3)


@bot.slash_command(name="purge", description="Delete messages")
@commands.has_permissions(manage_messages=True)
async def slash_purge(ctx, amount: int):
    await ctx.channel.purge(limit=amount)
    await ctx.respond(f"Deleted {amount} messages", delete_after=3)

@trigger.command(name="add")
@commands.has_permissions(administrator=True)
async def trigger_add(
    ctx,
    trigger_word: str,
    title: str,
    response: str
):

    trigger_word = trigger_word.lower()

    triggers[trigger_word] = {
        "title": title,
        "response": response
    }

    save_triggers()

    await ctx.respond(
        f"✅ Trigger `{trigger_word}` added.",
        ephemeral=True
    )


@trigger.command(name="remove")
@commands.has_permissions(administrator=True)
async def trigger_remove(
    ctx,
    trigger_word: str
):

    trigger_word = trigger_word.lower()

    if trigger_word not in triggers:
        return await ctx.respond(
            "Trigger not found.",
            ephemeral=True
        )

    del triggers[trigger_word]

    save_triggers()

    await ctx.respond(
        "Trigger removed.",
        ephemeral=True
    )


@trigger.command(name="list")
@commands.has_permissions(administrator=True)
async def trigger_list(ctx):

    embed = discord.Embed(
        title="Triggers",
        color=discord.Color.blurple()
    )

    if not triggers:
        embed.description = "No triggers."

    else:

        embed.description = "\n".join(
            f"• `{x}`"
            for x in triggers
        )

    await ctx.respond(embed=embed, ephemeral=True)


# =========================================================
# LEVEL COMMANDS
# =========================================================

@bot.slash_command(name="rank", description="Show your Cloudverse rank card")
async def rank(ctx: discord.ApplicationContext, member: discord.Member = None):
    target = member or ctx.author
    record = await get_level_record(target.id)
    current_level_xp = xp_for_level(record["level"])
    next_level_xp = xp_for_level(record["level"] + 1)
    current_progress = max(0, record["xp"] - current_level_xp)
    needed_progress = max(1, next_level_xp - current_level_xp)
    next_role_id = next_reward_role_id(record["level"])
    reward_text = f"<@&{next_role_id}>" if next_role_id else "All level rewards unlocked"

    embed = discord.Embed(
        title=f"{target.display_name}'s Rank",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=user_avatar_url(target))
    embed.add_field(name="Current Level", value=str(record["level"]), inline=True)
    embed.add_field(name="XP", value=f"{record['xp']:,}", inline=True)
    embed.add_field(name="XP Until Next Level", value=f"{max(0, next_level_xp - record['xp']):,}", inline=True)
    embed.add_field(name="Progress", value=f"`{progress_bar(current_progress, needed_progress)}`", inline=False)
    embed.add_field(name="Current Rank", value=current_rank_name(record["level"]), inline=True)
    embed.add_field(name="Messages", value=f"{record['messages']:,}", inline=True)
    embed.add_field(name="Voice Time", value=format_duration(record.get("voice_seconds", 0)), inline=True)
    embed.add_field(name="Reward Role", value=reward_text, inline=True)

    file = await generate_level_card(target, record)
    if file:
        embed.set_image(url="attachment://cloudverse-rank.png")
        await ctx.respond(embed=embed, file=file)
    else:
        await ctx.respond(embed=embed)


@bot.slash_command(name="level", description="Show your current Cloudverse level")
async def level(ctx: discord.ApplicationContext, member: discord.Member = None):
    target = member or ctx.author
    record = await get_level_record(target.id)
    next_xp = xp_for_level(record["level"] + 1)
    embed = discord.Embed(
        title="Cloudverse Level",
        description=f"{target.mention} is **Level {record['level']}** with **{record['xp']:,} XP**.",
        color=discord.Color.blue(),
    )
    embed.set_thumbnail(url=user_avatar_url(target))
    embed.add_field(name="XP Until Next Level", value=f"{max(0, next_xp - record['xp']):,}", inline=True)
    embed.add_field(name="Messages", value=f"{record['messages']:,}", inline=True)
    embed.add_field(name="Voice Time", value=format_duration(record.get("voice_seconds", 0)), inline=True)
    await ctx.respond(embed=embed)


@bot.slash_command(name="leaderboard", description="Show the top 10 Cloudverse XP users")
async def leaderboard(ctx: discord.ApplicationContext):
    if level_db is None:
        await init_level_database()
    async with level_db_lock:
        rows = level_db.execute(
            "SELECT user_id, xp, level, messages, voice_seconds FROM levels ORDER BY xp DESC, messages DESC LIMIT 10"
        ).fetchall()

    embed = discord.Embed(
        title="Cloudverse XP Leaderboard",
        description="Top 10 most active members.",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=CLOUDVERSE_THUMBNAIL_URL)
    if not rows:
        embed.description = "No XP data yet."
    else:
        lines = []
        for index, row in enumerate(rows, start=1):
            user_id = int(row["user_id"])
            member = ctx.guild.get_member(user_id) if ctx.guild else None
            name = member.mention if member else f"<@{user_id}>"
            lines.append(
                f"**#{index}** {name} - Level **{int(row['level'])}** • **{int(row['xp']):,} XP** • {int(row['messages']):,} messages"
            )
        embed.description = "\n".join(lines)
    await ctx.respond(embed=embed)


@bot.slash_command(name="stats", description="Show a Cloudverse player profile stats card")
async def stats(ctx: discord.ApplicationContext, member: discord.Member = None):
    target = member or ctx.author
    record = await get_level_record(target.id)
    embed = discord.Embed(
        title=f"{target.display_name}'s Cloudverse Stats",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=user_avatar_url(target))
    embed.add_field(name="Level", value=str(record["level"]), inline=True)
    embed.add_field(name="Total XP", value=f"{record['xp']:,}", inline=True)
    embed.add_field(name="Rank", value=current_rank_name(record["level"]), inline=True)
    embed.add_field(name="Messages", value=f"{record.get('messages', 0):,}", inline=True)
    embed.add_field(name="Voice Time", value=format_duration(record.get("voice_seconds", 0)), inline=True)
    embed.add_field(name="Voice XP", value=f"{record.get('voice_xp', 0):,}", inline=True)
    file = await generate_stats_card(target, record)
    if file:
        embed.set_image(url="attachment://cloudverse-stats.png")
        await ctx.respond(embed=embed, file=file)
    else:
        await ctx.respond(embed=embed)


@bot.slash_command(name="setlevel", description="Admin: set a member's Cloudverse level")
@commands.has_permissions(administrator=True)
async def setlevel(ctx: discord.ApplicationContext, member: discord.Member, new_level: int):
    new_level = max(0, int(new_level))
    record = await set_user_xp(member, xp_for_level(new_level))
    await ctx.respond(f"Set {member.mention} to Level {record['level']} with {record['xp']:,} XP.", ephemeral=True)


@bot.slash_command(name="addxp", description="Admin: add XP to a member")
@commands.has_permissions(administrator=True)
async def addxp(ctx: discord.ApplicationContext, member: discord.Member, amount: int):
    record = await get_level_record(member.id)
    old_level = record["level"]
    record = await set_user_xp(member, record["xp"] + max(0, int(amount)))
    if record["level"] > old_level:
        await apply_level_roles(member, record["level"])
    await ctx.respond(f"Added {max(0, int(amount)):,} XP to {member.mention}. New total: {record['xp']:,} XP.", ephemeral=True)


@bot.slash_command(name="removexp", description="Admin: remove XP from a member")
@commands.has_permissions(administrator=True)
async def removexp(ctx: discord.ApplicationContext, member: discord.Member, amount: int):
    record = await get_level_record(member.id)
    record = await set_user_xp(member, record["xp"] - max(0, int(amount)))
    await ctx.respond(f"Removed {max(0, int(amount)):,} XP from {member.mention}. New total: {record['xp']:,} XP.", ephemeral=True)


@bot.slash_command(name="resetxp", description="Admin: reset a member's XP")
@commands.has_permissions(administrator=True)
async def resetxp(ctx: discord.ApplicationContext, member: discord.Member):
    record = await get_level_record(member.id)
    record["xp"] = 0
    record["level"] = 0
    record["messages"] = 0
    record["last_xp_ts"] = 0
    record["voice_seconds"] = 0
    record["voice_xp"] = 0
    record["text_xp"] = 0
    await save_level_record(record)
    await ctx.respond(f"Reset XP for {member.mention}.", ephemeral=True)





@bot.slash_command(
    name="steal",
    description="Steal an emoji from Discord."
)
@discord.default_permissions(manage_emojis_and_stickers=True)
async def steal(
    ctx: discord.ApplicationContext,
    emoji: str,
    name: str = None
):

    await ctx.defer(ephemeral=True)

    # Match <:name:id> or <a:name:id>
    match = re.match(r"<(a?):(\w+):(\d+)>", emoji)

    if not match:
        return await ctx.followup.send(
            "❌ Please provide a valid custom emoji.\nExample: <:Cloud:123456789012345678>",
            ephemeral=True
        )

    animated = match.group(1) == "a"
    emoji_name = match.group(2)
    emoji_id = match.group(3)

    extension = "gif" if animated else "png"

    url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}?quality=lossless"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:

            if resp.status != 200:
                return await ctx.followup.send(
                    "❌ Couldn't download that emoji.",
                    ephemeral=True
                )

            image = await resp.read()

    try:

        new_emoji = await ctx.guild.create_custom_emoji(
            name=name or emoji_name,
            image=image,
            reason=f"Emoji stolen by {ctx.author}"
        )

        embed = discord.Embed(
            title="✅ Emoji Added",
            description=f"{new_emoji} has been added to **{ctx.guild.name}**!",
            color=discord.Color.green()
        )

        embed.add_field(
            name="Emoji",
            value=str(new_emoji),
            inline=True
        )

        embed.add_field(
            name="Name",
            value=f"`{new_emoji.name}`",
            inline=True
        )

        embed.add_field(
            name="Animated",
            value="Yes" if animated else "No",
            inline=True
        )

        embed.set_thumbnail(url=new_emoji.url)

        embed.set_footer(
            text=f"Added by {ctx.author}",
            icon_url=ctx.author.display_avatar.url
        )

        await ctx.followup.send(embed=embed)

    except discord.Forbidden:
        await ctx.followup.send(
            "❌ I need **Manage Emojis and Stickers** permission.",
            ephemeral=True
        )

    except discord.HTTPException as e:
        await ctx.followup.send(
            f"❌ Discord Error:\n```{e}```",
            ephemeral=True
        )

    except Exception as e:
        await ctx.followup.send(
            f"❌ Error:\n```{e}```",
            ephemeral=True
        )



# =========================================================
# EVENTS
# =========================================================

@bot.event
async def on_ready():
    global voice_xp_task
    await init_level_database()
    await restore_active_voice_sessions()
    if voice_xp_task is None or voice_xp_task.done():
        voice_xp_task = asyncio.create_task(voice_xp_loop())
    bot.add_view(TicketPanelView())
    bot.add_view(TicketStaffView())
    bot.add_view(ClosedTicketView())
    bot.add_view(DeleteConfirmView())
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message):

    if message.author.bot:
        return

    await handle_level_message(message)

    content = message.content.lower().strip()

    if content in triggers:

        data = triggers[content]

        embed = discord.Embed(
            title=data["title"],
            description=data["response"],
            color=discord.Color.red()
        )

        await message.reply(
            embed=embed,
            mention_author=False
        )

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    joined_voice = before.channel is None and after.channel is not None
    left_voice = before.channel is not None and after.channel is None
    moved_voice = before.channel is not None and after.channel is not None and before.channel.id != after.channel.id

    if joined_voice:
        await start_voice_session(member)
    elif left_voice:
        await flush_voice_session(member, final=True)
    elif moved_voice:
        session = voice_sessions.get(member.id)
        if session:
            session["channel_id"] = after.channel.id
        else:
            await start_voice_session(member)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Missing required arguments.")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You do not have permission.")
        return
    print(error)


async def main():
    if not TOKEN:
        raise RuntimeError("TOKEN is missing.")

    async with bot:
        asyncio.create_task(start_api())
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
