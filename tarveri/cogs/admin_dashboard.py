from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord
from discord import ui

from tarveri.config import FACULTY_ROLES, resolve_faculty_role
from tarveri.services.log_service import (
    archive_old_logs,
    list_daily_logs,
    list_log_archives,
)
from tarveri.services.update_checker import UpdateCheckerService
from tarveri.utils import format_ticket_seq, schedule_ttl_delete

if TYPE_CHECKING:
    from tarveri.cogs.admin_cog import AdminCog

logger = logging.getLogger("tarveri")


def _extract_user_id(raw_str: str) -> int | None:
    """Extracts a numeric user ID from a raw string or mention (<@123456789>)."""
    raw_str = raw_str.strip()
    match = re.search(r"(\d{15,22})", raw_str)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


class UnverifyModal(ui.Modal, title="❌ Unverify Student"):
    user_input = ui.TextInput(
        label="Student User ID or Mention",
        placeholder="e.g. 123456789012345678 or @student",
        required=True,
        max_length=64,
    )
    reason_input = ui.TextInput(
        label="Reason for Unlinking Verification",
        placeholder="e.g. Identity correction, student withdrawal",
        required=False,
        max_length=256,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, cog: AdminCog, dashboard_view: AdminDashboardView) -> None:
        super().__init__()
        self.cog = cog
        self.dashboard_view = dashboard_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        user_id = _extract_user_id(self.user_input.value)
        if not user_id:
            await interaction.followup.send("❌ Invalid User ID or mention provided.", ephemeral=True)
            return

        reason = self.reason_input.value.strip() or "Unverified via Admin Dashboard"
        res = await self.cog.service.unverify_member(
            user_id=user_id,
            admin=interaction.user,
            current_guild=interaction.guild,
            reason=reason,
        )

        if not res.get("success"):
            await interaction.followup.send(
                f"ℹ️ User with ID `{user_id}` is not currently verified in the database.", ephemeral=True
            )
            return

        roles_removed = res.get("roles_removed", [])
        await interaction.followup.send(
            f"✅ **Successfully unverified user ID `{user_id}`**.\n"
            f"• Reason: *{reason}*\n"
            f"• Roles removed in {len(roles_removed)} server(s): {', '.join(roles_removed) if roles_removed else 'None'}",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)


class AlumniRevokeModal(ui.Modal, title="🎓 Revoke Alumni Status"):
    user_input = ui.TextInput(
        label="Alumni User ID or Mention",
        placeholder="e.g. 123456789012345678 or @alumni",
        required=True,
        max_length=64,
    )
    reason_input = ui.TextInput(
        label="Reason for Revocation",
        placeholder="e.g. Ineligible claim, incorrect graduation year",
        required=False,
        max_length=256,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, cog: AdminCog, dashboard_view: AdminDashboardView) -> None:
        super().__init__()
        self.cog = cog
        self.dashboard_view = dashboard_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        user_id = _extract_user_id(self.user_input.value)
        if not user_id:
            await interaction.followup.send("❌ Invalid User ID or mention provided.", ephemeral=True)
            return

        target_member = None
        if interaction.guild:
            target_member = interaction.guild.get_member(user_id)
        if not target_member:
            target_member = discord.Object(id=user_id)

        reason = self.reason_input.value.strip() or "Revoked via Admin Dashboard"
        res = await self.cog.service.revoke_alumni_status(
            target_user=target_member,
            admin=interaction.user,
            current_guild=interaction.guild,
            reason=reason,
        )

        if not res.get("success"):
            await interaction.followup.send(
                f"❌ {res.get('message', 'Failed to revoke alumni status.')}", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ **Successfully revoked Alumni status for user ID `{user_id}`**.\n"
            f"• Removed `TARUMT Alumni` role across **{res.get('roles_removed_count', 0)}** mutual server(s).\n"
            f"• Reason: *{reason}*",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)


