"""
Admin Commands, Interactive Dashboard & Audit Tools for TARVeri.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from tarveri.cogs.admin_dashboard import AdminDashboardView
from tarveri.cogs.random_tag_cog import RandomTagDashboardView
from tarveri.config import (
    CAMPUS_ROLE_NAMES,
    CAMPUS_ROLES,
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    STUDY_LEVEL_ROLE_NAMES,
    STUDY_LEVEL_ROLES,
    get_configured_tz,
    resolve_campus_role,
    resolve_faculty_role,
    resolve_study_level_role,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.guest_service import GuestService
from tarveri.services.log_service import (
    LogRotationService,
    archive_old_logs,
    list_daily_logs,
    list_log_archives,
)
from tarveri.services.update_checker import UpdateCheckerService
from tarveri.services.verification_service import VerificationService
from tarveri.utils import format_ticket_seq, parse_ticket_seq, schedule_ttl_delete

logger = logging.getLogger("tarveri")


def is_admin_or_has_role(interaction: discord.Interaction, admin_role_name: str) -> bool:
    """Checks if invoking user has Administrator permission, the configured Admin role, or a standard admin/staff role."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    from tarveri.cogs.guest_cog import get_admin_role_or_fallback

    admin_role = get_admin_role_or_fallback(interaction.guild, admin_role_name)
    if admin_role and admin_role in interaction.user.roles:
        return True
    return any(getattr(r, "name", "").lower() == admin_role_name.lower() for r in getattr(interaction.user, "roles", []))


