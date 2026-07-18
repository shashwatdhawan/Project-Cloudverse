import discord
from discord.ext import commands
from datetime import timedelta
import json
import random
import os

TOKEN = os.getenv("TOKEN")

intents = discord.Intents.all()

bot = commands.Bot(
    command_prefix=",",
    intents=intents,
    help_command=None
)

@bot.event
async def on_ready():

    print(f"Logged in as {bot.user}")

# ================= ERROR HANDLER =================

@bot.event
async def on_command_error(ctx, error):

    if isinstance(error, commands.MissingRequiredArgument):

        await ctx.send(
            "❌ Missing required arguments."
        )

    elif isinstance(error, commands.CommandNotFound):

        return

    elif isinstance(error, commands.MissingPermissions):

        await ctx.send(
            "❌ You don't have permission."
        )

    else:
        print(error)

# =========================================================
# PING
# =========================================================

@bot.command()
async def ping(ctx):

    latency = round(bot.latency * 1000)

    await ctx.send(f"🏓 Pong! {latency}ms")

@bot.slash_command(
    name="ping",
    description="Check bot ping"
)
async def slash_ping(ctx):

    latency = round(bot.latency * 1000)

    await ctx.respond(
        f"🏓 Pong! {latency}ms"
    )

# =========================================================
# BAN
# =========================================================

@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason"):

    await member.ban(reason=reason)

    embed = discord.Embed(
        title="🔨 User Banned",
        color=discord.Color.red()
    )

    embed.add_field(
        name="User",
        value=member.mention
    )

    embed.add_field(
        name="Moderator",
        value=ctx.author.mention
    )

    embed.add_field(
        name="Reason",
        value=reason
    )

    await ctx.send(embed=embed)

@bot.slash_command(
    name="ban",
    description="Ban a member"
)
@commands.has_permissions(ban_members=True)
async def slash_ban(
    ctx,
    member: discord.Member,
    reason: str = "No reason"
):

    await member.ban(reason=reason)

    embed = discord.Embed(
        title="🔨 User Banned",
        color=discord.Color.red()
    )

    embed.add_field(
        name="User",
        value=member.mention
    )

    embed.add_field(
        name="Moderator",
        value=ctx.author.mention
    )

    embed.add_field(
        name="Reason",
        value=reason
    )

    await ctx.respond(embed=embed)

# =========================================================
# UNBAN
# =========================================================

@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int):

    user = await bot.fetch_user(user_id)

    await ctx.guild.unban(user)

    await ctx.send(
        f"✅ Unbanned {user}"
    )

@bot.slash_command(
    name="unban",
    description="Unban a member"
)
@commands.has_permissions(ban_members=True)
async def slash_unban(
    ctx,
    user_id: str
):

    user = await bot.fetch_user(
        int(user_id)
    )

    await ctx.guild.unban(user)

    await ctx.respond(
        f"✅ Unbanned {user}"
    )

# =========================================================
# TIMEOUT
# =========================================================

@bot.command()
@commands.has_permissions(moderate_members=True)
async def timeout(
    ctx,
    member: discord.Member,
    minutes: int
):

    duration = timedelta(
        minutes=minutes
    )

    await member.timeout(duration)

    await ctx.send(
        f"⏳ Timed out {member.mention} for {minutes} minutes"
    )

@bot.slash_command(
    name="timeout",
    description="Timeout a member"
)
@commands.has_permissions(moderate_members=True)
async def slash_timeout(
    ctx,
    member: discord.Member,
    minutes: int
):

    duration = timedelta(
        minutes=minutes
    )

    await member.timeout(duration)

    await ctx.respond(
        f"⏳ Timed out {member.mention} for {minutes} minutes"
    )

# =========================================================
# REMOVE TIMEOUT
# =========================================================

@bot.command()
@commands.has_permissions(moderate_members=True)
async def removetimeout(
    ctx,
    member: discord.Member
):

    await member.timeout(None)

    await ctx.send(
        f"✅ Removed timeout from {member.mention}"
    )

@bot.slash_command(
    name="removetimeout",
    description="Remove timeout"
)
@commands.has_permissions(moderate_members=True)
async def slash_removetimeout(
    ctx,
    member: discord.Member
):

    await member.timeout(None)

    await ctx.respond(
        f"✅ Removed timeout from {member.mention}"
    )

# =========================================================
# KICK
# =========================================================

@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(
    ctx,
    member: discord.Member
):

    await member.kick()

    await ctx.send(
        f"👢 Kicked {member.mention}"
    )

@bot.slash_command(
    name="kick",
    description="Kick a member"
)
@commands.has_permissions(kick_members=True)
async def slash_kick(
    ctx,
    member: discord.Member
):

    await member.kick()

    await ctx.respond(
        f"👢 Kicked {member.mention}"
    )

# =========================================================
# PURGE
# =========================================================

@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(
    ctx,
    amount: int
):

    await ctx.channel.purge(
        limit=amount + 1
    )

    msg = await ctx.send(
        f"🗑 Deleted {amount} messages"
    )

    await msg.delete(delay=3)

@bot.slash_command(
    name="purge",
    description="Delete messages"
)
@commands.has_permissions(manage_messages=True)
async def slash_purge(
    ctx,
    amount: int
):

    await ctx.channel.purge(
        limit=amount
    )

    await ctx.respond(
        f"🗑 Deleted {amount} messages",
        delete_after=3
    )

# =========================================================
# RUN
# =========================================================

bot.run(TOKEN)