class GuestRoleModal(ui.Modal, title="⚙️ Configure Guest Role Name"):
    role_name_input = ui.TextInput(
        label="Guest Role Name",
        placeholder="e.g. Guest (Approved) (leave empty to reset to 'Guest')",
        required=False,
        max_length=100,
    )

    def __init__(self, cog: AdminCog, dashboard_view: AdminDashboardView) -> None:
        super().__init__()
        self.cog = cog
        self.dashboard_view = dashboard_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Command must be used in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        role_name = self.role_name_input.value.strip() or "Guest"
        await self.cog.db.set_guild_guest_role(interaction.guild.id, role_name)

        await self.cog.db.log(
            "INFO",
            "CONFIG_GUEST_ROLE",
            f"Admin {interaction.user} set guest role for '{interaction.guild.name}' to '{role_name}'",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        embed = await self.dashboard_view.build_config_embed(interaction.guild)
        await self.dashboard_view.update_message(interaction, embed=embed)


class ChannelSelectComponent(ui.ChannelSelect):
    def __init__(self, cog: AdminCog, dashboard_view: AdminDashboardView, config_type: str) -> None:
        super().__init__(
            placeholder=f"Select channel for {config_type.title()}...",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            custom_id=f"admin_select_ch_{config_type}",
        )
        self.cog = cog
        self.dashboard_view = dashboard_view
        self.config_type = config_type

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not self.values:
            return
        await interaction.response.defer()
        selected_ch = self.values[0]
        channel_id = selected_ch.id

        if self.config_type == "welcome":
            await self.cog.db.set_guild_welcome_channel(interaction.guild.id, channel_id)
            verif_cog = self.cog.bot.get_cog("Verification")
            if verif_cog and hasattr(verif_cog, "invalidate_guild_cache"):
                verif_cog.invalidate_guild_cache(interaction.guild.id)
        elif self.config_type == "help":
            await self.cog.db.set_guild_help_channel(interaction.guild.id, channel_id)
            verif_cog = self.cog.bot.get_cog("Verification")
            if verif_cog and hasattr(verif_cog, "invalidate_guild_cache"):
                verif_cog.invalidate_guild_cache(interaction.guild.id)
        elif self.config_type == "review":
            await self.cog.db.set_guild_review_channel(interaction.guild.id, channel_id)
        elif self.config_type == "panel_deploy":
            from tarveri.cogs.guest_cog import VerificationGatewayView

            guest_service = getattr(self.cog.bot, "guest_service", None)
            if not guest_service:
                from tarveri.services.guest_service import GuestService

                guest_service = GuestService(
                    bot=self.cog.bot,
                    db=self.cog.db,
                    rate_limiter=self.cog.rate_limiter,
                    admin_role_name=self.cog.admin_role_name,
                )

            embed = discord.Embed(
                title="🎓 Welcome to the Server!",
                description=(
                    "Please choose how you would like to gain access to the server:\n\n"
                    "• 🎓 **TARUMT Students:** Click **Verify TARUMT Student** to submit your Student ID and receive your Faculty Role.\n"
                    "• 🎟️ **Have a Referral Code:** Click **Enter Referral Code** if a current student gave you an invite code.\n"
                    "• 🌐 **Outside Guests / Speakers:** Click **Apply as Guest** to request access from server administration."
                ),
                color=discord.Color.dark_teal(),
            )
            embed.set_footer(text="TARVeri Student & Guest Verification System")
            gateway_view = VerificationGatewayView(self.cog.service, guest_service)

            target_ch = interaction.guild.get_channel(channel_id)
            if isinstance(target_ch, discord.TextChannel):
                await target_ch.send(embed=embed, view=gateway_view)
                await interaction.followup.send(
                    f"✅ **Persistent Verification Gateway Panel deployed to {target_ch.mention}!**",
                    ephemeral=True,
                )
                return

        await self.cog.db.log(
            "INFO",
            f"CONFIG_{self.config_type.upper()}_CHANNEL",
            f"Admin {interaction.user} set {self.config_type} channel to {selected_ch.name} ({channel_id})",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        embed = await self.dashboard_view.build_config_embed(interaction.guild)
        await self.dashboard_view.update_message(interaction, embed=embed)


class RoleSelectComponent(ui.RoleSelect):
    def __init__(self, cog: AdminCog, dashboard_view: AdminDashboardView) -> None:
        super().__init__(
            placeholder="Select Admin / Reviewer Role...",
            min_values=1,
            max_values=1,
            custom_id="admin_select_admin_role",
        )
        self.cog = cog
        self.dashboard_view = dashboard_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not self.values:
            return
        await interaction.response.defer()
        selected_role = self.values[0]
        await self.cog.db.set_guild_admin_role(interaction.guild.id, selected_role.name)

        await self.cog.db.log(
            "INFO",
            "CONFIG_ADMIN_ROLE",
            f"Admin {interaction.user} set admin/reviewer role to '{selected_role.name}'",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        embed = await self.dashboard_view.build_config_embed(interaction.guild)
        await self.dashboard_view.update_message(interaction, embed=embed)


class AdminCategorySelect(ui.Select):
    def __init__(self, dashboard_view: AdminDashboardView) -> None:
        options = [
            discord.SelectOption(
                label="Overview & Statistics",
                value="overview",
                description="View verification metrics, faculty breakdown, and counts.",
                emoji="📊",
                default=dashboard_view.current_category == "overview",
            ),
            discord.SelectOption(
                label="Server Configuration",
                value="config",
                description="Configure welcome, help, review channels, and roles.",
                emoji="⚙️",
                default=dashboard_view.current_category == "config",
            ),
            discord.SelectOption(
                label="Diagnostics & Auto-Healing",
                value="diagnose",
                description="Check permissions, hierarchy, restore missing/SRC roles.",
                emoji="🩺",
                default=dashboard_view.current_category == "diagnose",
            ),
            discord.SelectOption(
                label="Member Moderation",
                value="moderation",
                description="Unverify student IDs and revoke Alumni status.",
                emoji="👥",
                default=dashboard_view.current_category == "moderation",
            ),
            discord.SelectOption(
                label="Guest Review Tickets",
                value="tickets",
                description="Inspect recent guest review tickets and verdicts.",
                emoji="🎟️",
                default=dashboard_view.current_category == "tickets",
            ),
            discord.SelectOption(
                label="Database Backups",
                value="backup",
                description="Manage SQLite database snapshots and restores.",
                emoji="💾",
                default=dashboard_view.current_category == "backup",
            ),
            discord.SelectOption(
                label="Logs & Archives",
                value="logs",
                description="Inspect daily log files and 10-day .tar.gz archives.",
                emoji="📋",
                default=dashboard_view.current_category == "logs",
            ),
            discord.SelectOption(
                label="Deploy Gateway Panel",
                value="panel",
                description="Post persistent 3-button verification panel to a channel.",
                emoji="🚀",
                default=dashboard_view.current_category == "panel",
            ),
            discord.SelectOption(
                label="Updates & Role Resync",
                value="updates",
                description="Check upstream git commits and force role resync.",
                emoji="🔄",
                default=dashboard_view.current_category == "updates",
            ),
        ]
        super().__init__(
            placeholder="Select Control Center Category...",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="admin_dashboard_category_select",
        )
        self.dashboard_view = dashboard_view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.dashboard_view.current_category = self.values[0]
        await self.dashboard_view.refresh_view(interaction)


class AdminDashboardView(ui.View):
    def __init__(
        self,
        cog: AdminCog,
        admin_user: discord.User | discord.Member,
        initial_category: str = "overview",
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.admin_user = admin_user
        self.current_category = initial_category
        self._rebuild_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_user.id or self.cog._check_admin(interaction):
            return True
        await interaction.response.send_message(
            "❌ You do not have permission to interact with this control center.", ephemeral=True
        )
        return False

    def _rebuild_components(self) -> None:
        self.clear_items()
        self.add_item(AdminCategorySelect(self))

        # Dynamic action buttons depending on the active category
        if self.current_category == "overview":
            btn_refresh = ui.Button(label="Refresh Metrics", style=discord.ButtonStyle.secondary, emoji="🔄")
            btn_refresh.callback = self._on_refresh_clicked
            self.add_item(btn_refresh)

            btn_diagnose = ui.Button(label="Run Diagnostics", style=discord.ButtonStyle.primary, emoji="🩺")
            btn_diagnose.callback = self._on_switch_diagnose
            self.add_item(btn_diagnose)

        elif self.current_category == "config":
            btn_set_welcome = ui.Button(label="Set Welcome Channel", style=discord.ButtonStyle.secondary, emoji="📢")
            btn_set_welcome.callback = lambda i: self._show_channel_select(i, "welcome")
            self.add_item(btn_set_welcome)

            btn_set_help = ui.Button(label="Set Help Channel", style=discord.ButtonStyle.secondary, emoji="❓")
            btn_set_help.callback = lambda i: self._show_channel_select(i, "help")
            self.add_item(btn_set_help)

            btn_set_review = ui.Button(label="Set Review Channel", style=discord.ButtonStyle.secondary, emoji="🎫")
            btn_set_review.callback = lambda i: self._show_channel_select(i, "review")
            self.add_item(btn_set_review)

            btn_set_guest_role = ui.Button(label="Set Guest Role", style=discord.ButtonStyle.secondary, emoji="👥")
            btn_set_guest_role.callback = self._on_set_guest_role_clicked
            self.add_item(btn_set_guest_role)

            btn_set_admin_role = ui.Button(label="Set Admin Role", style=discord.ButtonStyle.secondary, emoji="🛡️")
            btn_set_admin_role.callback = self._on_set_admin_role_clicked
            self.add_item(btn_set_admin_role)

            btn_toggle_email = ui.Button(label="Toggle Email Verification", style=discord.ButtonStyle.primary, emoji="📧")
            btn_toggle_email.callback = self._on_toggle_email_verification_clicked
            self.add_item(btn_toggle_email)

            btn_toggle_email_enforce = ui.Button(label="Toggle Role Enforcement", style=discord.ButtonStyle.secondary, emoji="🛡️")
            btn_toggle_email_enforce.callback = self._on_toggle_email_enforcement_clicked
            self.add_item(btn_toggle_email_enforce)

            btn_reset_auto = ui.Button(label="Reset to Auto-Detect", style=discord.ButtonStyle.danger, emoji="🔄")
            btn_reset_auto.callback = self._on_reset_config_clicked
            self.add_item(btn_reset_auto)

        elif self.current_category == "diagnose":
            btn_run_diag = ui.Button(label="Run Diagnostics Now", style=discord.ButtonStyle.success, emoji="🩺")
            btn_run_diag.callback = self._on_run_diagnostics_now
            self.add_item(btn_run_diag)

        elif self.current_category == "moderation":
            btn_unverify = ui.Button(label="Unverify Student", style=discord.ButtonStyle.danger, emoji="❌")
            btn_unverify.callback = self._on_unverify_clicked
            self.add_item(btn_unverify)

            btn_alumni_revoke = ui.Button(label="Revoke Alumni Status", style=discord.ButtonStyle.secondary, emoji="🎓")
            btn_alumni_revoke.callback = self._on_alumni_revoke_clicked
            self.add_item(btn_alumni_revoke)

        elif self.current_category == "tickets":
            btn_refresh_tickets = ui.Button(label="Refresh Tickets", style=discord.ButtonStyle.secondary, emoji="🔄")
            btn_refresh_tickets.callback = self._on_refresh_tickets
            self.add_item(btn_refresh_tickets)

        elif self.current_category == "backup":
            btn_create_backup = ui.Button(label="Create Backup Now", style=discord.ButtonStyle.success, emoji="💾")
            btn_create_backup.callback = self._on_create_backup_clicked
            self.add_item(btn_create_backup)

            btn_restore_settings = ui.Button(label="Restore Latest Settings", style=discord.ButtonStyle.secondary, emoji="🔄")
            btn_restore_settings.callback = self._on_restore_settings_clicked
            self.add_item(btn_restore_settings)

        elif self.current_category == "logs":
            btn_archive_logs = ui.Button(label="Run 10-Day Archival (.tar.gz)", style=discord.ButtonStyle.success, emoji="🗜️")
            btn_archive_logs.callback = self._on_archive_logs_clicked
            self.add_item(btn_archive_logs)

            btn_tail_logs = ui.Button(label="View Recent Log Lines", style=discord.ButtonStyle.secondary, emoji="📄")
            btn_tail_logs.callback = self._on_tail_logs_clicked
            self.add_item(btn_tail_logs)

        elif self.current_category == "panel":
            btn_deploy_here = ui.Button(label="Deploy to Current Channel", style=discord.ButtonStyle.success, emoji="🚀")
            btn_deploy_here.callback = self._on_deploy_here_clicked
            self.add_item(btn_deploy_here)

            btn_deploy_pick = ui.Button(label="Deploy to Another Channel...", style=discord.ButtonStyle.secondary, emoji="📢")
            btn_deploy_pick.callback = lambda i: self._show_channel_select(i, "panel_deploy")
            self.add_item(btn_deploy_pick)

        elif self.current_category == "updates":
            btn_check_up = ui.Button(label="Check Upstream Commits", style=discord.ButtonStyle.primary, emoji="🔄")
            btn_check_up.callback = self._on_check_updates_clicked
            self.add_item(btn_check_up)

            btn_resync = ui.Button(label="Resync Mutual Server Roles", style=discord.ButtonStyle.secondary, emoji="🔄")
            btn_resync.callback = self._on_resync_clicked
            self.add_item(btn_resync)

    async def refresh_view(self, interaction: discord.Interaction) -> None:
        self._rebuild_components()
        embed = await self.build_current_embed(interaction.guild)
        await self.update_message(interaction, embed=embed)

    async def update_message(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.edit_original_response(embed=embed, view=self)

    async def build_current_embed(self, guild: discord.Guild | None) -> discord.Embed:
        if self.current_category == "overview":
            return await self.build_overview_embed(guild)
        elif self.current_category == "config":
            return await self.build_config_embed(guild)
        elif self.current_category == "diagnose":
            return await self.build_diagnose_embed(guild)
        elif self.current_category == "moderation":
            return await self.build_moderation_embed(guild)
        elif self.current_category == "tickets":
            return await self.build_tickets_embed(guild)
        elif self.current_category == "backup":
            return await self.build_backup_embed(guild)
        elif self.current_category == "logs":
            return await self.build_logs_embed(guild)
        elif self.current_category == "panel":
            return await self.build_panel_embed(guild)
        elif self.current_category == "updates":
            return await self.build_updates_embed(guild)
        return await self.build_overview_embed(guild)

    # --- Embed Builders ---

    async def build_overview_embed(self, guild: discord.Guild | None) -> discord.Embed:
        total = await self.cog.db.total_verified()
        total_alumni = await self.cog.db.count_alumni()
        faculty_counts = await self.cog.db.counts_by_faculty()
        last_24h = await self.cog.db.verified_in_last(24)
        last_7d = await self.cog.db.verified_in_last(24 * 7)

        embed = discord.Embed(
            title="🛡️ TARVeri — Administrator Control Center",
            description="Welcome to the unified server management and moderation dashboard.",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Total Verified Students", value=f"**{total}** students", inline=True)
        embed.add_field(name="Graduated Alumni", value=f"**{total_alumni}** alumni", inline=True)
        embed.add_field(name="Active Guilds", value=f"**{len(self.cog.bot.guilds)}** servers", inline=True)
        embed.add_field(name="Verified Past 24h", value=f"**{last_24h}** new", inline=True)
        embed.add_field(name="Verified Past 7d", value=f"**{last_7d}** new", inline=True)

        email_stats = await self.cog.db.get_email_verification_stats(guild.id if guild else None)
        email_status_text = (
            f"**{email_stats['email_verified_students']}** / **{total}** ({email_stats['email_verified_rate']}%)"
        )
        embed.add_field(name="📧 Email Verified Students", value=email_status_text, inline=True)
        if guild:
            opt_status = "🔒 **Mandatory (Opted In)**" if email_stats["guild_opted_in"] else "⚪ **Optional (Default: Opted Out)**"
            embed.add_field(name="📧 Server Email Policy", value=opt_status, inline=True)

        if faculty_counts:
            breakdown_lines = []
            for f_code, count in faculty_counts:
                faculty_name = resolve_faculty_role(f_code) or FACULTY_ROLES.get(f_code, f"Code {f_code}")
                percentage = (count / total * 100) if total > 0 else 0
                breakdown_lines.append(f"• **{faculty_name}** (`{f_code}`): {count} ({percentage:.1f}%)")
            embed.add_field(name="Faculty Distribution", value="\n".join(breakdown_lines), inline=False)
        else:
            embed.add_field(name="Faculty Distribution", value="No student verifications recorded yet.", inline=False)

        embed.set_footer(text="Use the dropdown menu below to navigate categories.")
        return embed

    async def build_config_embed(self, guild: discord.Guild | None) -> discord.Embed:
        embed = discord.Embed(
            title="⚙️ Server Channel & Role Configuration",
            description=f"Current settings for **{guild.name if guild else 'Server'}**:",
            color=discord.Color.dark_teal(),
        )
        if not guild:
            embed.description = "Configuration requires a Discord server context."
            return embed

        settings = await self.cog.db.get_guild_settings(guild.id)
        w_id = settings[0] if settings else None
        h_id = settings[1] if settings else None
        g_role = settings[2] if settings and len(settings) > 2 and settings[2] else "Guest"
        r_id = settings[3] if settings and len(settings) > 3 else None
        adm_role = settings[4] if settings and len(settings) > 4 and settings[4] else f"Auto-detect ({self.cog.admin_role_name})"
        is_email_opted_in = bool(settings[5]) if settings and len(settings) > 5 else False
        is_email_enforced = bool(settings[6]) if settings and len(settings) > 6 else False

        w_ch = guild.get_channel(w_id) if w_id else None
        h_ch = guild.get_channel(h_id) if h_id else None
        r_ch = guild.get_channel(r_id) if r_id else None

        embed.add_field(
            name="📢 Welcome Channel",
            value=w_ch.mention if w_ch else (f"`ID: {w_id}`" if w_id else "*Auto-detect (#welcome / #verify)*"),
            inline=True,
        )
        embed.add_field(
            name="❓ Help Channel",
            value=h_ch.mention if h_ch else (f"`ID: {h_id}`" if h_id else "*Auto-detect (#help / #support)*"),
            inline=True,
        )
        embed.add_field(
            name="🎫 Guest Review Channel",
            value=r_ch.mention if r_ch else (f"`ID: {r_id}`" if r_id else "*Auto-detect (#tickets / #reviews)*"),
            inline=True,
        )
        embed.add_field(name="👥 Guest Role Name", value=f"`{g_role}`", inline=True)
        embed.add_field(name="🛡️ Admin / Reviewer Role", value=f"`{adm_role}`", inline=True)
        email_mode_str = "🔒 **Mandatory (Opted In)**" if is_email_opted_in else "⚪ **Optional (Default: Opted Out)**"
        embed.add_field(name="📧 Email Verification Policy", value=email_mode_str, inline=True)
        enforce_mode_str = "🔴 **Enforced (Retroactive Stripping)**" if is_email_enforced else "🟢 **Disabled (Existing Roles Preserved)**"
        embed.add_field(name="🛡️ Email Role Enforcement", value=enforce_mode_str, inline=True)

        embed.set_footer(text="Use the buttons below to modify channels, roles, or reset to defaults.")
        return embed

    async def build_diagnose_embed(self, guild: discord.Guild | None) -> discord.Embed:
        if not guild:
            return discord.Embed(title="🩺 Diagnostics", description="Must be run inside a Discord server.", color=discord.Color.red())

        warnings = self.cog.service.diagnose_guild_permissions(guild)
        embed = discord.Embed(
            title=f"🩺 Server Health & Self-Healing Diagnostics — {guild.name}",
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
                value="All required bot permissions and role positions are healthy.",
                inline=False,
            )

        embed.add_field(
            name="🔄 Self-Healing Capabilities",
            value=(
                "• **SRC Roles**: Auto-restores missing faculty SRC roles.\n"
                "• **Role Deduplication**: Migrates members and cleans bot duplicates.\n"
                "• **Member Role Reconciliation**: Restores faculty/alumni roles to verified students."
            ),
            inline=False,
        )
        embed.set_footer(text="Click 'Run Diagnostics Now' to execute full self-healing.")
        return embed

    async def build_moderation_embed(self, guild: discord.Guild | None) -> discord.Embed:
        total = await self.cog.db.total_verified()
        total_alumni = await self.cog.db.count_alumni()

        embed = discord.Embed(
            title="👥 Member Moderation & Verification Actions",
            description=(
                f"Currently managing **{total}** verified student(s) and **{total_alumni}** graduated alumni.\n\n"
                "Choose a moderation action below to open the corresponding input form:"
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="❌ Unverify Student",
            value="Unlinks student ID hash, resets rate limiting, and revokes faculty roles across mutual servers.",
            inline=False,
        )
        embed.add_field(
            name="🎓 Revoke Alumni Status",
            value="Revokes alumni graduation claim and removes `TARUMT Alumni` role across mutual servers.",
            inline=False,
        )
        return embed

    async def build_tickets_embed(self, guild: discord.Guild | None, status_filter: str | None = None) -> discord.Embed:
        if not guild:
            return discord.Embed(title="🎟️ Guest Review Tickets", description="Must be run in a server.", color=discord.Color.red())

        tickets = await self.cog.db.list_guest_tickets(guild.id, status=status_filter, limit=6)
        embed = discord.Embed(
            title=f"🎟️ Recent Guest Review Tickets — {guild.name}",
            color=discord.Color.dark_teal(),
        )
        if not tickets:
            embed.description = f"No guest review tickets found{' with status ' + status_filter if status_filter else ''}."
            return embed

        embed.description = f"Showing **{len(tickets)}** recent ticket(s):"
        for t in tickets:
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
            reason_info = f"\n> *\"{t['close_reason']}\"*" if t.get("close_reason") else ""

            embed.add_field(
                name=f"{status_emoji} Ticket #{seq_code} — {t_status}",
                value=f"**Applicant:** {applicant_mention} | **Thread:** {thread_mention}\n**Created:** `{t.get('created_at', 'N/A')}`{reason_info}",
                inline=False,
            )
        return embed

    async def build_backup_embed(self, guild: discord.Guild | None) -> discord.Embed:
        settings = getattr(self.cog.bot, "settings", None)
        backup_dir = settings.backup_dir if settings and isinstance(getattr(settings, "backup_dir", None), str) else "backups"
        max_backups = settings.max_backups if settings and isinstance(getattr(settings, "max_backups", None), int) else 10
        backups = self.cog.db.list_backups(backup_dir=backup_dir)
        embed = discord.Embed(
            title="💾 Database Backups & Snapshot Snapshots",
            description=f"Backups are stored in `{backup_dir}` and rotated up to **{max_backups}** files.",
            color=discord.Color.green() if backups else discord.Color.blue(),
        )
        if backups:
            lines = []
            for b in backups[:7]:
                size_kb = b.get("size_bytes", 0) / 1024 if "size_bytes" in b else b.get("size_kb", 0)
                time_str = b.get("timestamp") or b.get("created_at") or "N/A"
                lines.append(f"• 📦 `{b['filename']}` — {size_kb:.1f} KB (`{time_str}`)")
            embed.add_field(name=f"Available Snapshots ({len(backups)} total)", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Available Snapshots", value="No snapshots found in backup directory.", inline=False)
        return embed

    async def build_logs_embed(self, guild: discord.Guild | None) -> discord.Embed:
        settings = getattr(self.cog.bot, "settings", None)
        logs_dir = settings.logs_dir if settings and isinstance(getattr(settings, "logs_dir", None), str) else "logs"
        tz_name = settings.timezone_name if settings and isinstance(getattr(settings, "timezone_name", None), str) else "Asia/Kuala_Lumpur"

        daily_logs = list_daily_logs(logs_dir=logs_dir, tz_name=tz_name)
        archives = list_log_archives(logs_dir=logs_dir)

        embed = discord.Embed(
            title=f"📋 TARVeri Log Management & Historical Archives (`{logs_dir}/`)",
            color=discord.Color.blue(),
        )
        if daily_logs:
            daily_desc = [f"• 📄 `{dl['filename']}` — {dl['size_bytes'] / 1024:.1f} KB ({dl['lines']} lines)" for dl in daily_logs[:5]]
            embed.add_field(name=f"Active Daily Logs ({len(daily_logs)} total)", value="\n".join(daily_desc), inline=False)
        else:
            embed.add_field(name="Active Daily Logs", value="No daily log files found.", inline=False)

        if archives:
            archive_desc = [f"• 📦 `{ar['filename']}` — {ar['size_bytes'] / 1024:.1f} KB ({ar['file_count']} logs bundled)" for ar in archives[:5]]
            embed.add_field(name=f"10-Day Archives ({len(archives)} total)", value="\n".join(archive_desc), inline=False)
        else:
            embed.add_field(name="10-Day Archives", value="No archives created yet (logs $>10$ days old are compressed).", inline=False)

        return embed

    async def build_panel_embed(self, guild: discord.Guild | None) -> discord.Embed:
        embed = discord.Embed(
            title="🚀 Deploy Verification Gateway Panel",
            description=(
                "The **Verification Gateway Panel** is a permanent 3-button message that allows newcomers to easily:\n\n"
                "1. 🎓 **Verify TARUMT Student**: Submit student ID via modal.\n"
                "2. 🎟️ **Enter Referral Code**: Submit invite referral code.\n"
                "3. 🌐 **Apply as Guest**: Fill out guest application form.\n\n"
                "Click below to deploy it to the current channel or pick a specific channel."
            ),
            color=discord.Color.dark_teal(),
        )
        return embed

    async def build_updates_embed(self, guild: discord.Guild | None) -> discord.Embed:
        checker = self.cog.update_checker
        if not checker:
            checker = UpdateCheckerService(bot=self.cog.bot, db=self.cog.db, update_stream="auto")

        is_avail, count, local_h, remote_h, target_stream = await checker.check_for_updates()
        embed = discord.Embed(
            title="🔄 TARVeri Update Checker & Synchronization",
            color=discord.Color.green() if not is_avail else discord.Color.gold(),
        )
        embed.add_field(name="Target Stream", value=f"`{target_stream}`", inline=False)
        embed.add_field(name="Local Commit", value=f"`{local_h[:7]}`" if local_h else "*Unknown*", inline=True)
        embed.add_field(name="Remote Commit", value=f"`{remote_h[:7]}`" if remote_h else "*Unknown*", inline=True)

        if is_avail:
            branch_arg = target_stream.replace("origin/", "").strip()
            embed.description = (
                f"🔔 **Update available!** Remote is **{count} commit(s)** ahead.\n"
                f"To update, run `./scripts/update.sh {branch_arg}` on your host terminal."
            )
        else:
            embed.description = "✅ TARVeri is running the latest version on this stream."

        return embed

    # --- Button Callbacks ---

    async def _on_refresh_clicked(self, interaction: discord.Interaction) -> None:
        await self.refresh_view(interaction)

    async def _on_switch_diagnose(self, interaction: discord.Interaction) -> None:
        self.current_category = "diagnose"
        await self.refresh_view(interaction)

    async def _on_run_diagnostics_now(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        await interaction.response.defer()
        guild = interaction.guild

        src_stats = await self.cog.service.restore_src_roles(guild)
        dedup_stats = await self.cog.service.reconcile_duplicate_roles(guild)
        warnings = self.cog.service.diagnose_guild_permissions(guild)
        reconcile_stats = await self.cog.service.reconcile_verified_members(guild)
        alumni_stats = await self.cog.service.reconcile_alumni_members(guild)

        embed = discord.Embed(
            title=f"🩺 Server Diagnostics & Self-Healing Results — {guild.name}",
            color=discord.Color.green() if not warnings else discord.Color.orange(),
        )
        if warnings:
            embed.add_field(name="⚠️ Issues Detected", value="\n".join(f"• {w}" for w in warnings), inline=False)
        else:
            embed.add_field(name="✅ Health", value="All bot permissions and role positions are healthy.", inline=False)

        embed.add_field(
            name="🏛️ SRC Role Restorations",
            value=f"• Restored **{src_stats.get('created', 0)}** missing SRC role(s)",
            inline=True,
        )
        embed.add_field(
            name="🧹 Duplicate Role Cleanup",
            value=f"• Deleted **{dedup_stats.get('deleted_roles', 0)}** role(s), Migrated **{dedup_stats.get('migrated_members', 0)}** member(s)",
            inline=True,
        )
        embed.add_field(
            name="🔄 Role Reconciliation",
            value=f"• Verified students: checked {reconcile_stats['checked']}, restored {reconcile_stats['restored']}\n• Alumni: checked {alumni_stats['checked']}, restored {alumni_stats['restored']}",
            inline=False,
        )

        await self.update_message(interaction, embed=embed)

    async def _on_unverify_clicked(self, interaction: discord.Interaction) -> None:
        modal = UnverifyModal(self.cog, self)
        await interaction.response.send_modal(modal)

    async def _on_alumni_revoke_clicked(self, interaction: discord.Interaction) -> None:
        modal = AlumniRevokeModal(self.cog, self)
        await interaction.response.send_modal(modal)

    async def _on_set_guest_role_clicked(self, interaction: discord.Interaction) -> None:
        modal = GuestRoleModal(self.cog, self)
        await interaction.response.send_modal(modal)

    async def _on_set_admin_role_clicked(self, interaction: discord.Interaction) -> None:
        self.clear_items()
        self.add_item(AdminCategorySelect(self))
        self.add_item(RoleSelectComponent(self.cog, self))
        embed = await self.build_config_embed(interaction.guild)
        embed.set_footer(text="Select the Admin / Reviewer role from the menu below.")
        await self.update_message(interaction, embed=embed)

    async def _show_channel_select(self, interaction: discord.Interaction, config_type: str) -> None:
        self.clear_items()
        self.add_item(AdminCategorySelect(self))
        self.add_item(ChannelSelectComponent(self.cog, self, config_type))
        embed = (
            await self.build_config_embed(interaction.guild)
            if config_type != "panel_deploy"
            else await self.build_panel_embed(interaction.guild)
        )
        embed.set_footer(text=f"Select the target channel for {config_type.replace('_', ' ').title()}.")
        await self.update_message(interaction, embed=embed)

    async def _on_toggle_email_verification_clicked(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Server context required.", ephemeral=True)
            return
        await interaction.response.defer()
        curr = await self.cog.db.is_guild_email_verification_enabled(interaction.guild.id)
        new_val = not curr
        await self.cog.db.set_guild_email_verification(interaction.guild.id, new_val)
        status_word = "**MANDATORY (Opted In)**" if new_val else "**OPTIONAL (Opted Out)**"
        await self.cog.db.log(
            "INFO",
            "GUILD_EMAIL_VERIFICATION_TOGGLED",
            f"Email verification requirement set to {new_val} by admin {interaction.user}",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )
        self._rebuild_components()
        embed = await self.build_config_embed(interaction.guild)
        embed.description = f"📧 **Email verification requirement updated to {status_word}!**"
        await self.update_message(interaction, embed=embed)

    async def _on_toggle_email_enforcement_clicked(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("❌ Server context required.", ephemeral=True)
            return
        await interaction.response.defer()
        curr = await self.cog.db.is_guild_email_enforcement_enabled(interaction.guild.id)
        new_val = not curr
        await self.cog.db.set_guild_email_enforcement(interaction.guild.id, new_val)
        status_word = "🔴 **ENFORCED (Retroactive Role Stripping Active)**" if new_val else "🟢 **DISABLED (Existing Roles Preserved)**"
        await self.cog.db.log(
            "INFO",
            "GUILD_EMAIL_ENFORCEMENT_TOGGLED",
            f"Email role enforcement set to {new_val} by admin {interaction.user}",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )
        self._rebuild_components()
        embed = await self.build_config_embed(interaction.guild)
        embed.description = f"🛡️ **Email role enforcement policy updated to {status_word}!**"
        await self.update_message(interaction, embed=embed)

    async def _on_reset_config_clicked(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        await interaction.response.defer()
        await self.cog.db.set_guild_welcome_channel(interaction.guild.id, None)
        await self.cog.db.set_guild_help_channel(interaction.guild.id, None)
        await self.cog.db.set_guild_review_channel(interaction.guild.id, None)
        await self.cog.db.set_guild_guest_role(interaction.guild.id, "Guest")
        await self.cog.db.set_guild_admin_role(interaction.guild.id, None)
        await self.cog.db.set_guild_email_verification(interaction.guild.id, False)
        await self.cog.db.set_guild_email_enforcement(interaction.guild.id, False)

        verif_cog = self.cog.bot.get_cog("Verification")
        if verif_cog and hasattr(verif_cog, "invalidate_guild_cache"):
            verif_cog.invalidate_guild_cache(interaction.guild.id)

        embed = await self.build_config_embed(interaction.guild)
        embed.description = "🔄 **All server channels and roles have been reset to automatic auto-detection!**"
        await self.update_message(interaction, embed=embed)

    async def _on_refresh_tickets(self, interaction: discord.Interaction) -> None:
        await self.refresh_view(interaction)

    async def _on_create_backup_clicked(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        backup_path = await self.cog.db.create_backup()
        embed = await self.build_backup_embed(interaction.guild)
        embed.description = f"✅ **Database snapshot created successfully:** `{backup_path}`"
        await self.update_message(interaction, embed=embed)

    async def _on_restore_settings_clicked(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        restored = await self.cog.db.restore_latest_guild_settings()
        embed = await self.build_backup_embed(interaction.guild)
        if restored:
            embed.description = f"✅ Restored `{restored}` guild settings record(s) from latest backup snapshot."
        else:
            embed.description = "ℹ️ No previous settings backup found to restore."
        await self.update_message(interaction, embed=embed)

    async def _on_archive_logs_clicked(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        logs_dir = getattr(self.cog.bot, "settings", None) and self.cog.bot.settings.logs_dir or "logs"
        tz_name = getattr(self.cog.bot, "settings", None) and self.cog.bot.settings.timezone_name or "Asia/Kuala_Lumpur"
        results = archive_old_logs(logs_dir=logs_dir, older_than_days=10, tz_name=tz_name)
        embed = await self.build_logs_embed(interaction.guild)
        if results:
            total_saved = sum(r["space_saved_bytes"] for r in results) / 1024
            embed.description = f"✅ Successfully created **{len(results)}** archive(s), saving **{total_saved:.1f} KB**."
        else:
            embed.description = "ℹ️ No daily logs older than 10 days needed compression."
        await self.update_message(interaction, embed=embed)

    async def _on_tail_logs_clicked(self, interaction: discord.Interaction) -> None:
        logs_dir = getattr(self.cog.bot, "settings", None) and self.cog.bot.settings.logs_dir or "logs"
        tz_name = getattr(self.cog.bot, "settings", None) and self.cog.bot.settings.timezone_name or "Asia/Kuala_Lumpur"
        daily_logs = list_daily_logs(logs_dir=logs_dir, tz_name=tz_name)
        if not daily_logs:
            await interaction.response.send_message("⚠️ No active log file found.", ephemeral=True)
            return

        latest_log_path = daily_logs[0]["path"]
        try:
            with open(latest_log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            tail_lines = lines[-15:] if len(lines) > 15 else lines
            content = "".join(tail_lines)
            if len(content) > 1900:
                content = content[-1900:]
            await interaction.response.send_message(
                f"📄 **Latest Logs** (`{daily_logs[0]['filename']}`):\n```text\n{content}\n```",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=90.0)
        except Exception as e:
            logger.warning("Failed to read log file for admin dashboard: %s", e, exc_info=True)
            await interaction.response.send_message(f"❌ Failed to read log file: {e}", ephemeral=True)

    async def _on_deploy_here_clicked(self, interaction: discord.Interaction) -> None:
        target_ch = interaction.channel
        if not isinstance(target_ch, discord.TextChannel):
            await interaction.response.send_message("❌ Current channel is not a text channel.", ephemeral=True)
            return

        from tarveri.cogs.guest_cog import VerificationGatewayView

        guest_service = getattr(self.cog.bot, "guest_service", None)
        if not guest_service:
            from tarveri.services.guest_service import GuestService

            guest_service = GuestService(
                bot=self.cog.bot,
                db=self.cog.db,
                rate_limiter=self.cog.rate_limiter,
                admin_role_name=self.cog.admin_role_name,
            )

        embed = discord.Embed(
            title="🎓 Welcome to the Server!",
            description=(
                "Please choose how you would like to gain access to the server:\n\n"
                "• 🎓 **TARUMT Students:** Click **Verify TARUMT Student** to submit your Student ID and receive your Faculty Role.\n"
                "• 🎟️ **Have a Referral Code:** Click **Enter Referral Code** if a current student gave you an invite code.\n"
                "• 🌐 **Outside Guests / Speakers:** Click **Apply as Guest** to request access from server administration."
            ),
            color=discord.Color.dark_teal(),
        )
        embed.set_footer(text="TARVeri Student & Guest Verification System")
        gateway_view = VerificationGatewayView(self.cog.service, guest_service)
        await target_ch.send(embed=embed, view=gateway_view)

        await interaction.response.send_message(
            f"✅ Verification gateway panel posted to {target_ch.mention}!", ephemeral=True
        )
        schedule_ttl_delete(interaction, delay=60.0)

    async def _on_check_updates_clicked(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        embed = await self.build_updates_embed(interaction.guild)
        await self.update_message(interaction, embed=embed)

    async def _on_resync_clicked(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        await interaction.response.defer()
        guild = interaction.guild
        reconcile_stats = await self.cog.service.reconcile_verified_members(guild)
        alumni_stats = await self.cog.service.reconcile_alumni_members(guild)
        embed = await self.build_updates_embed(guild)
        embed.description = (
            f"✅ **Mutual Server Role Resynchronization Complete!**\n"
            f"• Verified Students: checked {reconcile_stats['checked']}, restored {reconcile_stats['restored']}\n"
            f"• Alumni: checked {alumni_stats['checked']}, restored {alumni_stats['restored']}"
        )
        await self.update_message(interaction, embed=embed)
