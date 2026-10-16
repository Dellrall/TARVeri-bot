"""
Discord UI Views and Modals for Administration and Security Authorizations.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from tarveri.config import get_configured_tz

if TYPE_CHECKING:
    from tarveri.services.verification_service import VerificationService

logger = logging.getLogger("tarveri")


class MassRevocationApprovalView(discord.ui.View):
    """
    Interactive approval view dispatched to #tarveri-log when a mass role revocation is intercepted.
    """

    def __init__(self, service: VerificationService | None = None):
        super().__init__(timeout=None)
        self.service = service

    @discord.ui.button(
        label="Approve & Execute Revocation",
        style=discord.ButtonStyle.danger,
        emoji="⚠️",
        custom_id="tarveri:mass_revocation:approve",
    )
    async def on_approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Server context required.", ephemeral=True)
            return

        member = interaction.user
        perms = getattr(member, "guild_permissions", None)
        if not perms or not (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)):
            await interaction.response.send_message(
                "❌ Only administrators can authorize mass role revocations.",
                ephemeral=True,
            )
            return

        if not self.service:
            cog = interaction.client.get_cog("Admin")
            self.service = getattr(cog, "service", None)

        if not self.service:
            await interaction.response.send_message("❌ Service unavailable.", ephemeral=True)
            return

        await interaction.response.defer()

        active = await self.service.db.get_active_pending_mass_action_for_guild(interaction.guild.id)
        if not active:
            await interaction.followup.send(
                "⚠️ No active pending mass revocation found for this server.",
                ephemeral=True,
            )
            return

        action_id = active["action_id"]
        success, msg = await self.service.execute_approved_mass_revocation(
            interaction.guild, action_id, admin=interaction.user
        )

        embed = discord.Embed(
            title="✅ [Authorized] Mass Role Revocation Executed",
            description=f"Mass role revocation was approved and executed by {interaction.user.mention}.",
            color=discord.Color.green(),
            timestamp=datetime.now(get_configured_tz()),
        )
        embed.add_field(name="📋 Action ID", value=f"`{action_id}`", inline=True)
        embed.add_field(name="👥 Affected Users", value=f"{len(active['user_ids'])} members", inline=True)
        embed.add_field(name="📝 Status", value="`EXECUTED`", inline=True)
        embed.set_footer(text="TARVeri Security Guard • Mass Action Authorization")

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        try:
            if interaction.message:
                await interaction.message.edit(embed=embed, view=self)
        except Exception as exc:
            logger.debug(f"Could not edit mass revocation interaction message: {exc}")

        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(
        label="Reject & Keep Roles",
        style=discord.ButtonStyle.secondary,
        emoji="🛡️",
        custom_id="tarveri:mass_revocation:reject",
    )
    async def on_reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Server context required.", ephemeral=True)
            return

        member = interaction.user
        perms = getattr(member, "guild_permissions", None)
        if not perms or not (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)):
            await interaction.response.send_message(
                "❌ Only administrators can reject mass role revocations.",
                ephemeral=True,
            )
            return

        if not self.service:
            cog = interaction.client.get_cog("Admin")
            self.service = getattr(cog, "service", None)

        if not self.service:
            await interaction.response.send_message("❌ Service unavailable.", ephemeral=True)
            return

        await interaction.response.defer()

        active = await self.service.db.get_active_pending_mass_action_for_guild(interaction.guild.id)
        if not active:
            await interaction.followup.send(
                "⚠️ No active pending mass revocation found for this server.",
                ephemeral=True,
            )
            return

        action_id = active["action_id"]
        success, msg = await self.service.reject_mass_revocation(interaction.guild, action_id, admin=interaction.user)

        embed = discord.Embed(
            title="❌ [Cancelled] Mass Role Revocation Rejected",
            description=f"Mass role revocation was rejected by {interaction.user.mention}. All member roles remain intact.",
            color=discord.Color.dark_grey(),
            timestamp=datetime.now(get_configured_tz()),
        )
        embed.add_field(name="📋 Action ID", value=f"`{action_id}`", inline=True)
        embed.add_field(name="👥 Affected Users", value=f"{len(active['user_ids'])} members (Protected)", inline=True)
        embed.add_field(name="📝 Status", value="`REJECTED`", inline=True)
        embed.set_footer(text="TARVeri Security Guard • Mass Action Authorization")

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        try:
            if interaction.message:
                await interaction.message.edit(embed=embed, view=self)
        except Exception as exc:
            logger.debug(f"Could not edit mass revocation interaction message: {exc}")

        await interaction.followup.send(msg, ephemeral=True)
