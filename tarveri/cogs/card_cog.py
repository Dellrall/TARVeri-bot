"""
CardCog: Digital Student and Guest ID Card slash commands and UI interactions.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from tarveri.cogs.verification_cog import StudentLifecycleResolutionView
from tarveri.database import Database
from tarveri.services.card_service import CardService
from tarveri.services.verification_service import VerificationService
from tarveri.utils import schedule_ttl_delete

logger = logging.getLogger("tarveri")


class CardCog(commands.Cog, name="CampusCard"):
    def __init__(
        self,
        bot: discord.Client,
        db: Database,
        card_service: CardService,
        verification_service: VerificationService | None = None,
    ):
        self.bot = bot
        self.db = db
        self.card_service = card_service
        self.verification_service = verification_service

        # Register User Context Menu command (Right-click Member -> Apps -> View Campus Card)
        self.context_menu = app_commands.ContextMenu(
            name="View Campus Card",
            callback=self.view_card_context_menu,
        )
        self.bot.tree.add_command(self.context_menu)

    def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.context_menu.name, type=self.context_menu.type)

    async def _send_card_response(
        self,
        interaction: discord.Interaction,
        target_member: discord.Member | discord.User,
        is_public: bool = True,
    ) -> None:
        """Helper to render and dispatch card image response."""
        ephemeral = not is_public
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)

        guild = interaction.guild
        if not guild:
            # Fallback for DM interaction
            if hasattr(self.bot, "guilds") and self.bot.guilds:
                guild = self.bot.guilds[0]
            else:
                guild = None

        # Fetch member avatar bytes
        avatar_bytes = None
        try:
            if hasattr(target_member, "display_avatar"):
                avatar_bytes = await target_member.display_avatar.read()
            elif hasattr(target_member, "avatar") and target_member.avatar:
                avatar_bytes = await target_member.avatar.read()
        except Exception as e:
            logger.debug(f"Could not read avatar for {target_member}: {e}")
            avatar_bytes = None

        try:
            card_data = await self.card_service.get_user_card_data(guild, target_member)
            buf = await self.card_service.render_card_from_data(card_data, avatar_bytes)
            file = discord.File(fp=buf, filename=f"tarveri_card_{target_member.id}.png")

            embed_color = self.card_service.get_card_color(card_data)
            embed = discord.Embed(
                title=f"🪪 TARUMT Campus ID • {target_member.display_name}",
                color=embed_color,
            )
            embed.set_image(url=f"attachment://tarveri_card_{target_member.id}.png")
            embed.set_footer(text="TARVeri Digital Student & Guest Passport • Official Verification")

            view = None
            if (
                card_data.get("is_card_expired")
                and target_member.id == interaction.user.id
                and self.verification_service
            ):
                view = StudentLifecycleResolutionView(self.verification_service, self.db)
                embed.description = (
                    "⚠️ **Student Card Validity Expired.** Please choose an option below to update your status:"
                )

            if view is not None:
                await interaction.followup.send(embed=embed, file=file, view=view, ephemeral=ephemeral)
            else:
                await interaction.followup.send(embed=embed, file=file, ephemeral=ephemeral)

            if ephemeral:
                schedule_ttl_delete(interaction, delay=180.0)

            await self.db.log(
                "INFO",
                "CARD_GENERATED",
                f"Generated digital campus card for {target_member} (ID: {target_member.id}) in '{guild.name if guild else 'DM'}'",
                guild=guild,
                user_id=target_member.id,
            )
        except Exception as e:
            logger.error(f"Failed to generate card for {target_member}: {e}", exc_info=True)
            await interaction.followup.send(
                f"❌ Failed to generate digital campus card: {e}",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=30.0)

    @app_commands.command(
        name="card",
        description="Display your official TARUMT Digital Campus ID card and verified badges.",
    )
    @app_commands.describe(
        member="Optional member to view (leave blank to view your own card)",
        hidden="Set to True to make the card visible only to you (default: False / public)",
    )
    async def card(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        hidden: bool = False,
    ) -> None:
        """Slash command to view your own or another member's digital student card."""
        target = member or interaction.user
        await self._send_card_response(interaction, target, is_public=not hidden)

    async def view_card_context_menu(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Context menu handler when right-clicking a user -> Apps -> View Campus Card."""
        await self._send_card_response(interaction, member, is_public=True)
