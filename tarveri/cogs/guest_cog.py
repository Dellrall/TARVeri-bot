"""
Guest cog providing UI modals, persistent gateway buttons, referral code commands,
and private thread review orchestration.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from tarveri.config import (
    FACULTY_ROLE_NAMES,
    get_configured_tz,
    is_expiry_date_anomalous,
    parse_card_expiry_date,
)
from tarveri.database import Database
from tarveri.services.guest_service import GuestService
from tarveri.services.verification_service import VerificationService
from tarveri.utils import format_ticket_seq, schedule_ttl_delete

logger = logging.getLogger("tarveri")


def get_admin_role_or_fallback(guild: discord.Guild, configured_role_name: str = "TARVeri Admin") -> discord.Role | None:
    """
    Intelligently discovers the server's administrator/moderator role in priority order:
    1. Configured admin role name (e.g. 'TARVeri Admin' or custom setting)
    2. Common administrative role names: 'Admin', 'Administrator', 'Staff', 'Moderator', 'Mod'
    3. Server roles with Administrator or Manage Guild permissions.
    """
    if not guild or not hasattr(guild, "roles"):
        return None

    roles = list(guild.roles) if isinstance(guild.roles, (list, tuple, set)) else []
    if not roles:
        return None

    if configured_role_name:
        for r in roles:
            if r.name == configured_role_name or r.name.lower() == configured_role_name.lower():
                return r

    aliases = (
        "tarveri admin",
        "server admin",
        "admin",
        "administrator",
        "administrators",
        "management",
        "moderator",
        "moderators",
        "mod",
        "mods",
        "staff",
    )
    for alias in aliases:
        for r in roles:
            if r.name.lower() == alias:
                perms = getattr(r, "permissions", None)
                if perms and (
                    getattr(perms, "administrator", False)
                    or getattr(perms, "manage_guild", False)
                    or getattr(perms, "manage_roles", False)
                    or getattr(perms, "moderate_members", False)
                    or getattr(perms, "kick_members", False)
                    or getattr(perms, "ban_members", False)
                ) or alias in ("tarveri admin", "server admin", "admin", "administrator"):
                    return r

    for r in reversed(roles):
        if getattr(r, "is_default", lambda: False)():
            continue
        perms = getattr(r, "permissions", None)
        if perms and (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)):
            return r

    return None



def get_admin_role_mention(guild: discord.Guild, admin_role_name: str = "TARVeri Admin") -> str:
    """Returns a clickable role mention or owner mention if no admin role found."""
    role = get_admin_role_or_fallback(guild, admin_role_name)
    if role:
        return role.mention
    if getattr(guild, "owner", None):
        return f"<@{guild.owner_id}>"
    return "@Staff"


def is_admin_or_has_role(interaction: discord.Interaction, admin_role_name: str) -> bool:
    """Checks if the user has Administrator permission, the configured admin role, or standard admin roles."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    admin_role = get_admin_role_or_fallback(interaction.guild, admin_role_name)
    if admin_role and admin_role in interaction.user.roles:
        return True
    return any(r.name.lower() == admin_role_name.lower() for r in interaction.user.roles)



