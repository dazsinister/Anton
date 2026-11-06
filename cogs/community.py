"""Welcome and leave messages, auto-role on join, and the color role picker.

Settings are saved per server in the database and changed with /welcome, /leave and /colors.
Welcome/leave messages need the Server Members intent (see README).
"""
from __future__ import annotations

import json
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core import db

COLORS: list[tuple[str, int, str]] = [
    ("Yellow", 0xF1C40F, "🟡"),
    ("Blue",   0x3498DB, "🔵"),
    ("Green",  0x2ECC71, "🟢"),
    ("Purple", 0x9B59B6, "🟣"),
    ("Red",    0xE74C3C, "🔴"),
    ("Orange", 0xE67E22, "🟠"),
]

DEFAULT_WELCOME = "Welcome to **{server}**, {user}! Make yourself at home. 🎉"
DEFAULT_LEAVE = "**{name}** has left the server. 👋"
PLACEHOLDER_HELP = "You can use {user} (mention), {name}, {server} and {count} (member count)."


# ── settings helpers ────────────────────────────────────────

def gkey(guild_id: int, name: str) -> str:
    return f"guild:{guild_id}:{name}"


def gget(guild_id: int, name: str) -> Optional[str]:
    return db.get_setting(gkey(guild_id, name))


def gset(guild_id: int, name: str, value: Optional[str]) -> None:
    db.set_setting(gkey(guild_id, name), value)


def fill(template: str, member: discord.abc.User, guild: discord.Guild) -> str:
    """Replace {user}, {name}, {server}, {count}; unknown {words} are left as typed."""
    values = {
        "user": member.mention,
        "name": getattr(member, "display_name", member.name),
        "server": guild.name,
        "count": str(guild.member_count or ""),
    }
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1).lower(), m.group(0)), template)


def color_roles(guild_id: int) -> dict[str, int]:
    raw = gget(guild_id, "color_roles")
    return json.loads(raw) if raw else {}


async def get_channel(bot: commands.Bot, cid: Optional[str]):
    if not cid:
        return None
    ch = bot.get_channel(int(cid))
    if ch is None:
        try:
            ch = await bot.fetch_channel(int(cid))
        except discord.HTTPException:
            return None
    return ch


# ── color buttons (keep working after restarts) ─────────────

class ColorButton(discord.ui.DynamicItem[discord.ui.Button], template=r"anton:color:(?P<role>\d+|none)"):
    def __init__(self, role: str, label: str = "", emoji: Optional[str] = None):
        is_none = role == "none"
        super().__init__(discord.ui.Button(
            label=label or ("Remove color" if is_none else "Color"),
            emoji=emoji or ("✖️" if is_none else None),
            style=discord.ButtonStyle.secondary,
            custom_id=f"anton:color:{role}",
        ))
        self.role = role

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["role"], label=item.label or "", emoji=str(item.emoji) if item.emoji else None)

    async def callback(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)
        configured = set(color_roles(guild.id).values())
        current = [r for r in member.roles if r.id in configured]

        if self.role == "none":
            if not current:
                return await interaction.response.send_message("You don't have a color right now.", ephemeral=True)
            try:
                await member.remove_roles(*current, reason="Color picker")
            except discord.Forbidden:
                return await interaction.response.send_message(NO_PERMS, ephemeral=True)
            return await interaction.response.send_message("Color removed.", ephemeral=True)

        role = guild.get_role(int(self.role))
        if role is None or role.id not in configured:
            return await interaction.response.send_message(
                "That color isn't set up anymore. Ask an admin to run `/colors setup` again.", ephemeral=True)
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Color picker")
                return await interaction.response.send_message(f"Removed **{role.name}**.", ephemeral=True)
            others = [r for r in current if r.id != role.id]
            if others:
                await member.remove_roles(*others, reason="Color picker")
            await member.add_roles(role, reason="Color picker")
        except discord.Forbidden:
            return await interaction.response.send_message(NO_PERMS, ephemeral=True)
        await interaction.response.send_message(f"You're now **{role.name}**! 🎨", ephemeral=True)


NO_PERMS = ("I'm not allowed to change that role. An admin needs to give me **Manage Roles** and drag my role "
            "**above** the color roles in Server Settings → Roles.")


def color_panel(guild: discord.Guild) -> tuple[discord.Embed, discord.ui.View]:
    roles = color_roles(guild.id)
    view = discord.ui.View(timeout=None)
    lines = []
    for name, _hex, emoji in COLORS:
        rid = roles.get(name)
        role = guild.get_role(rid) if rid else None
        if role is None:
            continue
        view.add_item(ColorButton(str(role.id), label=name, emoji=emoji))
        lines.append(f"{emoji} {role.mention}")
    view.add_item(ColorButton("none"))
    emb = discord.Embed(
        title="🎨 Pick your name color",
        description="Click a color to get it. Picking a new one swaps the old one out.\n\n" + "\n".join(lines),
        color=0x5865F2,
    )
    return emb, view


# ── the cog ─────────────────────────────────────────────────

