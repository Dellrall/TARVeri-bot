"""
Guest cog providing UI modals, persistent gateway buttons, referral code commands,
and private thread review orchestration.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from tarveri.config import (
    FACULTY_ROLE_NAMES,
)
from tarveri.database import Database
from tarveri.services.guest_service import GuestService
from tarveri.services.verification_service import VerificationService
from tarveri.utils import schedule_ttl_delete
from tarveri.views.guest_views import (
    CloseTicketModal,
    GuestApplicationModal,
    GuestReviewThreadView,
    ReferralEntryModal,
    RejectReasonModal,
    StudentVerificationModal,
    VerificationGatewayView,
    VouchModal,
    build_gateway_panel_embed,
    build_review_embed,
    get_admin_role_mention,
    get_admin_role_or_fallback,
    is_admin_or_has_role,
)

logger = logging.getLogger("tarveri")

__all__ = [
    "CloseTicketModal",
    "GuestApplicationModal",
    "GuestCog",
    "GuestReviewThreadView",
    "ReferralEntryModal",
    "RejectReasonModal",
    "StudentVerificationModal",
    "VerificationGatewayView",
    "VouchModal",
    "build_gateway_panel_embed",
    "build_review_embed",
    "get_admin_role_mention",
    "get_admin_role_or_fallback",
    "is_admin_or_has_role",
]


class GuestCog(commands.Cog, name="Guest"):
    """Handles guest verification, referral codes, and approval tickets."""

    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        guest_service: GuestService,
        verification_service: VerificationService,
    ) -> None:
        self.bot = bot
        self.db = db
        self.guest_service = guest_service
        self.verification_service = verification_service

    referral = app_commands.Group(name="referral", description="Commands to generate and manage guest referral codes")

    @referral.command(
        name="generate", description="Generate a guest referral code for a friend (Verified Students only)."
    )
    @app_commands.describe(ttl_hours="How many hours until the code expires (default: 48, max: 168)")
    async def referral_generate(self, interaction: discord.Interaction, ttl_hours: int = 48) -> None:
        """Generates a guest referral code."""
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This command can only be used inside a server.", ephemeral=True)
            return

        # Check if user is a verified student
        is_verified = bool(await self.db.get_verification_by_user(interaction.user.id))
        if not is_verified:
            # Also check if member has any faculty role in Discord
            user_roles = getattr(interaction.user, "roles", [])
            has_faculty_role = any(
                VerificationService._match_faculty_role_in_list([r], fac) is not None
                for fac in FACULTY_ROLE_NAMES
                for r in user_roles
            )
            is_verified = has_faculty_role

        if not is_verified:
            await interaction.response.send_message(
                "❌ Only verified TARUMT students can generate referral codes. Please verify your student status first with `/verify`.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        hours = max(1, min(ttl_hours, 168))
        await interaction.response.defer(ephemeral=True)

        success, code_or_err = await self.guest_service.create_referral_code(
            guild_id=interaction.guild.id,
            referrer_user=interaction.user,
            ttl_hours=hours,
        )

        if success:
            embed = discord.Embed(
                title="🎟️ Guest Referral Code Generated",
                description=(
                    f"Here is your single-use referral code for **{interaction.guild.name}**:\n\n"
                    f"### `{code_or_err}`\n\n"
                    f"⏱️ **Expires In:** {hours} hour(s)\n"
                    f"⚠️ **Note:** Give this code to your friend. When they join and submit the code, a private "
                    f"approval thread will open with staff where you can vouch for them."
                ),
                color=discord.Color.blue(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(code_or_err, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @referral.command(name="list", description="View your active and past referral codes.")
    async def referral_list(self, interaction: discord.Interaction) -> None:
        """Lists referral codes created by the caller in this server."""
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used inside a server.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        codes = await self.db.get_user_referrals(interaction.guild.id, interaction.user.id, limit=10)

        if not codes:
            await interaction.followup.send(
                "ℹ️ You have not generated any referral codes in this server yet. Use `/referral generate` to create one.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        embed = discord.Embed(
            title=f"🎟️ Your Referral Codes ({interaction.guild.name})",
            color=discord.Color.blue(),
        )
        for item in codes:
            status_emoji = (
                "🟢" if item["status"] == "ACTIVE" else ("🟡" if item["status"] == "PENDING_APPROVAL" else "⚪")
            )
            used_str = f" • Used by <@{item['used_by_discord_id']}>" if item.get("used_by_discord_id") else ""
            embed.add_field(
                name=f"`{item['code']}` {status_emoji} {item['status']}",
                value=f"Expires: `{item['expires_at']}`{used_str}",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(
        name="send_gateway_panel",
        description="Send the interactive 3-button verification gateway panel to a channel.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Target channel (defaults to current channel)")
    async def send_gateway_panel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        """Posts the persistent verification gateway panel."""
        if not is_admin_or_has_role(interaction, self.guest_service.admin_role_name):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        target_ch = channel or interaction.channel
        if not isinstance(target_ch, discord.TextChannel):
            await interaction.response.send_message("❌ Target must be a text channel.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        db = getattr(self.verification_service, "db", None) or self.db
        email_required = (
            await db.is_guild_email_verification_enabled(interaction.guild.id) if interaction.guild and db else False
        )
        guild_name = interaction.guild.name if interaction.guild else "the Server"
        embed = build_gateway_panel_embed(guild_name=guild_name, require_email=email_required)

        view = VerificationGatewayView(self.verification_service, self.guest_service)
        try:
            await target_ch.send(embed=embed, view=view)
            await interaction.followup.send(
                f"✅ Verification gateway panel posted to {target_ch.mention}!", ephemeral=True
            )
        except (discord.HTTPException, discord.Forbidden) as e:
            await interaction.followup.send(f"❌ Failed to send gateway panel: {e}", ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Handles guest role revocation and cleanup when a member leaves, is kicked, or removed."""
        await self.guest_service.handle_member_leave_or_ban(member.guild, member, is_ban=False)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member) -> None:
        """Handles guest role revocation and cleanup when a member is banned."""
        await self.guest_service.handle_member_leave_or_ban(guild, user, is_ban=True)