class StudentVerificationModal(discord.ui.Modal, title="🎓 TARUMT Student Verification"):
    student_id = discord.ui.TextInput(
        label="Student ID",
        placeholder="e.g. 23WMD09867 or 22PMR12345",
        min_length=7,
        max_length=20,
        required=True,
    )
    student_email = discord.ui.TextInput(
        label="Student Email (@student.tarc.edu.my)",
        placeholder="e.g. 23wmd09867@student.tarc.edu.my (Optional)",
        min_length=5,
        max_length=100,
        required=False,
    )
    card_expiry = discord.ui.TextInput(
        label="Student Card Expiry Date (MM/YY)",
        placeholder="e.g. 10/26 (Optional)",
        min_length=4,
        max_length=12,
        required=False,
    )

    def __init__(
        self,
        verification_service: VerificationService,
        email_service: Any = None,
        require_email: bool = False,
    ) -> None:
        super().__init__()
        self.verification_service = verification_service
        self.email_service = email_service or getattr(verification_service, "email_service", None)
        self.require_email = require_email
        current_yy = str(datetime.now().year)[-2:]
        self.student_id.placeholder = f"e.g. {current_yy}WMD09867 or {int(current_yy)-1:02d}PMR12345"
        self.card_expiry.placeholder = f"e.g. 10/{(int(current_yy) + 2) % 100:02d} (Optional)"

        is_global_email_active = bool(
            self.email_service and getattr(self.email_service, "is_enabled", False) is True
        )
        if is_global_email_active and self.require_email:
            self.student_email.required = True
            self.student_email.placeholder = f"e.g. {current_yy}wmd09867@student.tarc.edu.my (Required)"
        else:
            self.student_email.required = False
            self.student_email.placeholder = f"e.g. {current_yy}wmd09867@student.tarc.edu.my (Optional)"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_expiry = self.card_expiry.value.strip() if self.card_expiry.value else None
        student_id_val = self.student_id.value.strip()
        student_email_val = self.student_email.value.strip() if self.student_email.value else None

        if raw_expiry:
            iso_expiry = parse_card_expiry_date(raw_expiry)
            if not iso_expiry:
                await interaction.response.defer(ephemeral=True)
                current_yy = str(datetime.now().year)[-2:]
                await interaction.followup.send(
                    f"❌ Invalid student card expiry date format. Please use `MM/YY` (e.g. `10/{(int(current_yy) + 2) % 100:02d}`) "
                    f"or `DD/MM/YYYY` (e.g. `31/10/{datetime.now().year + 2}`), or leave it blank to auto-calculate.",
                    ephemeral=True,
                )
                schedule_ttl_delete(interaction, delay=30.0)
                return

            is_anomalous, anomaly_reason = is_expiry_date_anomalous(iso_expiry, student_id=student_id_val)
            if is_anomalous:
                await interaction.response.defer(ephemeral=True)
                from tarveri.cogs.verification_cog import (
                    ExpiryAnomalyConfirmView,
                    build_expiry_anomaly_embed,
                )

                db = getattr(self.verification_service, "db", None)
                embed = build_expiry_anomaly_embed(
                    raw_input=raw_expiry,
                    parsed_iso=iso_expiry,
                    student_id=student_id_val,
                    anomaly_reason=anomaly_reason or "",
                )
                view = ExpiryAnomalyConfirmView(
                    service=self.verification_service,
                    db=db,
                    student_id=student_id_val,
                    raw_expiry_input=raw_expiry,
                    parsed_iso_date=iso_expiry,
                    anomaly_reason=anomaly_reason or "",
                )
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                schedule_ttl_delete(interaction, delay=180.0)
                return

        # Check if email is required (per-guild policy or modal param)
        guild_id = interaction.guild.id if interaction.guild else None
        db = getattr(self.verification_service, "db", None)
        guild_email_required = self.require_email
        if not guild_email_required and guild_id and db:
            guild_email_required = await db.is_guild_email_verification_enabled(guild_id)

        is_email_active = bool(
            self.email_service and getattr(self.email_service, "is_enabled", False) is True
        )
        restrict_smtp = bool(
            self.email_service
            and getattr(self.email_service.settings, "email_restrict_smtp_usage", True) is True
        )

        # 1. If email is mandated for this guild, ensure student provided an email
        if is_email_active and guild_email_required and not student_email_val:
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(
                "❌ Institutional student email is required for verification in this server. Please enter your official TARUMT email (e.g. `@student.tarc.edu.my`).",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=30.0)
            return

        # 2. Trigger OTP flow if:
        #    - guild explicitly opted-in (guild_email_required), OR
        #    - SMTP usage is NOT restricted (restrict_smtp is False) and student provided an email
        should_send_otp = is_email_active and student_email_val and (guild_email_required or not restrict_smtp)
        if should_send_otp:
            await interaction.response.defer(ephemeral=True)
            from tarveri.cogs.verification_cog import OtpVerificationPromptView
            from tarveri.config import mask_email

            server_name = interaction.guild.name if interaction.guild else "TARUMT Community"
            send_result = await self.email_service.generate_and_send_otp(
                user_id=interaction.user.id,
                student_id=student_id_val,
                email_address=student_email_val,
                server_name=server_name,
                card_expiry_date=raw_expiry,
            )
            if not send_result["success"]:
                await interaction.followup.send(
                    f"❌ {send_result['error']}",
                    ephemeral=True,
                )
                schedule_ttl_delete(interaction, delay=30.0)
                return

            expire_ts = int(time.time()) + int(self.email_service.settings.email_otp_ttl_seconds)
            embed = discord.Embed(
                title="📬 Verification Code Sent!",
                description=(
                    f"A 6-digit one-time verification code has been dispatched to:\n"
                    f"👉 `{mask_email(student_email_val)}`\n\n"
                    "**Next Steps:**\n"
                    "1️⃣ Check your student email inbox *(or Spam/Junk folder)*.\n"
                    "2️⃣ Click **Enter Verification Code** below or type `/otp <code>`.\n\n"
                    f"⏱️ **Code expires:** <t:{expire_ts}:R> *(at <t:{expire_ts}:t>)*"
                ),
                color=discord.Color.blue(),
            )
            embed.set_footer(text="TARVeri Email Security • AES-256 Encrypted at Rest")
            view = OtpVerificationPromptView(self.verification_service, self.email_service)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            schedule_ttl_delete(interaction, delay=float(self.email_service.settings.email_otp_ttl_seconds))
            return

        await interaction.response.defer(ephemeral=True)
        kwargs = {}
        if raw_expiry:
            kwargs["raw_expiry_date"] = raw_expiry
        if student_email_val:
            kwargs["raw_email"] = student_email_val
        resp = await self.verification_service.perform_verification(
            interaction.user,
            student_id_val,
            **kwargs,
        )
        view = None
        db = getattr(self.verification_service, "db", None)
        if db and isinstance(getattr(interaction.user, "id", None), int):
            try:
                details = await db.get_verification_details(interaction.user.id)
                if details and details.get("is_alumni") == 0:
                    card_exp = details.get("card_expiry_date")
                    today_iso = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")
                    if card_exp and card_exp < today_iso:
                        from tarveri.cogs.verification_cog import (
                            StudentLifecycleResolutionView,
                        )

                        view = StudentLifecycleResolutionView(self.verification_service, db)
            except Exception as exc:
                logger.debug("Failed checking card expiration in guest gateway: %s", exc)

        if view:
            await interaction.followup.send(resp, view=view, ephemeral=True)
        else:
            await interaction.followup.send(resp, ephemeral=True)
        schedule_ttl_delete(interaction, delay=120.0 if view else 60.0)


