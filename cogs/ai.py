"""/ask: answers questions with Claude, using live web search when needed.

Needs ANTHROPIC_API_KEY in .env. Until it's set, /ask just explains how to turn it on.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from core import config, db

SYSTEM = (
    "You are Anton, a friendly bot in a Discord server for comic book and graphic novel collectors. "
    "Answer the member's question helpfully and concisely: aim for under 200 words, use short paragraphs "
    "or a few bullet points, and keep Discord markdown simple. Search the web when the answer depends on "
    "current information (prices, release dates, news, availability). If you're not sure, say so. "
    "Today's date is {today}."
)


class AI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = None
        if config.AI_ENABLED:
            try:
                import anthropic
                self.client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
            except ImportError:
                print("/ask: the 'anthropic' package isn't installed (pip install -r requirements.txt)")

    async def _limit_message(self, user_id: int) -> str | None:
        last = await asyncio.to_thread(db.last_ai_use, user_id)
        if last:
            waited = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
            if waited < config.AI_USER_COOLDOWN_SEC:
                return f"Give me {config.AI_USER_COOLDOWN_SEC - waited:.0f} more seconds before the next question."
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        if await asyncio.to_thread(db.ai_usage_since, day_start, user_id) >= config.AI_DAILY_LIMIT_PER_USER:
            return f"You've used your {config.AI_DAILY_LIMIT_PER_USER} questions for today. Try again tomorrow!"
        if await asyncio.to_thread(db.ai_usage_since, day_start) >= config.AI_DAILY_LIMIT:
            return "The server has used up today's AI questions. Try again tomorrow!"
        return None

    async def _ask_claude(self, question: str) -> tuple[str, list[tuple[str, str]], int]:
        """Returns (answer text, [(title, url)] sources, number of web searches)."""
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": config.AI_MAX_SEARCHES}]
        messages: list[dict] = [{"role": "user", "content": question}]
        system = SYSTEM.format(today=datetime.now(timezone.utc).strftime("%B %d, %Y"))
        text_parts: list[str] = []
        sources: dict[str, str] = {}
        searches = 0
        for _ in range(4):  # long searches can pause; continue them a few times
            resp = await self.client.messages.create(
                model=config.AI_MODEL, max_tokens=1024, system=system, messages=messages, tools=tools,
            )
            for block in resp.content:
                btype = getattr(block, "type", "")
                if btype == "text":
                    text_parts.append(block.text)
                    for c in getattr(block, "citations", None) or []:
                        url = getattr(c, "url", None)
                        if url and url not in sources:
                            sources[url] = getattr(c, "title", None) or url
                elif btype == "server_tool_use":
                    searches += 1
            if resp.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp.content})
        answer = "".join(text_parts).strip() or "I couldn't come up with an answer to that one."
        return answer, [(t, u) for u, t in sources.items()], searches

    @app_commands.command(name="ask", description="Ask Anton anything. It can search the web for current info.")
    @app_commands.describe(question="What do you want to know?")
    async def ask(self, interaction: discord.Interaction, question: app_commands.Range[str, 3, 500]):
        if not config.AI_ENABLED:
            return await interaction.response.send_message(
                "🤖 AI isn't set up yet. The bot owner needs to put a Claude API key in `.env` "
                "(`ANTHROPIC_API_KEY`) and restart Anton.", ephemeral=True)
        if self.client is None:
            return await interaction.response.send_message(
                "🤖 AI is configured but the `anthropic` package isn't installed on the bot's computer.", ephemeral=True)
        limit = await self._limit_message(interaction.user.id)
        if limit:
            return await interaction.response.send_message(limit, ephemeral=True)

        await interaction.response.defer(thinking=True)
        await asyncio.to_thread(db.log_ai_use, interaction.user.id)
        import anthropic
        try:
            answer, sources, searches = await asyncio.wait_for(self._ask_claude(question), timeout=120)
        except asyncio.TimeoutError:
            return await interaction.followup.send("That took too long. Try a simpler question?")
        except anthropic.AuthenticationError:
            return await interaction.followup.send("🤖 The API key in `.env` was rejected. The bot owner needs to check it.")
        except anthropic.RateLimitError:
            return await interaction.followup.send("🤖 I'm being rate limited right now. Try again in a minute.")
        except anthropic.APIStatusError as e:
            print(f"/ask API error {e.status_code}: {e}")
            return await interaction.followup.send("🤖 The AI service returned an error. Try again later.")
        except anthropic.APIConnectionError:
            return await interaction.followup.send("🤖 Couldn't reach the AI service. Try again later.")

        emb = discord.Embed(title=question[:256], description=answer[:4096], color=0xD97757)
        if sources:
            emb.add_field(name="Sources",
                          value="\n".join(f"• [{t[:60]}]({u})" for t, u in sources[:5])[:1024], inline=False)
        emb.set_footer(text=f"Asked by {interaction.user.display_name} · Claude"
                            + (f" · {searches} web search{'es' if searches != 1 else ''}" if searches else ""))
        await interaction.followup.send(embed=emb)


async def setup(bot: commands.Bot):
    await bot.add_cog(AI(bot))
