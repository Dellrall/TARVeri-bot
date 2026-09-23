"""
Admin Cog & Interactive Views for Random User Tagging in #general.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands, ui
from discord.ext import commands

if TYPE_CHECKING:
    from tarveri.bot import TARVeriBot
    from tarveri.services.random_tag_service import RandomTagService


class RandomTagConfigModal(ui.Modal, title="⚙️ Configure Random Tagging"):
    max_daily = ui.TextInput(
        label="Max Tags Per Day",
        placeholder="e.g. 5 (Default: 5)",
        default="5",
        required=True,
        max_length=3,
    )
    chance = ui.TextInput(
        label="Trigger Chance (1 in N)",
        placeholder="e.g. 5 (means 1 in 5 chance each tick)",
        default="5",
        required=True,
        max_length=3,
    )
    interval_range = ui.TextInput(
        label="Interval Range (Min - Max minutes)",
        placeholder="e.g. 15-180",
        default="15-180",
        required=True,
        max_length=15,
    )
    words_input = ui.TextInput(
        label="Custom Words (Comma separated)",
        placeholder="e.g. Hey, Cheers to, Shoutout to, Greetings",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, service: RandomTagService, current_config: dict) -> None:
        super().__init__()
        self.service = service
        self.current_config = current_config
        self.max_daily.default = str(current_config.get("max_daily_runs", 5))
        self.chance.default = str(current_config.get("chance_denominator", 5))
        min_i = current_config.get("min_interval_minutes", 15)
        max_i = current_config.get("max_interval_minutes", 180)
        self.interval_range.default = f"{min_i}-{max_i}"
        self.words_input.default = ", ".join(current_config.get("words_list", ["Hey", "Cheers to"]))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return

        try:
            max_daily_val = max(1, int(self.max_daily.value.strip()))
            chance_val = max(1, int(self.chance.value.strip()))
        except ValueError:
            await interaction.response.send_message(
                "❌ Max daily and Chance must be valid integers.", ephemeral=True
            )
            return

        # Parse intervals
        min_interval, max_interval = 15, 180
        if "-" in self.interval_range.value:
            parts = self.interval_range.value.split("-")
            try:
                min_interval = max(5, int(parts[0].strip()))
                max_interval = max(min_interval, int(parts[1].strip()))
            except ValueError:
                pass

        # Parse words list
        raw_words = [w.strip() for w in self.words_input.value.split(",") if w.strip()]
        if not raw_words:
            raw_words = ["Hey"]

        await self.service.update_config(
            interaction.guild.id,
            max_daily_runs=max_daily_val,
            chance_denominator=chance_val,
            min_interval_minutes=min_interval,
            max_interval_minutes=max_interval,
            words_list=raw_words,
        )

        await interaction.response.send_message(
            f"✅ **Random Tagging settings updated for {interaction.guild.name}**:\n"
            f"• **Target:** Strictly `#general`\n"
            f"• **Daily Cap:** `{max_daily_val}` triggers/day\n"
            f"• **Chance:** `1 in {chance_val}` per check\n"
            f"• **Random Interval:** `{min_interval}` to `{max_interval}` minutes\n"
            f"• **Words Pool ({len(raw_words)}):** {', '.join(f'`{w}`' for w in raw_words[:6])}{'...' if len(raw_words) > 6 else ''}",
            ephemeral=True,
        )


class RandomTagDashboardView(ui.View):
    def __init__(self, service: RandomTagService, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.service = service
        self.guild_id = guild_id

    @ui.button(label="Toggle Enable/Disable", style=discord.ButtonStyle.primary, emoji="🔄", row=0)
    async def toggle_service(self, interaction: discord.Interaction, button: ui.Button) -> None:
        config = await self.service.get_config(self.guild_id)
        new_state = not config["is_enabled"]
        await self.service.update_config(self.guild_id, is_enabled=new_state)

        status_text = "🟢 **Enabled**" if new_state else "🔴 **Disabled**"
        await interaction.response.send_message(
            f"Random Tagging is now {status_text} in this server.", ephemeral=True
        )

    @ui.button(label="Configure Settings & Words", style=discord.ButtonStyle.secondary, emoji="⚙️", row=0)
    async def configure_modal(self, interaction: discord.Interaction, button: ui.Button) -> None:
        config = await self.service.get_config(self.guild_id)
        modal = RandomTagConfigModal(self.service, config)
        await interaction.response.send_modal(modal)

    @ui.button(label="Rotation Mode (Random / Round Robin)", style=discord.ButtonStyle.secondary, emoji="🔀", row=1)
    async def toggle_mode(self, interaction: discord.Interaction, button: ui.Button) -> None:
        config = await self.service.get_config(self.guild_id)
        new_mode = "round_robin" if config["word_rotation_mode"] == "random" else "random"
        await self.service.update_config(self.guild_id, word_rotation_mode=new_mode)

        mode_name = "Round Robin (Sequential)" if new_mode == "round_robin" else "Uniform Random"
        await interaction.response.send_message(
            f"Word rotation mode set to: **{mode_name}**.", ephemeral=True
        )


class RandomTagCog(commands.Cog, name="RandomTag"):
    """Admin configuration for randomized user tagging in #general."""

    randomtag_group = app_commands.Group(
        name="randomtag",
        description="Admin controls for randomized user tagging in #general",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: TARVeriBot, service: RandomTagService):
        self.bot = bot
        self.service = service

    @randomtag_group.command(
        name="dashboard",
        description="Open the Admin Dashboard for Random Tagging",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def dashboard(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        config = await self.service.get_config(interaction.guild.id)
        status_emoji = "🟢 Enabled" if config["is_enabled"] else "🔴 Disabled"
        words_preview = ", ".join(f"`{w}`" for w in config["words_list"][:8])

        embed = discord.Embed(
            title=f"🎲 Random Tagging Control Panel — {interaction.guild.name}",
            color=discord.Color.blue() if config["is_enabled"] else discord.Color.greyple(),
            description=(
                f"**Status:** {status_emoji}\n"
                f"**Target Channel:** Strictly `#general`\n"
                f"**Daily Limit:** `{config['current_daily_runs']} / {config['max_daily_runs']}` tags sent today\n"
                f"**Odds:** `1 in {config['chance_denominator']}` chance per tick\n"
                f"**Interval Window:** `{config['min_interval_minutes']}` - `{config['max_interval_minutes']}` mins (highly randomized with jitter)\n"
                f"**Rotation Mode:** `{config['word_rotation_mode'].replace('_', ' ').title()}`\n\n"
                f"**Configured Words ({len(config['words_list'])}):**\n{words_preview}"
            ),
        )
        embed.set_footer(text="Click the buttons below to toggle or customize settings.")

        view = RandomTagDashboardView(self.service, interaction.guild.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