class ReferralEntryModal(discord.ui.Modal, title="🎟️ Enter Student Referral Code"):
    code = discord.ui.TextInput(
        label="Referral Code",
        placeholder="e.g. TAR-8X2K9P",
        min_length=5,
        max_length=20,
        required=True,
    )

    def __init__(self, guest_service: GuestService) -> None:
        super().__init__()
        self.guest_service = guest_service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This can only be done in a server.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        raw_code = self.code.value.strip().upper()

        success, msg, thread = await self.guest_service.open_guest_review_ticket(
            guild=interaction.guild,
            applicant=interaction.user,
            referral_code=raw_code,
        )

        if not success or not thread:
            await interaction.followup.send(msg, ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        # Fetch ticket details to render initial review panel
        ticket = await self.guest_service.db.get_guest_ticket_by_channel(thread.id)
        if ticket:
            embed = build_review_embed(ticket, interaction.guild, interaction.user)
            view = GuestReviewThreadView(self.guest_service)
            admin_mention = await self.guest_service.get_target_admin_mention(
                interaction.guild, exclude_ids={interaction.user.id}
            )
            vouch_prompt = f"\n👋 {interaction.user.mention} has submitted referral code `{raw_code}`."
            if ticket.get("referrer_id"):
                vouch_prompt += f" <@{ticket['referrer_id']}>, please confirm your vouch for this guest below."

            pending_notice = (
                "\n⚠️ *Note: If you have not completed server rules screening yet, please click 'Complete' on your Discord app to enable chatting.*"
                if getattr(interaction.user, "pending", False)
                else ""
            )

            await thread.send(
                content=f"{admin_mention} {vouch_prompt}{pending_notice}",
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
            )

        await interaction.followup.send(
            f"✅ Your referral code was verified! A private review thread has been opened: {thread.mention}. "
            "Please check that thread for staff approval.",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)


class GuestApplicationModal(discord.ui.Modal, title="🌐 Guest Access Application"):
    name_affiliation = discord.ui.TextInput(
        label="Your Full Name & Affiliation",
        placeholder="e.g. Alex Tan, Sunway University / Speaker",
        max_length=100,
        required=True,
    )
    reason = discord.ui.TextInput(
        label="Reason for Joining This Server",
        placeholder="Explain why you are requesting guest access...",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True,
    )

    def __init__(self, guest_service: GuestService) -> None:
        super().__init__()
        self.guest_service = guest_service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This can only be done in a server.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        reason_text = f"**Affiliation:** {self.name_affiliation.value.strip()}\n**Reason:** {self.reason.value.strip()}"

        success, msg, thread = await self.guest_service.open_guest_review_ticket(
            guild=interaction.guild,
            applicant=interaction.user,
            reason=reason_text,
        )

        if not success or not thread:
            await interaction.followup.send(msg, ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        ticket = await self.guest_service.db.get_guest_ticket_by_channel(thread.id)
        if ticket:
            embed = build_review_embed(ticket, interaction.guild, interaction.user)
            view = GuestReviewThreadView(self.guest_service)
            admin_mention = await self.guest_service.get_target_admin_mention(
                interaction.guild, exclude_ids={interaction.user.id}
            )

            pending_notice = (
                "\n⚠️ *Note: If you have not completed server rules screening yet, please click 'Complete' on your Discord app to enable chatting.*"
                if getattr(interaction.user, "pending", False)
                else ""
            )

            await thread.send(
                content=f"{admin_mention} New guest application from {interaction.user.mention}:{pending_notice}",
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
            )

        await interaction.followup.send(
            f"✅ Your application was submitted! A private review thread has been opened: {thread.mention}. "
            "Server staff will review your request shortly.",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)


class RejectReasonModal(discord.ui.Modal, title="🛑 Rejection Reason"):
    reason = discord.ui.TextInput(
        label="Reason for Rejection",
        placeholder="e.g. Unable to verify affiliation / Invalid vouch",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=False,
    )

    def __init__(self, guest_service: GuestService, ticket: dict[str, Any], message: discord.Message) -> None:
        super().__init__()
        self.guest_service = guest_service
        self.ticket = ticket
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        await interaction.response.defer(ephemeral=True)
        success, reply_msg = await self.guest_service.reject_guest_application(
            ticket=self.ticket,
            guild=interaction.guild,
            admin_user=interaction.user,
            reason=self.reason.value,
        )

        # Update thread embed
        updated_ticket = await self.guest_service.db.get_guest_ticket_by_id(self.ticket["ticket_id"])
        applicant_member = interaction.guild.get_member(self.ticket["applicant_id"])
        if updated_ticket:
            embed = build_review_embed(updated_ticket, interaction.guild, applicant_member, status_override="REJECTED")
            disabled_view = discord.ui.View()
            try:
                await self.message.edit(embed=embed, view=disabled_view)
            except discord.HTTPException as exc:
                logger.debug("Failed to edit review message after reject: %s", exc)

        await interaction.followup.send(reply_msg, ephemeral=True)

        if isinstance(interaction.channel, discord.Thread):
            await interaction.channel.send(
                f"🛑 **Application Rejected by {interaction.user.mention}.** This thread will be locked and archived."
            )
            await asyncio.sleep(5)
            try:
                await interaction.channel.edit(locked=True, archived=True)
            except discord.HTTPException as exc:
                logger.debug("Failed to lock/archive thread after reject: %s", exc)


class CloseTicketModal(discord.ui.Modal, title="🔒 Close Ticket (Without Kicking)"):
    reason = discord.ui.TextInput(
        label="Reason / Note for Closure",
        placeholder="e.g. Duplicate request, inquiries resolved, manual review, spam dismissal",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=False,
    )

    def __init__(self, guest_service: GuestService, ticket: dict[str, Any], message: discord.Message) -> None:
        super().__init__()
        self.guest_service = guest_service
        self.ticket = ticket
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            return
        await interaction.response.defer(ephemeral=True)
        success, reply_msg = await self.guest_service.close_guest_ticket_manually(
            ticket=self.ticket,
            guild=interaction.guild,
            admin_user=interaction.user,
            reason=self.reason.value,
        )

        if success:
            updated_ticket = await self.guest_service.db.get_guest_ticket_by_id(self.ticket["ticket_id"])
            applicant_member = interaction.guild.get_member(self.ticket["applicant_id"])
            if updated_ticket:
                embed = build_review_embed(updated_ticket, interaction.guild, applicant_member, status_override="CLOSED")
                disabled_view = discord.ui.View()
                try:
                    await self.message.edit(embed=embed, view=disabled_view)
                except discord.HTTPException as exc:
                    logger.debug("Failed to edit review message after close: %s", exc)

            await interaction.followup.send(reply_msg, ephemeral=True)

            if isinstance(interaction.channel, discord.Thread):
                reason_note = (
                    f"\n> **Reason:** *\"{self.reason.value.strip()}\"*"
                    if self.reason.value and self.reason.value.strip()
                    else ""
                )
                await interaction.channel.send(
                    f"🔒 **Ticket manually closed by {interaction.user.mention}.**\n"
                    f"*(Applicant remains in the server; no role changes or kicks executed)*{reason_note}\n\n"
                    f"This thread will be locked and archived."
                )
                await asyncio.sleep(5)
                try:
                    await interaction.channel.edit(locked=True, archived=True)
                except discord.HTTPException as exc:
                    logger.debug("Failed to lock/archive thread after close: %s", exc)
        else:
            await interaction.followup.send(reply_msg, ephemeral=True)


class VouchModal(discord.ui.Modal, title="🤝 Confirm Referral Vouch"):
    vouch_note = discord.ui.TextInput(
        label="Vouch Statement / Context for Staff",
        placeholder="e.g. My classmate working on the graduation project with me.",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=True,
    )

    def __init__(self, guest_service: GuestService, ticket: dict[str, Any], message: discord.Message) -> None:
        super().__init__()
        self.guest_service = guest_service
        self.ticket = ticket
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        note = self.vouch_note.value.strip()
        await self.guest_service.db.update_guest_ticket_vouch(
            self.ticket["ticket_id"], note, vouched_by_id=interaction.user.id
        )

        updated_ticket = await self.guest_service.db.get_guest_ticket_by_id(self.ticket["ticket_id"])
        applicant_member = interaction.guild.get_member(self.ticket["applicant_id"]) if interaction.guild else None
        if updated_ticket and interaction.guild:
            embed = build_review_embed(updated_ticket, interaction.guild, applicant_member)
            view = GuestReviewThreadView(self.guest_service)
            try:
                await self.message.edit(embed=embed, view=view)
            except discord.HTTPException as exc:
                logger.debug("Failed to edit review message after vouch: %s", exc)

        await interaction.followup.send("✅ Your vouch statement has been recorded! Waiting for Admin team approval.", ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)
        if isinstance(interaction.channel, discord.Thread):
            admin_mention = (
                await self.guest_service.get_target_admin_mention(
                    interaction.guild, exclude_ids={interaction.user.id, self.ticket.get("applicant_id")}
                )
                if interaction.guild
                else "@Staff"
            )
            await interaction.channel.send(
                f"🤝 **Voucher {interaction.user.mention} confirmed vouch for <@{self.ticket['applicant_id']}>:**\n"
                f"> {note}\n\n"
                f"{admin_mention} **Step 1/2 of Double Verification complete!** Please review and click **`Approve Guest`** to admit or **`Reject / Veto`** to decline.",
                allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
            )


def build_review_embed(
    ticket: dict[str, Any],
    guild: discord.Guild,
    applicant: discord.Member | discord.User | None,
    status_override: str | None = None,
) -> discord.Embed:
    status = status_override or ticket.get("status", "OPEN")
    color = discord.Color.gold()
    if status == "APPROVED":
        color = discord.Color.green()
    elif status in ("REJECTED", "EXPIRED", "BANNED", "LEFT_SERVER"):
        color = discord.Color.red()
    elif status in ("CLOSED", "DISMISSED", "CANCELLED"):
        color = discord.Color.dark_grey()

    is_referral = bool(ticket.get("referrer_id") or ticket.get("referral_code"))
    verification_mode = "Double Verification (Voucher + Admin Required)" if is_referral else "Admin Staff Review"

    seq = ticket.get("ticket_seq") or ticket.get("ticket_id", 0)
    seq_code = format_ticket_seq(seq)
    embed = discord.Embed(
        title=f"📋 Guest Review Ticket #{seq_code}",
        description=f"Status: **{status}**\nMode: **{verification_mode}**",
        color=color,
    )
    applicant_mention = f"<@{ticket['applicant_id']}>" if not applicant else applicant.mention
    embed.add_field(name="Applicant", value=applicant_mention, inline=True)

    if ticket.get("referrer_id"):
        embed.add_field(name="Referred By", value=f"<@{ticket['referrer_id']}>", inline=True)
    if ticket.get("referral_code"):
        embed.add_field(name="Referral Code", value=f"`{ticket['referral_code']}`", inline=True)

    if is_referral:
        if ticket.get("vouch_note"):
            voucher_id = ticket.get("vouched_by_id") or ticket.get("referrer_id")
            voucher_str = f"<@{voucher_id}>" if voucher_id else "Voucher"
            vouch_status = f"✅ Confirmed by {voucher_str}: *\"{ticket['vouch_note']}\"*"
            if ticket.get("vouched_at"):
                vouch_status += f" `({ticket['vouched_at']})`"
        else:
            vouch_status = f"⏳ Pending voucher confirmation from <@{ticket.get('referrer_id')}>"
        embed.add_field(name="1️⃣ Voucher Status", value=vouch_status, inline=False)

        if status == "APPROVED":
            admin_id = ticket.get("closed_by_admin_id")
            admin_str = f" by <@{admin_id}>" if admin_id else " by Admin"
            reason_str = f": *\"{ticket['close_reason']}\"*" if ticket.get("close_reason") else ""
            admin_status = f"✅ Approved{admin_str}{reason_str}"
        elif status in ("CLOSED", "DISMISSED", "CANCELLED"):
            admin_id = ticket.get("closed_by_admin_id")
            admin_str = f" by <@{admin_id}>" if admin_id else ""
            reason_str = f": *\"{ticket.get('close_reason')}\"*" if ticket.get("close_reason") else ""
            admin_status = f"🔒 Closed / Dismissed{admin_str}{reason_str} *(No kicking or role assigned)*"
        elif status in ("REJECTED", "BANNED", "LEFT_SERVER"):
            admin_id = ticket.get("closed_by_admin_id")
            admin_str = f" by <@{admin_id}>" if admin_id else ""
            reason_str = f": *\"{ticket.get('close_reason')}\"*" if ticket.get("close_reason") else ""
            admin_status = f"🛑 {status.capitalize()}{admin_str}{reason_str}"
        else:
            admin_status = "⏳ Pending Admin final approval"
        embed.add_field(name="2️⃣ Admin Decision", value=admin_status, inline=False)
    else:
        if ticket.get("reason"):
            embed.add_field(name="Application Details", value=ticket["reason"], inline=False)
        if status == "APPROVED":
            admin_id = ticket.get("closed_by_admin_id")
            admin_str = f" by <@{admin_id}>" if admin_id else ""
            reason_str = f": *\"{ticket['close_reason']}\"*" if ticket.get("close_reason") else ""
            embed.add_field(name="Staff Verdict", value=f"✅ Approved{admin_str}{reason_str}", inline=False)
        elif status in ("CLOSED", "DISMISSED", "CANCELLED"):
            admin_id = ticket.get("closed_by_admin_id")
            admin_str = f" by <@{admin_id}>" if admin_id else ""
            reason_str = f": *\"{ticket.get('close_reason')}\"*" if ticket.get("close_reason") else ""
            embed.add_field(name="Staff Verdict", value=f"🔒 Closed / Dismissed{admin_str}{reason_str} *(No kicking or role assigned)*", inline=False)
        elif status != "OPEN":
            admin_id = ticket.get("closed_by_admin_id")
            admin_str = f" by <@{admin_id}>" if admin_id else ""
            reason_str = f": *\"{ticket.get('close_reason')}\"*" if ticket.get("close_reason") else ""
            embed.add_field(name="Staff Verdict", value=f"🛑 {status.capitalize()}{admin_str}{reason_str}", inline=False)

    embed.set_footer(text=f"Server: {guild.name} • Created at {ticket.get('created_at', 'N/A')}")
    return embed


def build_gateway_panel_embed(
    guild_name: str = "the Server",
    require_email: bool = False,
) -> discord.Embed:
    """
    Builds the standardized verification gateway panel embed.
    If require_email is True, highlights that institutional email OTP (@student.tarc.edu.my)
    is required specifically for TARUMT Student verification (Guests & referrals do not need email).
    """
    if require_email:
        embed = discord.Embed(
            title="🎓 TARUMT Verification Gateway",
            description=(
                f"Welcome to **{guild_name}**!\n"
                "Choose an option below to gain access:\n\n"
                "🎓 **TARUMT Student** — Enter Student ID & verify `@student.tarc.edu.my` OTP\n"
                "🎟️ **Referral Code** — Enter an invite code from an existing student\n"
                "🌐 **Guest / Speaker** — Apply for visitor access"
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="🔒 Student email OTP required for TARUMT access • Guests exempt")
    else:
        embed = discord.Embed(
            title="🎓 TARUMT Verification Gateway",
            description=(
                f"Welcome to **{guild_name}**!\n"
                "Choose an option below to gain access:\n\n"
                "🎓 **TARUMT Student** — Enter Student ID to receive faculty roles\n"
                "🎟️ **Referral Code** — Enter an invite code from an existing student\n"
                "🌐 **Guest / Speaker** — Apply for visitor access"
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="TARVeri Verification System • Fast & Secure")
    return embed


class VerificationGatewayView(discord.ui.View):
    """Persistent 3-button verification gateway view for server welcome channels."""

    def __init__(self, verification_service: VerificationService, guest_service: GuestService) -> None:
        super().__init__(timeout=None)
        self.verification_service = verification_service
        self.guest_service = guest_service

    @discord.ui.button(
        label="Verify TARUMT Student",
        style=discord.ButtonStyle.primary,
        emoji="🎓",
        custom_id="tarveri:gateway:student",
    )
    async def verify_student(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        email_required = False
        db = getattr(self.verification_service, "db", None)
        if interaction.guild and db:
            email_required = await db.is_guild_email_verification_enabled(interaction.guild.id)
        modal = StudentVerificationModal(self.verification_service, require_email=email_required)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Enter Referral Code",
        style=discord.ButtonStyle.success,
        emoji="🎟️",
        custom_id="tarveri:gateway:referral",
    )
    async def enter_referral(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = ReferralEntryModal(self.guest_service)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Apply as Guest",
        style=discord.ButtonStyle.secondary,
        emoji="🌐",
        custom_id="tarveri:gateway:guest",
    )
    async def apply_guest(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = GuestApplicationModal(self.guest_service)
        await interaction.response.send_modal(modal)


class GuestReviewThreadView(discord.ui.View):
    """Interactive review buttons posted inside the private review thread."""

    def __init__(self, guest_service: GuestService) -> None:
        super().__init__(timeout=None)
        self.guest_service = guest_service

    @discord.ui.button(
        label="Approve Guest",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="tarveri:review:approve",
    )
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_admin_or_has_role(interaction, self.guest_service.admin_role_name):
            await interaction.response.send_message("❌ Only server administrators can approve guest requests.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild or not interaction.channel:
            return

        ticket = await self.guest_service.db.get_guest_ticket_by_channel(interaction.channel.id)
        if not ticket or ticket["status"] != "OPEN":
            await interaction.response.send_message("⚠️ This ticket is already resolved or not found.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        # Double verification requirement: for referral tickets, voucher must have confirmed vouch first
        if ticket.get("referrer_id") and not ticket.get("vouch_note"):
            await interaction.response.send_message(
                f"⚠️ **Double Verification Required:** The referring student (<@{ticket['referrer_id']}>) has not confirmed their vouch yet.\n"
                f"Both the voucher and Admin team must agree before the guest can be admitted.\n"
                f"*(You can click **`Reject / Veto`** or **`Close Ticket`** at any time to reject or dismiss this request).* ",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        success, msg = await self.guest_service.approve_guest_application(ticket, interaction.guild, interaction.user)

        if success:
            updated_ticket = await self.guest_service.db.get_guest_ticket_by_id(ticket["ticket_id"])
            applicant_member = interaction.guild.get_member(ticket["applicant_id"])
            if updated_ticket:
                embed = build_review_embed(updated_ticket, interaction.guild, applicant_member, status_override="APPROVED")
                disabled_view = discord.ui.View()
                try:
                    await interaction.message.edit(embed=embed, view=disabled_view)
                except discord.HTTPException as exc:
                    logger.debug("Failed to edit review message on approve: %s", exc)

            await interaction.followup.send(msg, ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            if isinstance(interaction.channel, discord.Thread):
                await interaction.channel.send(
                    f"🎉 **Application Approved by {interaction.user.mention}!** Double verification completed. This thread will be locked and archived."
                )
                await asyncio.sleep(5)
                try:
                    await interaction.channel.edit(locked=True, archived=True)
                except discord.HTTPException as exc:
                    logger.debug("Failed to lock/archive thread on approve: %s", exc)
        else:
            await interaction.followup.send(msg, ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)

    @discord.ui.button(
        label="Reject / Veto",
        style=discord.ButtonStyle.danger,
        emoji="🛑",
        custom_id="tarveri:review:reject",
    )
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_admin_or_has_role(interaction, self.guest_service.admin_role_name):
            await interaction.response.send_message("❌ Only server administrators can veto/reject guest requests.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild or not interaction.channel:
            return

        ticket = await self.guest_service.db.get_guest_ticket_by_channel(interaction.channel.id)
        if not ticket or ticket["status"] != "OPEN":
            await interaction.response.send_message("⚠️ This ticket is already resolved or not found.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        modal = RejectReasonModal(self.guest_service, ticket, interaction.message)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="tarveri:review:close",
    )
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_admin_or_has_role(interaction, self.guest_service.admin_role_name):
            await interaction.response.send_message(
                "❌ Only server administrators can close review tickets.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild or not interaction.channel:
            return

        ticket = await self.guest_service.db.get_guest_ticket_by_channel(interaction.channel.id)
        if not ticket or ticket["status"] != "OPEN":
            await interaction.response.send_message("⚠️ This ticket is already resolved or not found.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        modal = CloseTicketModal(self.guest_service, ticket, interaction.message)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Confirm Vouch",
        style=discord.ButtonStyle.primary,
        emoji="🤝",
        custom_id="tarveri:review:vouch",
    )
    async def vouch_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.channel:
            return

        ticket = await self.guest_service.db.get_guest_ticket_by_channel(interaction.channel.id)
        if not ticket or ticket["status"] != "OPEN":
            await interaction.response.send_message("⚠️ This ticket is already resolved or not found.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
            return

        # Only the referrer or an admin can provide vouch input
        is_referrer = ticket.get("referrer_id") == interaction.user.id
        is_admin = is_admin_or_has_role(interaction, self.guest_service.admin_role_name)
        if not (is_referrer or is_admin):
            await interaction.response.send_message(
                f"❌ Only the referring student (<@{ticket.get('referrer_id')}>) can submit the vouch confirmation.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        modal = VouchModal(self.guest_service, ticket, interaction.message)
        await interaction.response.send_modal(modal)


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

    @referral.command(name="generate", description="Generate a guest referral code for a friend (Verified Students only).")
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
            status_emoji = "🟢" if item["status"] == "ACTIVE" else ("🟡" if item["status"] == "PENDING_APPROVAL" else "⚪")
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
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
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
            await db.is_guild_email_verification_enabled(interaction.guild.id)
            if interaction.guild and db
            else False
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
