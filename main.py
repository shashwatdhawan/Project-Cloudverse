import asyncio
import os
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

# You can hardcode normal Discord IDs safely.
# Keep TOKEN and WEBSITE_TICKET_SECRET in Railway Variables.
GUILD_ID = 0
STAFF_ROLE_ID = 1502694829824540838
TICKET_CATEGORY_ID = 1529339706217861170
LOG_CHANNEL_ID = 1529340398651314207
PANEL_CHANNEL_ID = 1502694830625652878
TRANSCRIPT_CHANNEL_ID = LOG_CHANNEL_ID
DISCORD_INVITE_URL = "https://discord.gg/jWDH4GYuns"

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
) -> None:
    message = interaction.message
    if not message or not message.embeds:
        return

    embed = message.embeds[0]
    set_embed_field(embed, "Status", status, inline=True)
    if extra_fields:
        for name, value in extra_fields.items():
            set_embed_field(embed, name, value, inline=True)

    await message.edit(embed=embed, view=TicketStaffView())


async def send_purchase_log(embed: discord.Embed) -> None:
    if not TRANSCRIPT_CHANNEL_ID:
        return

    channel = bot.get_channel(TRANSCRIPT_CHANNEL_ID)
    if channel:
        await channel.send(embed=embed)


def get_target_guild() -> discord.Guild | None:
    if GUILD_ID:
        return bot.get_guild(GUILD_ID)
    return bot.guilds[0] if bot.guilds else None


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
# TICKET CREATION
# =========================================================

async def create_ticket_channel(
    guild: discord.Guild,
    user: discord.Member,
    reason: str,
    order_data: WebsiteOrder | None = None,
) -> discord.TextChannel:
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if category is not None and not isinstance(category, discord.CategoryChannel):
        category = None

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
        embed.set_footer(text="Staff: claim the ticket, then use Staff Tools.")

    embed.set_thumbnail(url=CLOUDVERSE_THUMBNAIL_URL)
    embed.set_image(url=CLOUDVERSE_BANNER_URL)

    message = await channel.send(
        content=f"{user.mention} {staff_ping}",
        embed=embed,
        view=TicketStaffView(order_data=order_data.model_dump() if order_data else None),
    )

    ticket_claims[message.id] = {
        "claimed_by_id": None,
        "claimed_by_name": None,
        "status": "Pending",
        "order_data": order_data.model_dump() if order_data else None,
        "original_name": channel.name,
    }

    return channel


# =========================================================
# NORMAL TICKET PANEL
# =========================================================

class TicketReasonSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="General Support", description="General server or store help"),
            discord.SelectOption(label="Refund Related", description="Help with refund questions"),
            discord.SelectOption(label="Report a Bug", description="Report a website, bot or server bug"),
            discord.SelectOption(label="Billing Support", description="Payment or purchase support"),
            discord.SelectOption(label="Punishment Appeal", description="Appeal a mute, kick or ban"),
            discord.SelectOption(label="Player Reports", description="Report a player"),
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
        channel = await create_ticket_channel(
            guild=interaction.guild,
            user=interaction.user,
            reason=self.values[0],
        )

        await interaction.response.send_message(
            f"Your ticket has been created: {channel.mention}",
            ephemeral=True,
        )


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketReasonSelect())


