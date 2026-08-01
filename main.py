import asyncio
import html
import io
import os
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
import uvicorn
from discord.ext import commands
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


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
GUILD_ID = 0
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
# EVENTS
# =========================================================

@bot.event
async def on_ready():
    bot.add_view(TicketPanelView())
    bot.add_view(TicketStaffView())
    bot.add_view(ClosedTicketView())
    bot.add_view(DeleteConfirmView())
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message):

    if message.author.bot:
        return

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
