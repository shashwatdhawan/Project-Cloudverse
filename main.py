import asyncio
import os
import re
from datetime import timedelta
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
        embed.add_field(name="Status", value=order_data.status, inline=True)
        embed.add_field(name="Products", value=product_lines(order_data.products), inline=False)
        embed.add_field(name="Coupon", value=order_data.coupon or "None", inline=True)
        embed.add_field(name="Discount", value=f"Rs. {order_data.discount}", inline=True)
        embed.add_field(name="Total", value=f"Rs. {order_data.amount}", inline=True)
        embed.set_footer(text="Staff: after handling the order, choose Bought or Not Bought / Close.")
    else:
        embed = discord.Embed(
            title="Cloudverse Support Ticket",
            description=f"Welcome {user.mention}. Staff will help you soon.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text="Please explain your issue clearly.")

    embed.set_thumbnail(url=CLOUDVERSE_THUMBNAIL_URL)
    embed.set_image(url=CLOUDVERSE_BANNER_URL)

    await channel.send(
        content=f"{user.mention} {staff_ping}",
        embed=embed,
        view=TicketStaffView(order_data=order_data.model_dump() if order_data else None),
    )

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
    def __init__(self, order_data: dict[str, Any] | None = None):
        super().__init__(title="Confirm Purchase")
        self.order_data = order_data or {}

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
        await interaction.response.send_message(embed=embed, view=AfterCloseView())


class TicketStaffView(discord.ui.View):
    def __init__(self, order_data: dict[str, Any] | None = None):
        super().__init__(timeout=None)
        self.order_data = order_data

    @discord.ui.button(label="Bought", style=discord.ButtonStyle.green, custom_id="cloudverse_ticket_bought")
    async def bought_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use this button.", ephemeral=True)
            return

        await interaction.response.send_modal(PurchaseConfirmModal(self.order_data))

    @discord.ui.button(label="Not Bought / Close", style=discord.ButtonStyle.red, custom_id="cloudverse_ticket_not_bought")
    async def not_bought_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use this button.", ephemeral=True)
            return

        if not interaction.channel.name.startswith("closed-"):
            await interaction.channel.edit(name=clean_channel_name(f"closed-{interaction.channel.name}"))

        embed = discord.Embed(
            title="Ticket Closed",
            description="This ticket was closed without confirmed purchase.",
            color=discord.Color.red(),
        )
        embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)

        await interaction.response.send_message(embed=embed, view=AfterCloseView())


class AfterCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Delete Ticket", style=discord.ButtonStyle.danger, custom_id="cloudverse_ticket_delete")
    async def delete_ticket(self, button: discord.ui.Button, interaction: discord.Interaction):
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

    member = await get_member(guild, order.discord_id)

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
    bot.add_view(AfterCloseView())
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
