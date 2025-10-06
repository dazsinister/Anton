from __future__ import annotations
from typing import Optional, Dict, List
import discord
from discord import app_commands
from discord.ext import commands

class PollView(discord.ui.View):
    def __init__(self, owner_id: int, question: str, options: List[str], duration_sec: Optional[int] = None):
        super().__init__(timeout=duration_sec)
        self.owner_id = owner_id
        self.question = question
        self.options = options[:5]
        self.votes: Dict[int, int] = {}  
        self.counts = [0] * len(self.options)
        self.message: Optional[discord.Message] = None  

        for idx, label in enumerate(self.options):
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"poll:{idx}"
            )

            async def on_click(interaction: discord.Interaction, index=idx):
                user_id = interaction.user.id
                prev = self.votes.get(user_id)

                if prev == index:
                    await interaction.response.send_message("You already voted for that option.", ephemeral=True)
                    return

                if prev is not None:
                    self.counts[prev] = max(0, self.counts[prev] - 1)

                self.votes[user_id] = index
                self.counts[index] += 1

                await self._edit_embed(interaction)

            btn.callback = on_click  
            self.add_item(btn)

    def build_embed(self) -> discord.Embed:
        total = sum(self.counts)
        emb = discord.Embed(
            title="📊 Poll",
            description=self.question,
            color=0x5865F2
        )
        for i, opt in enumerate(self.options):
            count = self.counts[i]
            pct = f"{(count / total * 100):.0f}%" if total > 0 else "0%"
            emb.add_field(name=f"{i+1}. {opt}", value=f"**{count}** votes ({pct})", inline=False)
        emb.set_footer(text=f"Total voters: {len(self.votes)} • Poll owner can close early")
        return emb

    async def _edit_embed(self, interaction: discord.Interaction):
        emb = self.build_embed()
        if self.message:
            await self.message.edit(embed=emb, view=self)
            if not interaction.response.is_done():
                await interaction.response.defer()  
        else:
            if interaction.response.is_done():
                await interaction.message.edit(embed=emb, view=self)  
            else:
                await interaction.response.edit_message(embed=emb, view=self)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
                child.style = discord.ButtonStyle.secondary
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except Exception:
                pass

active_polls: Dict[int, PollView] = {}

class PollCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="poll", description="Create a poll with up to 5 options.")
    @app_commands.describe(
        question="What are you asking?",
        option1="Option 1",
        option2="Option 2",
        option3="Option 3 (optional)",
        option4="Option 4 (optional)",
        option5="Option 5 (optional)",
        duration_minutes="How long the poll should stay open (optional)"
    )
    async def poll_cmd(
        self,
        interaction: discord.Interaction,
        question: str,
        option1: str,
        option2: str,
        option3: Optional[str] = None,
        option4: Optional[str] = None,
        option5: Optional[str] = None,
        duration_minutes: Optional[int] = None
    ):
        await interaction.response.defer(ephemeral=True)
        options = [o for o in [option1, option2, option3, option4, option5] if o]
        if len(options) < 2:
            return await interaction.followup.send("Please provide at least **two** options.", ephemeral=True)
        if len(options) > 5:
            options = options[:5]

        channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            return await interaction.followup.send("I can’t post a poll in this location.", ephemeral=True)

        duration_sec = None
        if duration_minutes is not None:
            if duration_minutes < 1 or duration_minutes > 7 * 24 * 60:
                return await interaction.followup.send("Duration must be between 1 minute and 7 days.", ephemeral=True)
            duration_sec = duration_minutes * 60

        view = PollView(owner_id=interaction.user.id, question=question, options=options, duration_sec=duration_sec)
        embed = view.build_embed()
        msg = await channel.send(embed=embed, view=view)
        view.message = msg  
        active_polls[msg.id] = view

        await interaction.followup.send("Poll posted ✅", ephemeral=True)

    @app_commands.command(name="pollclose", description="Close a poll early (admin or poll owner).")
    @app_commands.describe(message_id="The message ID of the poll to close")
    async def pollclose_cmd(self, interaction: discord.Interaction, message_id: str):
        await interaction.response.defer(ephemeral=True)

        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.followup.send("Invalid message_id.", ephemeral=True)

        view = active_polls.get(mid)
        if view is None:
            return await interaction.followup.send("That poll is not active (maybe already closed or bot restarted).", ephemeral=True)

        is_owner = (interaction.user.id == view.owner_id)
        is_admin = False
        if interaction.guild:
            member = interaction.guild.get_member(interaction.user.id)
            if member:
                is_admin = member.guild_permissions.administrator

        if not (is_owner or is_admin):
            return await interaction.followup.send("Only the poll owner or an admin can close this poll.", ephemeral=True)

        for child in view.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
                child.style = discord.ButtonStyle.secondary

        try:
            if view.message:
                await view.message.edit(embed=view.build_embed(), view=view)
            else:
                channel = interaction.channel
                if channel:
                    msg = await channel.fetch_message(mid)
                    await msg.edit(embed=view.build_embed(), view=view)
        except Exception:
            pass

        active_polls.pop(mid, None)
        await interaction.followup.send("Poll closed ✅", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(PollCog(bot))