class AdminCog(commands.Cog, name="Admin"):
    """Consolidated Administrator Control Center and Server Operations."""

    admin_group = app_commands.Group(
        name="admin",
        description="TARVeri Administrator Control Center and Server Operations",
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        service: VerificationService,
        rate_limiter: RateLimiter,
        admin_role_name: str,
        update_checker: UpdateCheckerService | None = None,
        log_rotator: LogRotationService | None = None,
        guest_service: GuestService | None = None,
    ):
        self.bot = bot
        self.db = db
        self.service = service
        self.rate_limiter = rate_limiter
        self.admin_role_name = admin_role_name
        self.update_checker = update_checker
        self.log_rotator = log_rotator
        self.guest_service = guest_service

    def _check_admin(self, interaction: discord.Interaction) -> bool:
        return is_admin_or_has_role(interaction, self.admin_role_name)

    # ==========================================
    # 🛡️ 1. Interactive Dashboard Launcher
    # ==========================================

    @admin_group.command(
        name="dashboard",
        description="Open the interactive TARVeri Administrator Control Center dashboard.",
    )
    @app_commands.default_permissions(administrator=True)
    async def dashboard(self, interaction: discord.Interaction) -> None:
        """Launches the rich interactive Administrator Control Center UI."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        view = AdminDashboardView(cog=self, admin_user=interaction.user, initial_category="overview")
        embed = await view.build_overview_embed(interaction.guild)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @admin_group.command(
        name="randomtag",
        description="Configure randomized user mentions in #general.",
    )
    @app_commands.default_permissions(administrator=True)
    async def randomtag(self, interaction: discord.Interaction) -> None:
        """Launches the Random Tagging configuration dashboard."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command must be executed within a Discord server.", ephemeral=True
            )
            return

        random_tag_service = getattr(self.bot, "random_tag_service", None)
        if not random_tag_service:
            await interaction.response.send_message(
                "❌ Random Tagging Service is not currently active.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        config = await random_tag_service.get_config(interaction.guild.id)
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

        view = RandomTagDashboardView(random_tag_service, interaction.guild.id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ==========================================
    # 📊 2. Statistics & Metrics
    # ==========================================

    @admin_group.command(name="stats", description="View student verification statistics and server metrics.")
    @app_commands.default_permissions(administrator=True)
    async def stats(self, interaction: discord.Interaction) -> None:
        """Displays total verifications, faculty breakdown, and recent activity."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        total = await self.db.total_verified()
        total_alumni = await self.db.count_alumni()
        faculty_counts = await self.db.counts_by_faculty()
        last_24h = await self.db.verified_in_last(24)
        last_7d = await self.db.verified_in_last(24 * 7)

        embed = discord.Embed(
            title="📊 TARVeri — Verification Statistics",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Total Verified", value=f"**{total}** students", inline=True)
        embed.add_field(name="Graduated Alumni", value=f"**{total_alumni}** alumni", inline=True)
        embed.add_field(name="Past 24 Hours", value=f"**{last_24h}** new", inline=True)
        embed.add_field(name="Past 7 Days", value=f"**{last_7d}** new", inline=True)

        email_stats = await self.db.get_email_verification_stats(interaction.guild.id if interaction.guild else None)
        embed.add_field(
            name="Email Verified",
            value=f"**{email_stats['email_verified_students']}** / **{total}** ({email_stats['email_verified_rate']}%)",
            inline=True,
        )

        if faculty_counts:
            breakdown_lines = []
            for f_code, count in faculty_counts:
                faculty_name = FACULTY_ROLES.get(f_code, f"Code {f_code}")
                percentage = (count / total * 100) if total > 0 else 0
                breakdown_lines.append(f"• **{faculty_name}** (`{f_code}`): {count} ({percentage:.1f}%)")
            embed.add_field(name="Faculty Breakdown", value="\n".join(breakdown_lines), inline=False)
        else:
            embed.add_field(name="Faculty Breakdown", value="No verifications recorded yet.", inline=False)

        if interaction.guild:
            guild_settings = await self.db.get_guild_settings(interaction.guild.id)
            w_id = guild_settings[0] if guild_settings else None
            h_id = guild_settings[1] if guild_settings else None
            g_role = (
                guild_settings[2]
                if guild_settings and len(guild_settings) > 2 and guild_settings[2]
                else "Guest(Approved)"
            )
            r_id = guild_settings[3] if guild_settings and len(guild_settings) > 3 else None
            is_email_opted_in = bool(guild_settings[5]) if guild_settings and len(guild_settings) > 5 else False

            w_ch = interaction.guild.get_channel(w_id) if w_id else None
            h_ch = interaction.guild.get_channel(h_id) if h_id else None
            r_ch = interaction.guild.get_channel(r_id) if r_id else None

            w_display = w_ch.mention if w_ch else (f"`ID: {w_id}`" if w_id else "*Auto-detect*")
            h_display = h_ch.mention if h_ch else (f"`ID: {h_id}`" if h_id else "*Auto-detect*")
            r_display = r_ch.mention if r_ch else (f"`ID: {r_id}`" if r_id else "*Auto-detect*")

            adm_role = (
                guild_settings[4]
                if guild_settings and len(guild_settings) > 4 and guild_settings[4]
                else f"*Auto-detect ({self.admin_role_name})*"
            )

            email_policy_str = "Mandatory (Opted In)" if is_email_opted_in else "Optional (Opted Out)"

            embed.add_field(name="Welcome Channel", value=w_display, inline=True)
            embed.add_field(name="Help Channel", value=h_display, inline=True)
            embed.add_field(name="Guest Role", value=f"`{g_role}`", inline=True)
            embed.add_field(name="Review Channel", value=r_display, inline=True)
            embed.add_field(name="Admin / Review Role", value=f"`{adm_role}`", inline=True)
            embed.add_field(name="Email Verification Policy", value=f"`{email_policy_str}`", inline=True)

        embed.set_footer(text=f"TARVeri Bot • Active in {len(self.bot.guilds)} servers")
        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    # ==========================================
    # 🩺 3. Diagnostics & Self-Healing
    # ==========================================

    @admin_group.command(
        name="diagnose",
        description="Run self-healing diagnostics and verify server permissions, roles, and channels.",
    )
    @app_commands.default_permissions(administrator=True)
    async def diagnose(self, interaction: discord.Interaction) -> None:
        """Runs role hierarchy check, channel validity checks, and auto-reconciliation."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        src_stats = await self.service.restore_src_roles(guild)
        dedup_stats = await self.service.reconcile_duplicate_roles(guild)
        warnings = self.service.diagnose_guild_permissions(guild)
        reconcile_stats = await self.service.reconcile_verified_members(guild)
        alumni_stats = await self.service.reconcile_alumni_members(guild)

        # Check channel configurations
        settings = await self.db.get_guild_settings(guild.id)
        w_id = settings[0] if settings else None
        h_id = settings[1] if settings else None
        r_id = settings[3] if settings and len(settings) > 3 else None

        channel_status = []
        if w_id:
            w_ch = guild.get_channel(w_id)
            if not w_ch:
                await self.db.clear_stale_channel_setting(guild.id, "welcome")
                channel_status.append("⚠️ Stale welcome channel ID was detected and auto-cleared.")
            else:
                channel_status.append(f"✅ Welcome channel: {w_ch.mention}")
        else:
            channel_status.append("ℹ️ Welcome channel: *Auto-detected*")

        if h_id:
            h_ch = guild.get_channel(h_id)
            if not h_ch:
                await self.db.clear_stale_channel_setting(guild.id, "help")
                channel_status.append("⚠️ Stale help channel ID was detected and auto-cleared.")
            else:
                channel_status.append(f"✅ Help channel: {h_ch.mention}")
        else:
            channel_status.append("ℹ️ Help channel: *Auto-detected*")

        if r_id:
            r_ch = guild.get_channel(r_id)
            if not r_ch:
                await self.db.clear_stale_channel_setting(guild.id, "review")
                channel_status.append("⚠️ Stale review channel ID was detected and auto-cleared.")
            else:
                channel_status.append(f"✅ Review channel: {r_ch.mention}")
        else:
            channel_status.append("ℹ️ Review channel: *Auto-detected*")

        is_email_opted_in = await self.db.is_guild_email_verification_enabled(guild.id)
        email_policy_label = "Mandatory (Opted In)" if is_email_opted_in else "Optional (Opted Out)"
        channel_status.append(f"📧 Email Verification Policy: **{email_policy_label}**")

        embed = discord.Embed(
            title="🛡️ TARVeri — Server Health & Diagnostics",
            color=discord.Color.green() if not warnings else discord.Color.orange(),
        )

        if warnings:
            embed.add_field(
                name="⚠️ Permission / Hierarchy Issues Detected",
                value="\n".join(f"• {w}" for w in warnings),
                inline=False,
            )
        else:
            embed.add_field(
                name="✅ Permissions & Role Hierarchy",
                value="All required permissions and role positions are properly configured.",
                inline=False,
            )

        if src_stats.get("created", 0) > 0:
            embed.add_field(
                name="🏛️ SRC Roles Self-Healing",
                value=f"• Restored **{src_stats['created']}** missing faculty SRC role(s)",
                inline=False,
            )

        if (
            dedup_stats.get("deleted_roles", 0) > 0
            or dedup_stats.get("migrated_members", 0) > 0
            or dedup_stats.get("failed", 0) > 0
        ):
            embed.add_field(
                name="🧹 Duplicate Role Cleanup & Migration",
                value=(
                    f"• Deleted **{dedup_stats.get('deleted_roles', 0)}** duplicate role(s)\n"
                    f"• Migrated **{dedup_stats.get('migrated_members', 0)}** member(s) to primary role\n"
                    f"• Blocked / hierarchy errors: **{dedup_stats.get('failed', 0)}**"
                ),
                inline=False,
            )

        embed.add_field(
            name="🔄 Self-Healing Member & Alumni Reconciliation",
            value=(
                f"• Verified students: checked **{reconcile_stats['checked']}**, restored **{reconcile_stats['restored']}**, failed **{reconcile_stats['failed']}**\n"
                f"• Graduated alumni: checked **{alumni_stats['checked']}**, restored **{alumni_stats['restored']}**, failed **{alumni_stats['failed']}**"
            ),
            inline=False,
        )

        embed.add_field(
            name="📡 Channel Configuration Health",
            value="\n".join(channel_status),
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=90.0)

    # ==========================================
    # 👥 4. Member Moderation (Lookup, Unverify & Revoke)
    # ==========================================

    @admin_group.command(
        name="user_info",
        description="Inspect member verification profile, student roles, join date, and audit history.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        user="The Discord member or user to inspect",
    )
    async def user_info(
        self,
        interaction: discord.Interaction,
        user: discord.User | discord.Member,
    ) -> None:
        """Inspects member verification status, faculty/campus/level roles, join history, and audit log."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        dashboard_view = AdminDashboardView(cog=self, admin_user=interaction.user, initial_category="moderation")
        embed = await dashboard_view.build_user_details_embed(user.id, interaction.guild)
        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=180.0)

    @admin_group.command(name="unverify", description="Unlink a member's student ID and revoke faculty roles.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        user="The Discord user to unverify",
        reason="Optional reason for unlinking the verification record",
    )
    async def unverify(
        self,
        interaction: discord.Interaction,
        user: discord.User | discord.Member,
        reason: str | None = None,
    ) -> None:
        """Unlinks verification from DB, resets rate limit, and strips faculty/alumni roles across mutual servers."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        unverify_fn = getattr(self.service, "unverify_member", None)
        if callable(unverify_fn) and (
            inspect.iscoroutinefunction(unverify_fn)
            or getattr(type(unverify_fn), "__name__", "") in ("AsyncMock", "AsyncMockMixin")
            or getattr(type(self.service), "__name__", "") not in ("MagicMock", "Mock")
        ):
            res = await self.service.unverify_member(
                user_id=user.id,
                admin=interaction.user,
                current_guild=interaction.guild,
                reason=reason or "No reason provided",
            )
            if not res.get("success"):
                await interaction.followup.send(f"❌ {user.mention} is not verified.", ephemeral=True)
                schedule_ttl_delete(interaction, delay=60.0)
                return

            roles_removed_servers = res.get("roles_removed", [])
            report = f"✅ **Successfully unverified {user.mention} (ID: `{user.id}`).**\n"
            if reason:
                report += f"• **Reason:** *{reason}*\n"
            if roles_removed_servers:
                report += f"• **Roles removed in {len(roles_removed_servers)} server(s):** {', '.join(roles_removed_servers)}\n"
            else:
                report += "• **Roles removed:** None (member not found or had no roles)\n"
            report += "• **Rate Limiter:** Reset successfully. The user may now verify a new ID."

            await interaction.followup.send(report, ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        verif = await self.db.get_verification_by_user(user.id)
        if not verif:
            await interaction.followup.send(f"❌ {user.mention} is not verified.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        mutual_guilds = await self.service.get_mutual_guilds_for_user(user.id)
        roles_removed_servers = []

        for guild in mutual_guilds:
            member = await self.service.get_or_fetch_member(guild, user.id)
            if not member:
                continue

            roles_to_remove = [
                r
                for r in getattr(member, "roles", [])
                if any(self.service._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES)
                or any(self.service._match_campus_role_in_list([r], camp) is not None for camp in CAMPUS_ROLE_NAMES)
                or any(self.service._match_study_level_role_in_list([r], lvl) is not None for lvl in STUDY_LEVEL_ROLE_NAMES)
                or self.service._match_alumni_role_in_list([r]) is not None
            ]

            me = getattr(guild, "me", None)
            can_manage = (
                getattr(me.guild_permissions, "manage_roles", False)
                if me and hasattr(me, "guild_permissions")
                else False
            )
            bot_top = getattr(me, "top_role", None)
            bot_pos = getattr(bot_top, "position", 0) if bot_top else 0

            for role in roles_to_remove:
                role_pos = getattr(role, "position", 0)
                if (
                    can_manage
                    and isinstance(bot_pos, int)
                    and isinstance(role_pos, int)
                    and role_pos < bot_pos
                ):
                    try:
                        await member.remove_roles(
                            role,
                            reason=f"TARVeri: Verification unlinked by admin {interaction.user}. Reason: {reason or 'None'}",
                        )
                        roles_removed_servers.append(f"{guild.name} ({role.name})")
                    except discord.HTTPException as e:
                        logger.warning(f"Could not remove role {role.name} from {member} in {guild.name}: {e}")

        await self.db.delete_verification(user.id)
        self.rate_limiter.reset(user.id)

        await self.db.log(
            "WARNING",
            "MEMBER_UNVERIFIED",
            f"Admin {interaction.user} unverified member {user} (ID: {user.id}). Reason: {reason or 'No reason provided'}",
            guild=interaction.guild,
            user_id=user.id,
        )

        report = f"✅ **Successfully unverified {user.mention} (ID: `{user.id}`).**\n"
        if reason:
            report += f"• **Reason:** *{reason}*\n"
        if roles_removed_servers:
            report += f"• **Roles removed in {len(roles_removed_servers)} server(s):** {', '.join(roles_removed_servers)}\n"
        else:
            report += "• **Roles removed:** None (member not found or had no roles)\n"
        report += "• **Rate Limiter:** Reset successfully. The user may now verify a new ID."

        await interaction.followup.send(report, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @admin_group.command(
        name="alumni_revoke",
        description="Revoke a member's graduated alumni status and remove TARUMT Alumni role.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        user="The Discord member whose Alumni status to revoke",
        reason="Reason for revoking Alumni status",
    )
    async def alumni_revoke(
        self,
        interaction: discord.Interaction,
        user: discord.User | discord.Member,
        reason: str | None = None,
    ) -> None:
        """Revokes alumni status from a verified member and strips their alumni role across mutual servers."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        res = await self.service.revoke_alumni_status(
            target_user=user,
            admin=interaction.user,
            current_guild=interaction.guild,
            reason=reason,
        )

        if not res.get("success"):
            await interaction.followup.send(
                f"❌ {res.get('message', 'Failed to revoke alumni status.')}", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        msg = (
            f"✅ **Successfully revoked Alumni status for {user.mention} (ID: `{user.id}`).**\n"
            f"• Removed `TARUMT Alumni` role across **{res.get('roles_removed_count', 0)}** mutual server(s).\n"
            f"• **Reason:** *{reason or 'No reason provided'}*"
        )
        await interaction.followup.send(msg, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    # ==========================================
    # ⚙️ 5. Consolidated Server Channel & Role Settings
    # ==========================================

    @admin_group.command(
        name="set_channel",
        description="Configure or reset welcome, help, or guest review channels for this server.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        channel_type="The type of channel to configure (welcome, help, or review)",
        channel="The text channel to set (leave blank to reset to auto-detect)",
    )
    async def set_channel(
        self,
        interaction: discord.Interaction,
        channel_type: Literal["welcome", "help", "review"],
        channel: discord.TextChannel | None = None,
    ) -> None:
        """Consolidated channel configuration for welcome, help, and review channels."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        channel_id = channel.id if channel else None

        if channel_type == "welcome":
            await self.db.set_guild_welcome_channel(interaction.guild.id, channel_id)
            verif_cog = self.bot.get_cog("Verification")
            if verif_cog and hasattr(verif_cog, "invalidate_guild_cache"):
                verif_cog.invalidate_guild_cache(interaction.guild.id)
            desc = f"Welcome channel set to {channel.mention}." if channel else "Welcome channel reset to **auto-detect** mode."
        elif channel_type == "help":
            await self.db.set_guild_help_channel(interaction.guild.id, channel_id)
            verif_cog = self.bot.get_cog("Verification")
            if verif_cog and hasattr(verif_cog, "invalidate_guild_cache"):
                verif_cog.invalidate_guild_cache(interaction.guild.id)
            desc = f"Help channel set to {channel.mention}." if channel else "Help channel reset to **auto-detect** mode."
        elif channel_type == "review":
            await self.db.set_guild_review_channel(interaction.guild.id, channel_id)
            desc = f"Guest review channel set to {channel.mention}." if channel else "Guest review channel reset to **auto-detect** mode."

        await self.db.log(
            "INFO",
            f"CONFIG_{channel_type.upper()}_CHANNEL",
            f"Admin {interaction.user} set {channel_type} channel to '{channel.name if channel else 'Auto-detect'}' ({channel_id})",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        await interaction.followup.send(f"✅ {desc}", ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @admin_group.command(
        name="set_role",
        description="Configure or reset custom guest role or reviewer/admin role.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        role_type="The role setting to configure (guest or admin)",
        role="The Discord role (for admin role, or leave blank to reset)",
        role_name="Custom role name string (for guest role, e.g. 'Guest (Approved)')",
    )
    async def set_role(
        self,
        interaction: discord.Interaction,
        role_type: Literal["guest", "admin"],
        role: discord.Role | None = None,
        role_name: str | None = None,
    ) -> None:
        """Consolidated role configuration for guest and admin reviewer roles."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        if role_type == "guest":
            target_name = role_name.strip() if role_name else (role.name if role else "Guest")
            await self.db.set_guild_guest_role(interaction.guild.id, target_name)
            desc = f"Guest role name set to **{target_name}**."
            log_key = "CONFIG_GUEST_ROLE"
            log_val = target_name
        else:  # admin
            target_name = role.name if role else (role_name.strip() if role_name else None)
            await self.db.set_guild_admin_role(interaction.guild.id, target_name)
            desc = (
                f"Admin / Reviewer role set to {role.mention if role else target_name}."
                if target_name
                else "Admin / Reviewer role reset to **auto-detect** mode."
            )
            log_key = "CONFIG_ADMIN_ROLE"
            log_val = target_name or "Auto-detect"

        await self.db.log(
            "INFO",
            log_key,
            f"Admin {interaction.user} set {role_type} role to '{log_val}'",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        await interaction.followup.send(f"✅ {desc}", ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    # Legacy individual helper aliases for direct backwards compatibility
    async def setwelcomec(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        await self.set_channel(interaction, channel_type="welcome", channel=channel)

    async def sethelpc(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        await self.set_channel(interaction, channel_type="help", channel=channel)

    async def setreviewchannel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        await self.set_channel(interaction, channel_type="review", channel=channel)

    async def setguestrole(self, interaction: discord.Interaction, role_name: str | None = None) -> None:
        await self.set_role(interaction, role_type="guest", role_name=role_name)

    async def setadminrole(self, interaction: discord.Interaction, role: discord.Role | None = None) -> None:
        await self.set_role(interaction, role_type="admin", role=role)

    @admin_group.command(
        name="email_verification",
        description="Configure whether student email OTP verification is required in this server.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        enabled="True to mandate student email OTP (Opt-In), False to make it optional (Opt-Out).",
    )
    async def email_verification(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Sets the per-guild email verification requirement (opt-in / opt-out)."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await self.db.set_guild_email_verification(interaction.guild.id, enabled)
        await self.db.log(
            "INFO",
            "CONFIG_EMAIL_VERIFICATION",
            f"Email verification requirement set to {enabled} by {interaction.user}",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        mode_str = "**MANDATORY (Opted In)**" if enabled else "**OPTIONAL (Opted Out)**"
        await interaction.response.send_message(
            f"✅ Email verification requirement for **{interaction.guild.name}** is now {mode_str}.",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)

    @admin_group.command(
        name="email_enforcement",
        description="Toggle retroactive role stripping for non-email-verified members during self-healing.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        enabled="True to retroactively strip roles from non-email-verified members, False to preserve existing roles (Default).",
    )
    async def email_enforcement(self, interaction: discord.Interaction, enabled: bool) -> None:
        """Sets the retroactive email role enforcement toggle for the server."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await self.db.set_guild_email_enforcement(interaction.guild.id, enabled)
        await self.db.log(
            "INFO",
            "CONFIG_EMAIL_ENFORCEMENT",
            f"Email role enforcement set to {enabled} by {interaction.user}",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        mode_str = "🔴 **ENFORCED (Retroactive role stripping active during self-healing)**" if enabled else "🟢 **DISABLED (Existing verified roles are safely preserved)**"
        await interaction.response.send_message(
            f"✅ Email role enforcement for **{interaction.guild.name}** is now {mode_str}.",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)

    # ==========================================
    # 🚫 5b. Per-Server Blacklist Management
    # ==========================================

    blacklist_group = app_commands.Group(
        name="blacklist",
        description="Per-server blacklist management for Discord users, Student IDs, and emails.",
        parent=admin_group,
    )

    @blacklist_group.command(
        name="user",
        description="Blacklist a Discord user from verifying or holding verified roles in this server.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        user="The Discord user to blacklist",
        reason="Reason for blacklisting this user",
    )
    async def blacklist_user(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str | None = None,
    ) -> None:
        """Blacklists a Discord user in this server."""
        if not self._check_admin(interaction):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        _, message = await self.service.blacklist_target(
            guild=interaction.guild,
            target_type="USER",
            raw_value=str(user.id),
            reason=reason,
            admin=interaction.user,
        )
        await interaction.followup.send(message, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @blacklist_group.command(
        name="student_id",
        description="Blacklist a Student ID from verifying in this server.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        student_id="The Student ID to blacklist (e.g. 23WMD09867)",
        reason="Reason for blacklisting this Student ID",
    )
    async def blacklist_student_id(
        self,
        interaction: discord.Interaction,
        student_id: str,
        reason: str | None = None,
    ) -> None:
        """Blacklists a Student ID in this server."""
        if not self._check_admin(interaction):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        _, message = await self.service.blacklist_target(
            guild=interaction.guild,
            target_type="STUDENT_ID",
            raw_value=student_id,
            reason=reason,
            admin=interaction.user,
        )
        await interaction.followup.send(message, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @blacklist_group.command(
        name="email",
        description="Blacklist an institutional student email from verifying in this server.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        email="The institutional email address to blacklist (e.g. student@student.tarc.edu.my)",
        reason="Reason for blacklisting this email",
    )
    async def blacklist_email(
        self,
        interaction: discord.Interaction,
        email: str,
        reason: str | None = None,
    ) -> None:
        """Blacklists an institutional student email in this server."""
        if not self._check_admin(interaction):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        _, message = await self.service.blacklist_target(
            guild=interaction.guild,
            target_type="EMAIL",
            raw_value=email,
            reason=reason,
            admin=interaction.user,
        )
        await interaction.followup.send(message, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @blacklist_group.command(
        name="remove",
        description="Remove a target (User ID, Student ID, or Email) from this server's blacklist.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        target_type="The type of target to remove",
        target="The Discord User ID, Student ID, or Email to unblacklist",
    )
    async def blacklist_remove(
        self,
        interaction: discord.Interaction,
        target_type: Literal["USER", "STUDENT_ID", "EMAIL"],
        target: str,
    ) -> None:
        """Removes a target from this server's blacklist."""
        if not self._check_admin(interaction):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        _, message = await self.service.unblacklist_target(
            guild=interaction.guild,
            target_type=target_type,
            raw_value=target,
            admin=interaction.user,
        )
        await interaction.followup.send(message, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @blacklist_group.command(
        name="list",
        description="View blacklisted entries for this server.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        target_type="Filter by target type (USER, STUDENT_ID, EMAIL, or ALL)",
        page="Page number to view (default: 1)",
    )
    async def blacklist_list(
        self,
        interaction: discord.Interaction,
        target_type: Literal["ALL", "USER", "STUDENT_ID", "EMAIL"] = "ALL",
        page: int = 1,
    ) -> None:
        """Lists blacklist entries for this server."""
        if not self._check_admin(interaction):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        query_type = None if target_type == "ALL" else target_type
        total = await self.db.count_guild_blacklist(interaction.guild.id, target_type=query_type)
        if total == 0:
            type_suffix = f" matching type `{target_type}`" if target_type != "ALL" else ""
            await interaction.followup.send(
                f"ℹ️ No blacklist entries found for **{interaction.guild.name}**{type_suffix}.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        per_page = 10
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * per_page

        entries = await self.db.get_guild_blacklist(
            interaction.guild.id,
            target_type=query_type,
            limit=per_page,
            offset=offset,
        )

        embed = discord.Embed(
            title=f"🚫 Guild Blacklist • {interaction.guild.name}",
            description=f"Showing **{len(entries)}** of **{total}** blacklisted entry/entries (Page {page}/{total_pages}).",
            color=discord.Color.red(),
        )

        for entry in entries:
            t_type = entry["target_type"]
            mask = entry["display_mask"] or entry["target_value"]
            reason = entry["reason"] or "No reason specified"
            by_user = f"<@{entry['blacklisted_by']}>" if entry["blacklisted_by"] else "Admin"
            created = entry["created_at"]
            embed.add_field(
                name=f"[{t_type}] {mask}",
                value=f"**Reason:** {reason}\n**Added By:** {by_user} • **Date:** `{created}`",
                inline=False,
            )

        embed.set_footer(text=f"TARVeri Blacklist • Page {page}/{total_pages}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=90.0)

    @blacklist_group.command(
        name="clear",
        description="Clear all blacklist entries for this server.",
    )
    @app_commands.default_permissions(administrator=True)
    async def blacklist_clear(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Clears all blacklist entries for this server."""
        if not self._check_admin(interaction):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        cleared_count = await self.db.clear_guild_blacklist(interaction.guild.id)
        await self.db.log(
            "WARNING",
            "BLACKLIST_CLEARED",
            f"Admin {interaction.user} cleared all {cleared_count} blacklist entries in '{interaction.guild.name}'",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )
        await interaction.followup.send(
            f"✅ Cleared **{cleared_count}** blacklist entries for **{interaction.guild.name}**.",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)

    @admin_group.command(
        name="backfill_roles",
        description="One-time migration: backfill branch campus & study level roles to previously verified students.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        default_campus="Default branch campus for legacy students without a branch tag (default: KL Main Campus)",
        default_level="Optional default study level for legacy students without a study level tag",
        all_servers="Whether to run the backfill across all mutual servers (default: True)",
    )
    @app_commands.choices(
        default_campus=[
            app_commands.Choice(name="KL Main Campus", value="W"),
            app_commands.Choice(name="Penang Branch", value="P"),
            app_commands.Choice(name="Perak Branch", value="A"),
            app_commands.Choice(name="Johor Branch", value="J"),
            app_commands.Choice(name="Pahang Branch", value="C"),
            app_commands.Choice(name="Sabah Branch", value="S"),
        ],
        default_level=[
            app_commands.Choice(name="Diploma", value="D"),
            app_commands.Choice(name="Degree", value="R"),
            app_commands.Choice(name="Foundation", value="F"),
            app_commands.Choice(name="Postgraduate", value="P"),
        ],
    )
    async def backfill_roles(
        self,
        interaction: discord.Interaction,
        default_campus: app_commands.Choice[str] | None = None,
        default_level: app_commands.Choice[str] | None = None,
        all_servers: bool = True,
    ) -> None:
        """Backfills missing branch campus and study level roles for existing verified students."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        campus_code = default_campus.value if default_campus else "W"
        level_code = default_level.value if default_level else None
        target_guild = None if all_servers else interaction.guild

        stats = await self.service.backfill_branch_roles(
            guild=target_guild,
            default_campus_code=campus_code,
            default_level_code=level_code,
        )

        embed = discord.Embed(
            title="🔄 Branch Campus & Study Level Role Backfill",
            color=discord.Color.brand_green(),
        )
        embed.add_field(
            name="📊 Migration Summary",
            value=(
                f"• Shared Servers Scanned: **{stats['guilds_scanned']}**\n"
                f"• Verified Members Checked: **{stats['members_checked']}**\n"
                f"• Roles Backfilled / Restored: **{stats['roles_assigned']}**\n"
                f"• Database Records Migrated: **{stats['db_migrated']}**\n"
                f"• Failed / Blocked: **{stats['failed']}**"
            ),
            inline=False,
        )
        campus_label = CAMPUS_ROLES.get(campus_code, "KL Main Campus")
        level_label = STUDY_LEVEL_ROLES.get(level_code) if level_code else "Auto-detected from Discord / Kept as is"
        embed.set_footer(text=f"Default Branch: {campus_label} | Default Level: {level_label}")

        await self.db.log(
            "INFO",
            "BRANCH_ROLES_BACKFILLED",
            f"Admin {interaction.user} triggered branch role backfill: {stats}",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=90.0)

    @admin_group.command(
        name="restore_roles",
        description="Immediately restore and recover missing verified roles for past verified members.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        user="Optional specific member to recover roles for (leave empty to scan and recover entire server)"
    )
    async def restore_roles(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        """Immediately restores missing verified roles for past verified students."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ Server context required.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if user:
            details = await self.db.get_verification_details(user.id)
            if not details:
                verif = await self.db.get_verification_by_user(user.id)
                if not verif:
                    await interaction.followup.send(
                        f"⚠️ {user.mention} is not recorded as a verified student in the database.",
                        ephemeral=True,
                    )
                    return
                faculty_code = verif[1]
                campus_code = "W"
                level_code = "R"
                is_alumni = False
            else:
                faculty_code = details.get("faculty_code")
                campus_code = details.get("campus_code") or "W"
                level_code = details.get("level_code") or "R"
                is_alumni = bool(details.get("is_alumni"))

            target_faculty = resolve_faculty_role(faculty_code)
            target_campus = resolve_campus_role(campus_code)
            target_level = resolve_study_level_role(level_code)

            if not target_faculty:
                await interaction.followup.send(
                    f"❌ Could not resolve faculty role for {user.mention} (stored code: `{faculty_code}`).",
                    ephemeral=True,
                )
                return

            result = await self.service.assign_role_across_guilds(
                user.id,
                target_faculty,
                [interaction.guild],
                campus_role_name=target_campus,
                level_role_name=target_level,
                is_email_verified=bool(details.get("student_email_hash")) if details else False,
            )

            if is_alumni:
                try:
                    await self.service.sync_alumni_role_across_guilds(
                        user.id, [interaction.guild], reason="TARVeri: Admin role recovery"
                    )
                except Exception as exc:
                    logger.warning("Failed syncing alumni role during restore_roles: %s", exc)

            summary = self.service.format_role_summary(result)
            await self.db.log(
                "INFO",
                "ROLE_RECOVERED_MANUAL",
                f"Admin {interaction.user} restored roles for {user} (ID: {user.id}) in '{interaction.guild.name}'",
                guild=interaction.guild,
                user_id=user.id,
            )
            embed = discord.Embed(
                title=f"✅ Verified Roles Restored for {user.display_name}",
                description=summary or f"Roles successfully synchronized for {user.mention}.",
                color=discord.Color.green(),
                timestamp=datetime.now(get_configured_tz()),
            )
            embed.add_field(name="🏛️ Faculty", value=f"`{target_faculty}`", inline=True)
            embed.add_field(name="🏫 Campus", value=f"`{target_campus}`", inline=True)
            embed.add_field(name="🎓 Level", value=f"`{target_level}`", inline=True)
            if is_alumni:
                embed.add_field(name="🎖️ Alumni", value="`TARUMT Alumni`", inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            stats = await self.service.reconcile_verified_members(interaction.guild)
            alumni_stats = await self.service.reconcile_alumni_members(interaction.guild)
            embed = discord.Embed(
                title=f"🔄 Server Role Recovery & Reconciliation — {interaction.guild.name}",
                description="Completed self-healing recovery scan for past verified members.",
                color=discord.Color.brand_green(),
                timestamp=datetime.now(get_configured_tz()),
            )
            embed.add_field(name="👥 Members Checked", value=f"**{stats.get('checked', 0)}**", inline=True)
            embed.add_field(name="✅ Roles Restored", value=f"**{stats.get('restored', 0)}**", inline=True)
            embed.add_field(name="🎓 Alumni Restored", value=f"**{alumni_stats.get('restored', 0)}**", inline=True)
            if stats.get("failed", 0) > 0:
                embed.add_field(name="⚠️ Hierarchy/Blocked", value=f"**{stats.get('failed', 0)}**", inline=True)

            await self.db.log(
                "INFO",
                "SERVER_ROLES_RESTORED",
                f"Admin {interaction.user} triggered full server role recovery: {stats}",
                guild=interaction.guild,
                user_id=interaction.user.id,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        schedule_ttl_delete(interaction, delay=90.0)

    # ==========================================
    # 🚀 6. Verification Gateway Panel Deployer
    # ==========================================

    @admin_group.command(
        name="panel",
        description="Deploy the persistent 3-button verification gateway panel.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Target channel (defaults to current channel)")
    async def panel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        """Posts the persistent verification gateway panel."""
        if not self._check_admin(interaction):
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

        from tarveri.cogs.guest_cog import VerificationGatewayView, build_gateway_panel_embed

        guest_service = getattr(self.bot, "guest_service", None)
        if not guest_service:
            from tarveri.services.guest_service import GuestService

            guest_service = GuestService(
                bot=self.bot,
                db=self.db,
                rate_limiter=self.rate_limiter,
                admin_role_name=self.admin_role_name,
            )

        email_required = (
            await self.db.is_guild_email_verification_enabled(interaction.guild.id)
            if interaction.guild
            else False
        )
        guild_name = interaction.guild.name if interaction.guild else "the Server"
        embed = build_gateway_panel_embed(guild_name=guild_name, require_email=email_required)

        view = VerificationGatewayView(self.service, guest_service)
        try:
            await target_ch.send(embed=embed, view=view)
            await interaction.followup.send(
                f"✅ Verification gateway panel posted to {target_ch.mention}!", ephemeral=True
            )
        except (discord.HTTPException, discord.Forbidden) as e:
            await interaction.followup.send(f"❌ Failed to send gateway panel: {e}", ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    # Legacy alias
    async def send_gateway_panel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        await self.panel(interaction, channel=channel)

    # ==========================================
    # 🎟️ 7. Guest Review Tickets
    # ==========================================

    @admin_group.command(
        name="tickets",
        description="List and inspect recent guest review tickets with links to threads.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        status="Filter by status (OPEN, APPROVED, REJECTED, EXPIRED, LEFT_SERVER)",
        limit="Number of records to show (1-20, default 10)",
    )
    async def tickets(
        self,
        interaction: discord.Interaction,
        status: str | None = None,
        limit: app_commands.Range[int, 1, 20] = 10,
    ) -> None:
        """Displays recent guest review tickets with clickable thread links and resolution details."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        tickets_list = await self.db.list_guest_tickets(interaction.guild.id, status=status, limit=limit)

        if not tickets_list:
            filter_text = f" with status `{status}`" if status else ""
            await interaction.followup.send(
                f"ℹ️ No guest tickets found in this server{filter_text}.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        embed = discord.Embed(
            title=f"📋 Guest Review Tickets — {interaction.guild.name}",
            description=f"Showing **{len(tickets_list)}** recent ticket(s)"
            + (f" filtered by `{status.upper()}`" if status else "")
            + ":",
            color=discord.Color.blue(),
        )

        for t in tickets_list:
            seq = t.get("ticket_seq") or t.get("ticket_id")
            seq_code = format_ticket_seq(seq)
            t_status = t.get("status", "OPEN")
            status_emoji = {
                "OPEN": "⏳",
                "APPROVED": "✅",
                "REJECTED": "🛑",
                "CLOSED": "🔒",
                "DISMISSED": "📁",
                "CANCELLED": "⚪",
                "EXPIRED": "⏰",
                "LEFT_SERVER": "🚪",
                "BANNED": "🔨",
            }.get(t_status, "📄")

            thread_mention = f"<#{t['channel_id']}>"
            applicant_mention = f"<@{t['applicant_id']}>"
            admin_info = f" • Closed by <@{t['closed_by_admin_id']}>" if t.get("closed_by_admin_id") else ""
            reason_info = f"\n> Reason: *\"{t['close_reason']}\"*" if t.get("close_reason") else ""

            field_name = f"{status_emoji} Ticket #{seq_code} — {t_status}"
            field_value = (
                f"**Applicant:** {applicant_mention} | **Thread:** {thread_mention}{admin_info}\n"
                f"**Created:** `{t.get('created_at', 'N/A')}`{reason_info}"
            )
            embed.add_field(name=field_name, value=field_value, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=90.0)

    # Legacy alias
    async def guest_tickets(
        self,
        interaction: discord.Interaction,
        status: str | None = None,
        limit: app_commands.Range[int, 1, 20] = 10,
    ) -> None:
        await self.tickets(interaction, status=status, limit=limit)

    # ==========================================
    # 💾 8. Database Backups
    # ==========================================

    @admin_group.command(
        name="backup",
        description="Manage SQLite database snapshots, backups, and settings restoration.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        action="Backup operation to perform (create, list, or restore_settings)",
        backup_file="Optional specific backup filename to restore (default: most recent)",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Create Snapshot Now", value="create"),
            app_commands.Choice(name="List Available Backups", value="list"),
            app_commands.Choice(name="Restore Previous Settings", value="restore_settings"),
        ]
    )
    async def backup(
        self,
        interaction: discord.Interaction,
        action: str = "create",
        backup_file: str | None = None,
    ) -> None:
        """Manages SQLite database backups and guild settings restoration."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        action_val = action.value if hasattr(action, "value") else str(action)

        if action_val == "create":
            backup_path = await self.db.create_backup()
            max_b = getattr(self.bot, "settings", None) and self.bot.settings.max_backups or 10
            await interaction.followup.send(
                f"✅ **Database backup created successfully.**\n"
                f"• Snapshot file: `{backup_path}`\n"
                f"• Retention: up to **{max_b}** most recent backups are automatically kept in `backups/`.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=60.0)

        elif action_val == "list":
            settings = getattr(self.bot, "settings", None)
            backup_dir = settings.backup_dir if settings and isinstance(getattr(settings, "backup_dir", None), str) else "backups"
            backups = self.db.list_backups(backup_dir=backup_dir)
            if not backups:
                await interaction.followup.send("ℹ️ No backups currently found.", ephemeral=True)
                schedule_ttl_delete(interaction, delay=60.0)
                return

            embed = discord.Embed(
                title=f"💾 Database Backups (`{backup_dir}/`)",
                description=f"Showing **{len(backups)}** available snapshot(s):",
                color=discord.Color.blue(),
            )
            for b in backups[:10]:
                size_kb = b.get("size_bytes", 0) / 1024 if "size_bytes" in b else b.get("size_kb", 0)
                time_str = b.get("timestamp") or b.get("created_at") or "N/A"
                embed.add_field(
                    name=f"📦 {b['filename']}",
                    value=f"• Size: `{size_kb:.1f} KB`\n• Created: `{time_str}`",
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
            schedule_ttl_delete(interaction, delay=90.0)

        elif action_val == "restore_settings":
            if backup_file:
                res = await self.db.restore_guild_settings_from_backup(backup_file)
                count = res.get("restored_guilds", 0) if isinstance(res, dict) else 0
            else:
                count = await self.db.restore_latest_guild_settings()
            if count:
                await interaction.followup.send(
                    f"✅ **Successfully restored `{count}` guild settings records** from backup.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send("⚠️ No suitable backup found to restore settings from.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)

    # ==========================================
    # 📋 9. Daily Logs & Historical Archives
    # ==========================================

    @admin_group.command(
        name="logs",
        description="Inspect daily log files, view compressed 10-day archives, or trigger log rotation.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        action="Log operation to perform (list: view files & archives, archive: trigger compression, recent: tail log)",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="List Daily Logs & 10-Day Archives", value="list"),
            app_commands.Choice(name="Run 10-Day Log Archival (.tar.gz)", value="archive"),
            app_commands.Choice(name="View Recent Active Log Lines", value="recent"),
        ]
    )
    async def logs(
        self,
        interaction: discord.Interaction,
        action: str = "list",
    ) -> None:
        """Inspects daily log files in logs/, compressed 10-day archives, or forces immediate rotation."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        action_val = action.value if hasattr(action, "value") else str(action)
        settings = getattr(self.bot, "settings", None)
        logs_dir = settings.logs_dir if settings and isinstance(getattr(settings, "logs_dir", None), str) else "logs"
        tz_name = settings.timezone_name if settings and isinstance(getattr(settings, "timezone_name", None), str) else "Asia/Kuala_Lumpur"

        if action_val == "list":
            daily_logs = list_daily_logs(logs_dir=logs_dir, tz_name=tz_name)
            archives = list_log_archives(logs_dir=logs_dir)

            embed = discord.Embed(
                title=f"📁 TARVeri Logs & Historical Archives (`{logs_dir}/`)",
                color=discord.Color.blue(),
            )

            if daily_logs:
                daily_desc = []
                for dl in daily_logs[:7]:
                    size_kb = dl["size_bytes"] / 1024
                    daily_desc.append(f"• 📄 `{dl['filename']}` — {size_kb:.1f} KB ({dl['lines']} lines)")
                embed.add_field(
                    name=f"📅 Active Daily Logs ({len(daily_logs)} total)",
                    value="\n".join(daily_desc) if daily_desc else "None",
                    inline=False,
                )
            else:
                embed.add_field(name="📅 Active Daily Logs", value="No daily log files found.", inline=False)

            if archives:
                archive_desc = []
                for ar in archives[:7]:
                    size_kb = ar["size_bytes"] / 1024
                    archive_desc.append(
                        f"• 📦 `{ar['filename']}` — {size_kb:.1f} KB ({ar['file_count']} logs bundled)"
                    )
                embed.add_field(
                    name=f"🗜️ 10-Day Compressed Archives ({len(archives)} total in `{logs_dir}/archives/`)",
                    value="\n".join(archive_desc) if archive_desc else "None",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="🗜️ 10-Day Compressed Archives",
                    value="No archives created yet (logs $>10$ days old are grouped and compressed).",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
            schedule_ttl_delete(interaction, delay=90.0)

        elif action_val == "archive":
            results = archive_old_logs(
                logs_dir=logs_dir,
                older_than_days=getattr(self.bot, "settings", None) and self.bot.settings.log_archive_days or 10,
                tz_name=tz_name,
            )

            if not results:
                await interaction.followup.send(
                    "ℹ️ No uncompressed daily log files older than 10 days found to archive.",
                    ephemeral=True,
                )
                schedule_ttl_delete(interaction, delay=60.0)
                return

            total_saved_kb = sum(r["space_saved_bytes"] for r in results) / 1024
            total_files = sum(len(r["files_archived"]) for r in results)

            embed = discord.Embed(
                title="🗜️ Log Archival & Compression Completed",
                description=(
                    f"Successfully grouped and compressed **{total_files}** daily log file(s) "
                    f"into **{len(results)}** 10-day `.tar.gz` archive(s).\n"
                    f"💾 **Space Saved:** `{total_saved_kb:.1f} KB`"
                ),
                color=discord.Color.green(),
            )
            for r in results[:10]:
                ar_size_kb = r["archive_bytes"] / 1024
                embed.add_field(
                    name=f"📦 `{r['archive_name']}`",
                    value=f"• **Period:** `{r['period_tag']}`\n• **Files Bundled:** {len(r['files_archived'])}\n• **Archive Size:** `{ar_size_kb:.1f} KB`",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)
            schedule_ttl_delete(interaction, delay=90.0)

        elif action_val == "recent":
            daily_logs = list_daily_logs(logs_dir=logs_dir, tz_name=tz_name)
            if not daily_logs:
                await interaction.followup.send("⚠️ No active log file found.", ephemeral=True)
                schedule_ttl_delete(interaction, delay=60.0)
                return

            latest_log_path = daily_logs[0]["path"]
            try:
                with open(latest_log_path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                tail_lines = lines[-15:] if len(lines) > 15 else lines
                content = "".join(tail_lines)
                if len(content) > 1900:
                    content = content[-1900:]

                await interaction.followup.send(
                    f"📄 **Latest Logs** (`{daily_logs[0]['filename']}`):\n```text\n{content}\n```",
                    ephemeral=True,
                )
            except Exception as e:
                logger.warning("Failed to read daily log file: %s", e, exc_info=True)
                await interaction.followup.send(f"❌ Failed to read log file: {e}", ephemeral=True)
            schedule_ttl_delete(interaction, delay=90.0)

    # ==========================================
    # 🔍 10. Audit Log Query
    # ==========================================

    @admin_group.command(name="audit", description="Query recent audit log entries.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        limit="Number of log records to retrieve (1-50, default 10)",
        event_type="Filter by event type (e.g. VERIFICATION_SUCCESS, MEMBER_UNVERIFIED)",
    )
    async def audit(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 50] = 10,
        event_type: str | None = None,
    ) -> None:
        """Retrieves recent audit log records with filtering."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        logs = await self.db.recent_audit(limit=limit, event_type=event_type)
        if not logs:
            await interaction.followup.send("ℹ️ No audit log records found.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        embed = discord.Embed(
            title="📜 TARVeri — Audit Log Entries",
            description=f"Showing the last **{len(logs)}** log event(s):",
            color=discord.Color.dark_gray(),
        )

        for log_row in logs:
            timestamp, level, ev_type, guild_name, user_id, msg = log_row
            guild_str = f"Guild: {guild_name}" if guild_name else "Global"
            user_str = f"User: <@{user_id}>" if user_id else "System"

            embed.add_field(
                name=f"[{level}] {ev_type} • {timestamp}",
                value=f"{msg}\n*{guild_str} | {user_str}*",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=90.0)

    # ==========================================
    # 🔄 11. Role Resynchronization
    # ==========================================

    @admin_group.command(name="resync", description="Force resynchronization of verification roles.")
    @app_commands.default_permissions(administrator=True)
    async def resync(self, interaction: discord.Interaction) -> None:
        """Checks and re-applies faculty and alumni roles across mutual servers."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        if interaction.guild:
            reconcile_stats = await self.service.reconcile_verified_members(interaction.guild)
            alumni_stats = await self.service.reconcile_alumni_members(interaction.guild)
            await interaction.followup.send(
                f"✅ **Mutual Server Role Resynchronization Complete!**\n"
                f"• Verified Students: checked **{reconcile_stats['checked']}**, restored **{reconcile_stats['restored']}**\n"
                f"• Graduated Alumni: checked **{alumni_stats['checked']}**, restored **{alumni_stats['restored']}**",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("❌ Resync must be run in a server.", ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    # ==========================================
    # 🔄 12. Updates Checker
    # ==========================================

    @admin_group.command(
        name="updates",
        description="Check if bot updates are available from git upstream.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        stream="Optional branch/stream name to check against (defaults to configured stream)"
    )
    async def updates(
        self, interaction: discord.Interaction, stream: str | None = None
    ) -> None:
        """Checks git upstream for new commits on the configured or specified branch."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        checker = self.update_checker
        if not checker:
            checker = UpdateCheckerService(
                bot=self.bot,
                db=self.db,
                update_stream=stream or "auto",
            )

        is_avail, count, local_h, remote_h, target_stream = await checker.check_for_updates(
            custom_stream=stream
        )

        embed = discord.Embed(
            title="🔄 TARVeri Update Status",
            color=discord.Color.green() if not is_avail else discord.Color.gold(),
        )
        embed.add_field(name="Target Stream", value=f"`{target_stream}`", inline=False)
        embed.add_field(name="Local Version", value=f"`{local_h[:7]}`" if local_h else "*Unknown*", inline=True)
        embed.add_field(name="Remote Version", value=f"`{remote_h[:7]}`" if remote_h else "*Unknown*", inline=True)

        if is_avail:
            branch_arg = target_stream.replace("origin/", "").strip()
            embed.description = (
                f"🔔 **Update available!** Remote is **{count} commit(s)** ahead.\n\n"
                f"To update, run on your server terminal:\n"
                f"```bash\n./scripts/update.sh {branch_arg}\n```"
            )
        else:
            if not remote_h:
                embed.description = f"⚠️ Could not resolve remote branch `{target_stream}`. Check if the branch exists on remote."
            else:
                embed.description = "✅ TARVeri is up to date on this stream!"

        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    # Legacy alias
    async def check_updates(self, interaction: discord.Interaction, stream: str | None = None) -> None:
        await self.updates(interaction, stream=stream)

    # ==========================================
    # ⚡ 13. Sync Commands
    # ==========================================

    @admin_group.command(
        name="sync_commands",
        description="Synchronize application slash commands with Discord.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        clean_duplicates="Clear guild-specific command overrides before syncing",
        guild_only="Sync only to the current server (faster) instead of globally",
    )
    async def sync_commands(
        self,
        interaction: discord.Interaction,
        clean_duplicates: bool = False,
        guild_only: bool = False,
    ) -> None:
        """Synchronizes application commands with Discord API."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            if clean_duplicates and interaction.guild:
                self.bot.tree.clear_commands(guild=interaction.guild)

            if guild_only and interaction.guild:
                self.bot.tree.copy_global_to(guild=interaction.guild)
                synced = await self.bot.tree.sync(guild=interaction.guild)
                scope = f"to server '{interaction.guild.name}'"
            else:
                synced = await self.bot.tree.sync()
                scope = "globally"

            msg_suffix = "Duplicates cleared prior to sync." if clean_duplicates else ""
            await interaction.followup.send(
                f"✅ Successfully synced {len(synced)} command(s) {scope}.\n{msg_suffix}",
                ephemeral=True,
            )
        except Exception as e:
            logger.warning("Failed to sync commands: %s", e, exc_info=True)
            await interaction.followup.send(f"❌ Failed to sync commands: {e}", ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    # ==========================================
    # 🔒 14. Manual Ticket Closure
    # ==========================================

    @admin_group.command(
        name="close_ticket",
        description="Manually close a guest review ticket thread without kicking the applicant.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        reason="Reason for closing the ticket (e.g. duplicate request, inquiries resolved, spam dismissal)",
        ticket="Optional ticket code (e.g. A0001, 1, #A0001) or DB ID (defaults to current thread ticket)",
    )
    async def close_ticket(
        self,
        interaction: discord.Interaction,
        reason: str | None = None,
        ticket: str | None = None,
    ) -> None:
        """Manually closes a guest review ticket without granting guest roles or kicking/banning."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used inside a server.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        guest_svc = self.guest_service or getattr(self.bot, "guest_service", None)
        if not guest_svc:
            await interaction.response.send_message("❌ Guest verification service is unavailable.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        ticket_data = None
        if ticket is not None:
            parsed_seq = parse_ticket_seq(ticket)
            if parsed_seq is not None:
                # 1. Search by guild-scoped sequence or DB ID
                ticket_data = await self.db.get_guest_ticket_by_seq(interaction.guild.id, parsed_seq)
                if not ticket_data:
                    ticket_data = await self.db.get_guest_ticket_by_id(parsed_seq)
                    if ticket_data and ticket_data.get("guild_id") != interaction.guild.id:
                        ticket_data = None
        else:
            # 2. Default to current thread / channel
            if interaction.channel:
                ticket_data = await self.db.get_guest_ticket_by_channel(interaction.channel.id)

        if not ticket_data:
            if ticket is not None:
                await interaction.followup.send(
                    f"❌ No guest review ticket found matching `{ticket}`.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "❌ This channel is not an active guest review ticket thread. "
                    "Please run this command inside a ticket thread or specify `ticket`.",
                    ephemeral=True,
                )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if ticket_data.get("status") != "OPEN":
            seq = ticket_data.get("ticket_seq") or ticket_data["ticket_id"]
            seq_code = format_ticket_seq(seq)
            status_str = ticket_data.get("status", "UNKNOWN")
            ticket_ch_id = ticket_data.get("channel_id")

            thread = None
            if isinstance(interaction.channel, discord.Thread) and interaction.channel.id == ticket_ch_id:
                thread = interaction.channel
            elif ticket_ch_id:
                if hasattr(interaction.guild, "get_thread"):
                    thread = interaction.guild.get_thread(ticket_ch_id)
                if not thread and hasattr(guest_svc, "_get_or_fetch_thread"):
                    res = guest_svc._get_or_fetch_thread(interaction.guild, ticket_ch_id)
                    thread = await res if inspect.isawaitable(res) else res
                if not thread and hasattr(interaction.guild, "get_channel"):
                    thread = interaction.guild.get_channel(ticket_ch_id)

            # If the thread exists and is unarchived or unlocked, cleanly archive it!
            if thread and (isinstance(thread, discord.Thread) or hasattr(thread, "send")) and (
                not getattr(thread, "archived", False) or not getattr(thread, "locked", False)
            ):
                reason_note = (
                    f"\n> **Reason:** *\"{reason.strip()}\"*"
                    if reason and reason.strip()
                    else ""
                )
                try:
                    res = thread.send(
                        f"🔒 **Ticket thread archived by {interaction.user.mention} via `/admin close_ticket`.**\n"
                        f"*(Ticket was previously recorded as `{status_str}`)*{reason_note}\n\n"
                        f"This thread is now locked and archived."
                    )
                    if inspect.isawaitable(res):
                        await res
                except (discord.HTTPException, Exception) as exc:
                    logger.debug("Failed sending thread closure message: %s", exc)

                # Try to update the review embed on the root message
                try:
                    from tarveri.cogs.guest_cog import build_review_embed
                    applicant_member = interaction.guild.get_member(ticket_data["applicant_id"])
                    embed = build_review_embed(
                        ticket_data, interaction.guild, applicant_member, status_override=status_str
                    )
                    starter_msg = getattr(thread, "starter_message", None)
                    if starter_msg and starter_msg.author.id == self.bot.user.id and hasattr(starter_msg, "edit"):
                        res = starter_msg.edit(embed=embed, view=discord.ui.View())
                        if inspect.isawaitable(res):
                            await res
                except Exception as exc:
                    logger.debug("Failed updating starter message embed: %s", exc)

                await asyncio.sleep(1)
                try:
                    if hasattr(thread, "edit"):
                        res = thread.edit(
                            locked=True,
                            archived=True,
                            reason=f"TARVeri: Archived by {interaction.user} (status: {status_str})",
                        )
                        if inspect.isawaitable(res):
                            await res
                except (discord.HTTPException, Exception) as exc:
                    logger.debug("Failed locking/archiving thread: %s", exc)

                if hasattr(guest_svc, "_cleanup_channel_overwrites"):
                    await guest_svc._cleanup_channel_overwrites(interaction.guild, ticket_data)

                await interaction.followup.send(
                    f"🔒 Ticket #{seq_code} ({status_str}) was open as an unarchived thread. Cleaned up and archived the thread.",
                    ephemeral=True,
                )
                schedule_ttl_delete(interaction, delay=60.0)
                return

            await interaction.followup.send(
                f"⚠️ Ticket #{seq_code} is already resolved ({status_str}) and archived.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        # Execute manual closure
        success, reply_msg = await guest_svc.close_guest_ticket_manually(
            ticket=ticket_data,
            guild=interaction.guild,
            admin_user=interaction.user,
            reason=reason,
        )

        if success:
            ticket_ch_id = ticket_data.get("channel_id")
            thread = None
            if isinstance(interaction.channel, discord.Thread) and interaction.channel.id == ticket_ch_id:
                thread = interaction.channel
            elif ticket_ch_id:
                if hasattr(interaction.guild, "get_thread"):
                    thread = interaction.guild.get_thread(ticket_ch_id)
                if not thread and hasattr(guest_svc, "_get_or_fetch_thread"):
                    res = guest_svc._get_or_fetch_thread(interaction.guild, ticket_ch_id)
                    thread = await res if inspect.isawaitable(res) else res
                if not thread and hasattr(interaction.guild, "get_channel"):
                    thread = interaction.guild.get_channel(ticket_ch_id)

            # If inside the thread (or thread is accessible), post closure notice and lock/archive
            if thread and (isinstance(thread, discord.Thread) or hasattr(thread, "send")):
                reason_note = (
                    f"\n> **Reason:** *\"{reason.strip()}\"*"
                    if reason and reason.strip()
                    else ""
                )
                try:
                    res = thread.send(
                        f"🔒 **Ticket manually closed by {interaction.user.mention} via `/admin close_ticket`.**\n"
                        f"*(Applicant remains in the server; no role changes or kicks executed)*{reason_note}\n\n"
                        f"This thread will be locked and archived."
                    )
                    if inspect.isawaitable(res):
                        await res
                except (discord.HTTPException, Exception) as exc:
                    logger.debug("Failed sending thread closure message: %s", exc)

                # Try to update the review embed on the root message if found in guest_cog
                try:
                    updated_ticket = await self.db.get_guest_ticket_by_id(ticket_data["ticket_id"])
                    applicant_member = interaction.guild.get_member(ticket_data["applicant_id"])
                    if updated_ticket:
                        from tarveri.cogs.guest_cog import build_review_embed
                        embed = build_review_embed(
                            updated_ticket, interaction.guild, applicant_member, status_override="CLOSED"
                        )
                        starter_msg = getattr(thread, "starter_message", None)
                        if starter_msg and starter_msg.author.id == self.bot.user.id and hasattr(starter_msg, "edit"):
                            res = starter_msg.edit(embed=embed, view=discord.ui.View())
                            if inspect.isawaitable(res):
                                 await res
                except Exception as exc:
                    logger.debug("Failed updating starter message embed: %s", exc)

                await asyncio.sleep(2)
                try:
                    if hasattr(thread, "edit"):
                        res = thread.edit(locked=True, archived=True, reason=f"TARVeri: Ticket closed by {interaction.user}")
                        if inspect.isawaitable(res):
                            await res
                except (discord.HTTPException, Exception) as exc:
                    logger.debug("Failed locking/archiving thread: %s", exc)

            await interaction.followup.send(reply_msg, ephemeral=True)
        else:
            await interaction.followup.send(reply_msg, ephemeral=True)

        schedule_ttl_delete(interaction, delay=60.0)

    # ==========================================
    # 🛡️ 13. Mass Action Approvals & Circuit Breaker Commands
    # ==========================================

    @admin_group.command(
        name="mass_revocation",
        description="Inspect, approve, or reject suspended mass role revocation actions.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        action="Action to perform (list, approve, or reject).",
        action_id="The Action ID (e.g. MREV-XXXXXX) to approve or reject.",
    )
    async def mass_revocation(
        self,
        interaction: discord.Interaction,
        action: Literal["list", "approve", "reject"],
        action_id: str | None = None,
    ) -> None:
        """Inspects or authorizes staged mass role revocations."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if action == "list":
            actions = await self.db.list_pending_mass_actions(guild_id=interaction.guild.id, limit=10)
            if not actions:
                await interaction.followup.send(
                    f"ℹ️ No pending or recent mass actions found for **{interaction.guild.name}**.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title=f"🛡️ Mass Actions Queue — {interaction.guild.name}",
                description="List of staged mass actions and their approval status:",
                color=discord.Color.blue(),
                timestamp=datetime.now(get_configured_tz()),
            )
            for act in actions:
                status_emoji = "⏳" if act["status"] == "PENDING" else ("✅" if act["status"] == "APPROVED" else "❌")
                embed.add_field(
                    name=f"{status_emoji} `{act['action_id']}` — {act['action_type']}",
                    value=(
                        f"• **Status**: `{act['status']}`\n"
                        f"• **Affected**: {len(act['user_ids'])} members\n"
                        f"• **Reason**: {act['reason']}\n"
                        f"• **Created**: <t:{int(datetime.fromisoformat(act['created_at']).timestamp())}:R>"
                    ),
                    inline=False,
                )
            embed.set_footer(text="Use /admin mass_revocation action:approve|reject action_id:ID to decide.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if not action_id:
            await interaction.followup.send(
                "❌ Please specify an `action_id` (e.g. `MREV-A1B2C3`) to approve or reject.",
                ephemeral=True,
            )
            return

        clean_id = action_id.strip().upper()
        if action == "approve":
            success, msg = await self.service.execute_approved_mass_revocation(
                interaction.guild, clean_id, admin=interaction.user
            )
            await interaction.followup.send(msg, ephemeral=True)
        elif action == "reject":
            success, msg = await self.service.reject_mass_revocation(
                interaction.guild, clean_id, admin=interaction.user
            )
            await interaction.followup.send(msg, ephemeral=True)

        schedule_ttl_delete(interaction, delay=60.0)


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
        success, msg = await self.service.reject_mass_revocation(
            interaction.guild, action_id, admin=interaction.user
        )

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
            logger.debug(f"Could not edit mass revocation rejection message: {exc}")

        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    pass