@bot.slash_command(name="sendpanel", description="Send the Cloudverse ticket panel")
@commands.has_permissions(administrator=True)
async def sendpanel(ctx: discord.ApplicationContext):
    channel = bot.get_channel(PANEL_CHANNEL_ID) if PANEL_CHANNEL_ID else ctx.channel

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
            "- Refund Related\n"
            "- Report a Bug\n"
            "- Billing Support\n"
            "- Punishment Appeal\n"
            "- Player Reports\n"
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

        embed = discord.Embed(title="Purchase Confirmed", color=discord.Color.green())
        embed.add_field(name="Minecraft IGN", value=self.ign.value, inline=True)
        embed.add_field(name="Amount", value=f"Rs. {self.amount.value}", inline=True)
        embed.add_field(name="Bought", value=self.bought.value, inline=False)
        embed.add_field(name="Confirmed By", value=interaction.user.mention, inline=False)
        if self.order_data.get("order_id"):
            embed.add_field(name="Order ID", value=str(self.order_data["order_id"]), inline=True)

        await send_purchase_log(embed)
        if self.source_message and self.source_message.embeds:
            ticket_embed = self.source_message.embeds[0]
            set_embed_field(ticket_embed, "Status", "Confirmed", inline=True)
            set_embed_field(ticket_embed, "Confirmed By", interaction.user.mention, inline=True)
            await self.source_message.edit(embed=ticket_embed, view=TicketStaffView())
            ticket_claims.setdefault(self.source_message.id, {})["status"] = "Confirmed"
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
        self.user_value = discord.ui.InputText(label="User ID or Mention", placeholder="123456789012345678")
        self.add_item(self.user_value)

    async def callback(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can manage ticket users.", ephemeral=True)
            return

        raw = str(self.user_value.value)
        match = re.search(r"\d{15,25}", raw)
        if not match:
            await interaction.response.send_message("Please provide a valid Discord user ID or mention.", ephemeral=True)
            return

        try:
            member = interaction.guild.get_member(int(match.group(0))) or await interaction.guild.fetch_member(int(match.group(0)))
        except Exception:
            await interaction.response.send_message("That user is not in this server.", ephemeral=True)
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
    if not interaction.channel.name.startswith("closed-"):
        await interaction.channel.edit(name=clean_channel_name(f"closed-{interaction.channel.name}"))
    await lock_ticket_channel(interaction.channel)

    embed = discord.Embed(title="Ticket Closed", color=discord.Color.red())
    embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
    embed.add_field(name="Time", value=now_text(), inline=True)
    embed.add_field(name="Reason", value=reason or "No reason provided.", inline=False)
    await send_purchase_log(embed)

    if source_message and source_message.embeds:
        ticket_embed = source_message.embeds[0]
        set_embed_field(ticket_embed, "Status", "Closed", inline=True)
        await source_message.edit(embed=ticket_embed, view=ClosedTicketView())
        ticket_claims.setdefault(source_message.id, {})["status"] = "Closed"

    await interaction.response.send_message(embed=embed, view=ClosedTicketView())


async def reopen_ticket(interaction: discord.Interaction) -> None:
    current_name = interaction.channel.name
    if current_name.startswith("closed-"):
        await interaction.channel.edit(name=clean_channel_name(current_name.removeprefix("closed-")))
    await unlock_ticket_channel(interaction.channel)
    await edit_ticket_status_message(interaction, "Claimed" if ticket_claims.get(interaction.message.id, {}).get("claimed_by_id") else "Pending")
    await interaction.response.send_message("Ticket reopened.", ephemeral=True)


class StaffToolsSelect(discord.ui.Select):
    def __init__(self):
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
        state = ticket_claims.setdefault(interaction.message.id, {})
        order_data = state.get("order_data")

        if action == "confirm_purchase":
            await interaction.response.send_modal(PurchaseConfirmModal(order_data, interaction.message))
        elif action == "not_bought":
            embed = discord.Embed(title="Purchase Marked Not Bought", color=discord.Color.red())
            embed.add_field(name="Marked By", value=interaction.user.mention, inline=True)
            embed.add_field(name="Time", value=now_text(), inline=True)
            if order_data and order_data.get("order_id"):
                embed.add_field(name="Order ID", value=str(order_data["order_id"]), inline=True)
            await send_purchase_log(embed)
            await edit_ticket_status_message(interaction, "Cancelled")
            await interaction.response.send_message(embed=embed)
        elif action == "close_ticket":
            await interaction.response.send_modal(CloseReasonModal(interaction.message))
        elif action == "reopen_ticket":
            await reopen_ticket(interaction)
        elif action == "delete_ticket":
            await interaction.response.send_message("Delete confirmation:", view=DeleteConfirmView(), ephemeral=True)
        elif action == "add_user":
            await interaction.response.send_modal(UserPermissionModal("add"))
        elif action == "remove_user":
            await interaction.response.send_modal(UserPermissionModal("remove"))
        elif action == "rename_ticket":
            await interaction.response.send_modal(RenameTicketModal())


class TicketStaffView(discord.ui.View):
    def __init__(self, order_data: dict[str, Any] | None = None):
        super().__init__(timeout=None)
        self.order_data = order_data
        self.add_item(StaffToolsSelect())

    @discord.ui.button(label="Claimed", style=discord.ButtonStyle.green, custom_id="cloudverse_ticket_claimed")
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

        button.disabled = True
        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0]
            set_embed_field(embed, "Status", "Claimed", inline=True)
            set_embed_field(embed, "Claimed By", interaction.user.mention, inline=True)
            set_embed_field(embed, "Claimed At", state["claimed_at"], inline=True)
            await interaction.message.edit(embed=embed, view=self)

        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.", ephemeral=True)


class ClosedToolsSelect(discord.ui.Select):
    def __init__(self):
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
            await reopen_ticket(interaction)
        else:
            await interaction.response.send_message("Delete confirmation:", view=DeleteConfirmView(), ephemeral=True)


class ClosedTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ClosedToolsSelect())


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