class Community(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Events

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        g = member.guild
        role_id = gget(g.id, "autorole")
        if role_id:
            role = g.get_role(int(role_id))
            if role:
                try:
                    await member.add_roles(role, reason="Anton auto-role")
                except discord.HTTPException as e:
                    print(f"[welcome] couldn't give auto-role in {g.name}: {e}")

        if gget(g.id, "welcome_on") == "1":
            ch = await get_channel(self.bot, gget(g.id, "welcome_channel"))
            if isinstance(ch, discord.abc.Messageable):
                try:
                    await ch.send(content=member.mention, embed=self.welcome_embed(member),
                                  allowed_mentions=discord.AllowedMentions(users=True))
                except discord.HTTPException as e:
                    print(f"[welcome] post failed in {g.name}: {e}")

        dm = gget(g.id, "welcome_dm")
        if dm:
            try:
                await member.send(fill(dm, member, g)[:2000])
            except discord.HTTPException:
                pass  # DMs closed; nothing to do

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        g = member.guild
        if gget(g.id, "leave_on") != "1":
            return
        ch = await get_channel(self.bot, gget(g.id, "leave_channel") or gget(g.id, "welcome_channel"))
        if isinstance(ch, discord.abc.Messageable):
            try:
                await ch.send(embed=self.leave_embed(member), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                print(f"[leave] post failed in {g.name}: {e}")

    def welcome_embed(self, member: discord.Member) -> discord.Embed:
        g = member.guild
        text = fill(gget(g.id, "welcome_message") or DEFAULT_WELCOME, member, g)
        emb = discord.Embed(title=f"Welcome to {g.name}!"[:256], description=text[:4096], color=0x57F287)
        emb.set_thumbnail(url=member.display_avatar.url)
        if g.member_count:
            emb.set_footer(text=f"Member #{g.member_count}")
        return emb

    def leave_embed(self, member: discord.Member) -> discord.Embed:
        g = member.guild
        text = fill(gget(g.id, "leave_message") or DEFAULT_LEAVE, member, g)
        emb = discord.Embed(description=text[:4096], color=0x99AAB5)
        emb.set_thumbnail(url=member.display_avatar.url)
        if g.member_count:
            emb.set_footer(text=f"{g.member_count} members now")
        return emb

    def _intent_note(self) -> str:
        return "" if self.bot.intents.members else (
            "\n⚠️ The **Server Members Intent** is off, so join/leave events won't arrive yet. Turn it on in the "
            "Discord Developer Portal → your app → Bot, then restart Anton.")

    # /welcome

    welcome = app_commands.Group(name="welcome", description="Welcome messages for new members (admins).",
                                 default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    @welcome.command(name="channel", description="Where to welcome new members (turns welcomes on).")
    async def welcome_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        gset(interaction.guild_id, "welcome_channel", str(channel.id))
        gset(interaction.guild_id, "welcome_on", "1")
        await interaction.response.send_message(
            f"New members will be welcomed in {channel.mention}. Try `/welcome test` to preview."
            + self._intent_note(), ephemeral=True)

    @welcome.command(name="message", description="Change the welcome text. Leave it empty to reset.")
    @app_commands.describe(text=PLACEHOLDER_HELP)
    async def welcome_message(self, interaction: discord.Interaction,
                              text: Optional[app_commands.Range[str, 1, 1500]] = None):
        gset(interaction.guild_id, "welcome_message", text)
        await interaction.response.send_message(
            "Welcome message updated." if text else "Welcome message reset to the default.", ephemeral=True)

    @welcome.command(name="dm", description="Also DM new members (e.g. rules). Leave it empty to turn DMs off.")
    @app_commands.describe(text=PLACEHOLDER_HELP)
    async def welcome_dm(self, interaction: discord.Interaction,
                         text: Optional[app_commands.Range[str, 1, 1500]] = None):
        gset(interaction.guild_id, "welcome_dm", text)
        await interaction.response.send_message(
            "New members will get that as a DM." if text else "Welcome DMs are off.", ephemeral=True)

    @welcome.command(name="autorole", description="Give new members a role when they join. Leave it empty to stop.")
    async def welcome_autorole(self, interaction: discord.Interaction, role: Optional[discord.Role] = None):
        if role is None:
            gset(interaction.guild_id, "autorole", None)
            return await interaction.response.send_message("Auto-role turned off.", ephemeral=True)
        me = interaction.guild.me
        if role.managed or role.is_default():
            return await interaction.response.send_message("That role can't be handed out.", ephemeral=True)
        if role >= me.top_role or not me.guild_permissions.manage_roles:
            return await interaction.response.send_message(NO_PERMS, ephemeral=True)
        gset(interaction.guild_id, "autorole", str(role.id))
        await interaction.response.send_message(f"New members will get {role.mention}." + self._intent_note(),
                                                ephemeral=True)

    @welcome.command(name="test", description="Preview the welcome message using yourself.")
    async def welcome_test(self, interaction: discord.Interaction):
        member = interaction.user
        dm = gget(interaction.guild_id, "welcome_dm")
        where = gget(interaction.guild_id, "welcome_channel")
        status = "on" if gget(interaction.guild_id, "welcome_on") == "1" else "off"
        info = f"Welcomes are **{status}**" + (f" in <#{where}>" if where else "") + "."
        if dm:
            info += f"\n**DM preview:**\n{fill(dm, member, interaction.guild)[:1500]}"
        await interaction.response.send_message(info, embed=self.welcome_embed(member), ephemeral=True)

    @welcome.command(name="off", description="Stop posting welcome messages.")
    async def welcome_off(self, interaction: discord.Interaction):
        gset(interaction.guild_id, "welcome_on", "0")
        await interaction.response.send_message("Welcome messages are off. `/welcome channel` turns them back on.",
                                                ephemeral=True)

    # /leave

    leave = app_commands.Group(name="leave", description="Messages when someone leaves (admins).",
                               default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    @leave.command(name="channel", description="Where to post when someone leaves (turns leave messages on).")
    @app_commands.describe(channel="Leave empty to use the welcome channel")
    async def leave_channel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        gset(interaction.guild_id, "leave_channel", str(channel.id) if channel else None)
        gset(interaction.guild_id, "leave_on", "1")
        where = channel.mention if channel else "the welcome channel"
        await interaction.response.send_message(f"Leave messages will post in {where}." + self._intent_note(),
                                                ephemeral=True)

    @leave.command(name="message", description="Change the leave text. Leave it empty to reset.")
    @app_commands.describe(text="You can use {name}, {server} and {count}.")
    async def leave_message(self, interaction: discord.Interaction,
                            text: Optional[app_commands.Range[str, 1, 1500]] = None):
        gset(interaction.guild_id, "leave_message", text)
        await interaction.response.send_message(
            "Leave message updated." if text else "Leave message reset to the default.", ephemeral=True)

    @leave.command(name="test", description="Preview the leave message using yourself.")
    async def leave_test(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self.leave_embed(interaction.user), ephemeral=True)

    @leave.command(name="off", description="Stop posting leave messages.")
    async def leave_off(self, interaction: discord.Interaction):
        gset(interaction.guild_id, "leave_on", "0")
        await interaction.response.send_message("Leave messages are off.", ephemeral=True)

    # /colors

    colors = app_commands.Group(name="colors", description="Name color roles (admins).",
                                default_permissions=discord.Permissions(manage_roles=True), guild_only=True)

    @colors.command(name="setup", description="Create or find the color roles and post the picker.")
    @app_commands.describe(channel="Where to post the picker (default: here)")
    async def colors_setup(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        guild = interaction.guild
        me = guild.me
        if not me.guild_permissions.manage_roles:
            return await interaction.response.send_message(NO_PERMS, ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        saved = color_roles(guild.id)
        result: dict[str, int] = {}
        created, reused, problems = [], [], []
        for name, hexval, _emoji in COLORS:
            role = guild.get_role(saved.get(name, 0)) or discord.utils.find(
                lambda r, n=name: r.name.lower() == n.lower(), guild.roles)
            if role is None:
                try:
                    role = await guild.create_role(name=name, colour=discord.Colour(hexval),
                                                   permissions=discord.Permissions.none(),
                                                   reason="Anton color picker")
                    created.append(name)
                except discord.HTTPException as e:
                    problems.append(f"{name}: {e}")
                    continue
            else:
                reused.append(name)
            if role >= me.top_role:
                problems.append(f"**{role.name}** is above my role, so I can't hand it out")
            result[name] = role.id
        gset(guild.id, "color_roles", json.dumps(result))

        # Roles start at the bottom of the list. Move them just under Anton's role so the
        # color actually shows (a colored role higher up would win otherwise).
        if created:
            try:
                positions = {guild.get_role(result[n]): max(me.top_role.position - 1, 1)
                             for n in created if guild.get_role(result[n])}
                await guild.edit_role_positions(positions=positions, reason="Anton color picker")
            except discord.HTTPException:
                problems.append("couldn't move the new roles up the list; drag them up under my role yourself")

        target = channel or interaction.channel
        emb, view = color_panel(guild)
        try:
            await target.send(embed=emb, view=view)
        except discord.HTTPException as e:
            problems.append(f"couldn't post the picker in {target.mention}: {e}")

        lines = [f"Color picker posted in {target.mention}."]
        if created:
            lines.append(f"Created: {', '.join(created)}")
        if reused:
            lines.append(f"Used existing roles: {', '.join(reused)}")
        if problems:
            lines.append("⚠️ " + "\n⚠️ ".join(problems))
        lines.append("\nTip: members only see their color if no other colored role sits above it in the role list.")
        await interaction.followup.send("\n".join(lines)[:2000], ephemeral=True)

    @colors.command(name="post", description="Post the color picker again (e.g. in another channel).")
    async def colors_post(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        if not color_roles(interaction.guild_id):
            return await interaction.response.send_message("Run `/colors setup` first.", ephemeral=True)
        target = channel or interaction.channel
        emb, view = color_panel(interaction.guild)
        await target.send(embed=emb, view=view)
        await interaction.response.send_message(f"Posted in {target.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(ColorButton)
    await bot.add_cog(Community(bot))
