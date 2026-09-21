"""
Discord UI and Commands for Student Verification.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from tarveri.cogs.guest_cog import VerificationGatewayView
from tarveri.config import (
    CAMPUS_ROLES,
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    GUEST_ROLE_PATTERN,
    ROLE_HELP_KEYWORDS_PATTERN,
    STUDY_LEVEL_ROLES,
    Settings,
    estimate_student_card_expiry,
    format_card_expiry_display,
    get_configured_tz,
    is_expiry_date_anomalous,
    mask_email,
    parse_card_expiry_date,
    parse_student_id,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.email_service import EmailService
from tarveri.services.guest_service import GuestService
from tarveri.services.verification_service import VerificationService
from tarveri.utils import parse_db_timestamp, schedule_ttl_delete

logger = logging.getLogger("tarveri")


class AlumniClaimModal(discord.ui.Modal, title="TARUMT Alumni Transition"):
    grad_year = discord.ui.TextInput(
        label="Graduation Year",
        placeholder="e.g. 2025",
        min_length=4,
        max_length=4,
        required=True,
    )
    programme = discord.ui.TextInput(
        label="Completed Programme (Optional)",
        placeholder="e.g. Bachelor of Software Engineering (Honours)",
        min_length=2,
        max_length=80,
        required=False,
    )

    def __init__(self, service: VerificationService):
        super().__init__()
        self.service = service
        self.grad_year.placeholder = f"e.g. {datetime.now().year}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False, thinking=True)
        raw_year = self.grad_year.value.strip()
        try:
            year_int = int(raw_year)
        except ValueError:
            await interaction.followup.send(
                f"❌ Please enter a valid 4-digit graduation year (e.g. {datetime.now().year}).",
                ephemeral=True,
            )
            return

        result = await self.service.claim_alumni_status(
            user_id=interaction.user.id,
            user_display_name=interaction.user.display_name,
            graduated_year=year_int,
            programme=self.programme.value if self.programme.value else None,
            current_guild=interaction.guild,
        )

        if not result["success"]:
            await interaction.followup.send(result["message"], ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🎓 Congratulations on Graduating, {interaction.user.display_name}!",
            description=(
                f"🎉 **{interaction.user.mention}** has successfully registered their **TARUMT Alumni** status!\n\n"
                f"• **Class Cohort**: Class of {result['graduated_year']}\n"
                f"• **Faculty**: {result['faculty_name']}\n"
                + (f"• **Programme**: {result['programme']}\n" if result['programme'] else "")
                + f"\n🏷️ **`TARUMT Alumni`** role assigned in {result['guilds_updated']} server(s).\n"
                f"🪪 **`❖ ALUMNI`** badge unlocked on your Digital Campus Card (`/card`)."
            ),
            color=discord.Color.from_rgb(212, 175, 55),
        )
        embed.set_footer(text="TARVeri Alumni Verification • Instant & Tamper-Proof")
        await interaction.followup.send(embed=embed, ephemeral=False)


def build_alumni_email_confirm_embed(
    student_id: str,
    email: str,
    iso_expiry: str,
) -> discord.Embed:
    """Builds a helpful embed when a graduated/alumni student ID is submitted for email verification."""
    expiry_display = format_card_expiry_display(iso_expiry) or iso_expiry
    masked = mask_email(email)
    embed = discord.Embed(
        title="🎓 Graduated Student Cohort Detected",
        description=(
            f"Your Student ID **`{student_id}`** indicates that your cohort completed studies around **`{expiry_display}`**.\n\n"
            f"⚠️ **Note on Email Verification**:\n"
            f"TARUMT institutional Google Workspace accounts (`@student.tarc.edu.my`) are typically deactivated after graduation.\n\n"
            f"Please choose how you would like to proceed:"
        ),
        color=discord.Color.from_rgb(212, 175, 55),
    )
    embed.add_field(
        name="🎓 Verify as Graduated Alumni (Recommended)",
        value="If your student inbox is closed, skip the email OTP. Your student ID will be verified and you will be granted the **`TARUMT Alumni`** role.",
        inline=False,
    )
    embed.add_field(
        name=f"📬 Send OTP to `{masked}` Anyway",
        value="If you still have active access to your student email inbox and wish to complete OTP verification.",
        inline=False,
    )
    embed.add_field(
        name="📚 Continuing Studies (New ID)",
        value="If you have progressed to a new programme at TARUMT and have a new Student ID.",
        inline=False,
    )
    embed.set_footer(text="TARVeri Alumni & Email Gateway • Safe Verification")
    return embed


class AlumniEmailConfirmationView(discord.ui.View):
    """
    Interactive view presented when a graduated student submits a verification request
    with email OTP, allowing them to verify as alumni without email OTP or proceed with OTP.
    """

    def __init__(
        self,
        service: VerificationService,
        email_service: EmailService,
        student_id: str,
        email: str,
        raw_expiry: str | None,
        iso_expiry: str,
        timeout: float = 180.0,
    ):
        super().__init__(timeout=timeout)
        self.service = service
        self.email_service = email_service
        self.student_id = student_id
        self.email = email
        self.raw_expiry = raw_expiry
        self.iso_expiry = iso_expiry

    @discord.ui.button(
        label="Verify as Graduated Alumni",
        style=discord.ButtonStyle.success,
        emoji="🎓",
        custom_id="tarveri_alumni_confirm_verify",
        row=0,
    )
    async def on_alumni_verify(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Directly verifies user with their student ID + encrypted email, then presents AlumniClaimModal."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        response_text = await self.service.perform_verification(
            interaction.user,
            self.student_id,
            raw_expiry_date=self.iso_expiry,
            raw_email=self.email,
        )
        lifecycle_view = StudentLifecycleResolutionView(self.service, self.service.db)
        await interaction.followup.send(
            f"✅ **Student ID Verified!**\n\n{response_text}\n\n"
            f"🎉 Click **I have Graduated** below to register your graduation cohort and claim your **`TARUMT Alumni`** role across mutual servers!",
            view=lifecycle_view,
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=180.0)

    @discord.ui.button(
        label="Send OTP Anyway",
        style=discord.ButtonStyle.primary,
        emoji="📬",
        custom_id="tarveri_alumni_send_otp_anyway",
        row=0,
    )
    async def on_send_otp(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Proceeds to send OTP email to student email."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        server_name = interaction.guild.name if interaction.guild else "TARUMT Community"
        send_result = await self.email_service.generate_and_send_otp(
            user_id=interaction.user.id,
            student_id=self.student_id,
            email_address=self.email,
            server_name=server_name,
            card_expiry_date=self.raw_expiry,
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
                f"👉 `{mask_email(self.email)}`\n\n"
                "**Next Steps:**\n"
                "1️⃣ Check your student email inbox *(or Spam/Junk folder)*.\n"
                "2️⃣ Click **Enter Verification Code** below or type `/otp <code>`.\n\n"
                f"⏱️ **Code expires:** <t:{expire_ts}:R> *(at <t:{expire_ts}:t>)*"
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="TARVeri Email Security • AES-256 Encrypted at Rest")
        view = OtpVerificationPromptView(self.service, self.email_service)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        schedule_ttl_delete(interaction, delay=float(self.email_service.settings.email_otp_ttl_seconds))

    @discord.ui.button(
        label="Continuing Studies (New ID)",
        style=discord.ButtonStyle.secondary,
        emoji="📚",
        custom_id="tarveri_alumni_further_study",
        row=0,
    )
    async def on_further_study(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Opens modal to input new Student ID."""
        modal = FurtherStudyTransitionModal(self.service)
        await interaction.response.send_modal(modal)


def build_expiry_anomaly_embed(
    raw_input: str,
    parsed_iso: str,
    student_id: str,
    anomaly_reason: str,
) -> discord.Embed:
    """Builds a helpful confirmation embed when an entered expiry date exceeds the anomaly threshold."""
    display_str = format_card_expiry_display(parsed_iso)
    current_year = datetime.now().year
    current_yy = str(current_year)[-2:]
    embed = discord.Embed(
        title="⚠️ Please Confirm Student Card Expiry Date",
        description=(
            f"You entered: **`{raw_input}`**\n"
            f"Interpreted as: **`{display_str}`** (`{parsed_iso}`)\n\n"
            f"🔍 **Notice**: {anomaly_reason}\n\n"
            f"💡 **Common Typo**: Did you enter **Day/Month** (e.g. `06/07` for 6th July) "
            f"instead of **Month/Year** (e.g. `07/{current_yy}` or `06/07/{current_year}`)?\n\n"
            f"Please choose an action below to proceed:"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="✅ Confirm This Date",
        value="If this date is correct (e.g. you graduated in this year).",
        inline=False,
    )
    embed.add_field(
        name="✏️ Re-enter Expiry Date",
        value="Open a new form to enter your corrected card expiry date.",
        inline=False,
    )
    embed.add_field(
        name="⚡ Auto-Calculate for Me",
        value="Let TARVeri automatically calculate your standard study duration from your Student ID.",
        inline=False,
    )
    embed.set_footer(text="TARVeri Dynamic Lifecycle Guard • Safe Date Validation")
    return embed


class ExpiryAnomalyConfirmView(discord.ui.View):
    """
    Interactive view presented when a card expiry date exceeds the 8-year threshold
    or appears to be an ambiguous Day/Month entry (e.g. 06/07).
    """

    def __init__(
        self,
        service: VerificationService,
        db: Database,
        student_id: str,
        raw_expiry_input: str,
        parsed_iso_date: str,
        anomaly_reason: str,
        timeout: float = 180.0,
    ):
        super().__init__(timeout=timeout)
        self.service = service
        self.db = db
        self.student_id = student_id
        self.raw_expiry_input = raw_expiry_input
        self.parsed_iso_date = parsed_iso_date
        self.anomaly_reason = anomaly_reason

    @discord.ui.button(
        label="Confirm This Date",
        style=discord.ButtonStyle.secondary,
        emoji="✅",
        custom_id="tarveri_expiry_anomaly_confirm",
        row=0,
    )
    async def on_confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        response_text = await self.service.perform_verification(
            interaction.user,
            self.student_id,
            raw_expiry_date=self.parsed_iso_date,
        )
        lifecycle_view = None
        if self.db and isinstance(interaction.user.id, int):
            try:
                details = await self.db.get_verification_details(interaction.user.id)
                if details and details.get("is_alumni") == 0:
                    card_exp = details.get("card_expiry_date")
                    today_iso = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")
                    if card_exp and card_exp < today_iso:
                        lifecycle_view = StudentLifecycleResolutionView(self.service, self.db)
            except Exception as e:
                logger.debug("Could not determine student lifecycle state: %s", e)

        header = f"✅ **Expiry Date Confirmed**: Recorded as `{format_card_expiry_display(self.parsed_iso_date)}` (`{self.parsed_iso_date}`).\n\n"
        final_msg = header + response_text
        if lifecycle_view:
            await interaction.followup.send(final_msg, view=lifecycle_view, ephemeral=True)
        else:
            await interaction.followup.send(final_msg, ephemeral=True)
        schedule_ttl_delete(interaction, delay=120.0 if lifecycle_view else 60.0)

    @discord.ui.button(
        label="Re-enter Expiry Date",
        style=discord.ButtonStyle.primary,
        emoji="✏️",
        custom_id="tarveri_expiry_anomaly_reenter",
        row=0,
    )
    async def on_reenter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = ReEnterExpiryModal(
            service=self.service,
            db=self.db,
            student_id=self.student_id,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Auto-Calculate for Me",
        style=discord.ButtonStyle.success,
        emoji="⚡",
        custom_id="tarveri_expiry_anomaly_auto",
        row=0,
    )
    async def on_auto_calculate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        response_text = await self.service.perform_verification(
            interaction.user,
            self.student_id,
            raw_expiry_date=None,
        )
        lifecycle_view = None
        if self.db and isinstance(interaction.user.id, int):
            try:
                details = await self.db.get_verification_details(interaction.user.id)
                if details and details.get("is_alumni") == 0:
                    card_exp = details.get("card_expiry_date")
                    today_iso = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")
                    if card_exp and card_exp < today_iso:
                        lifecycle_view = StudentLifecycleResolutionView(self.service, self.db)
            except Exception as e:
                logger.debug("Could not determine student lifecycle state: %s", e)

        if lifecycle_view:
            await interaction.followup.send(response_text, view=lifecycle_view, ephemeral=True)
        else:
            await interaction.followup.send(response_text, ephemeral=True)
        schedule_ttl_delete(interaction, delay=120.0 if lifecycle_view else 60.0)


class ReEnterExpiryModal(discord.ui.Modal, title="✏️ Re-enter Card Expiry Date"):
    card_expiry = discord.ui.TextInput(
        label="New Expiry Date (MM/YY or DD/MM/YYYY)",
        placeholder="e.g. 10/26 or 06/07/2026",
        min_length=3,
        max_length=15,
        required=True,
    )

    def __init__(
        self,
        service: VerificationService,
        db: Database,
        student_id: str,
    ):
        super().__init__()
        self.service = service
        self.db = db
        self.student_id = student_id
        current_yy = str(datetime.now().year)[-2:]
        self.card_expiry.placeholder = f"e.g. 10/{(int(current_yy) + 2) % 100:02d} or 31/10/{datetime.now().year + 2}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_val = self.card_expiry.value.strip()
        new_iso = parse_card_expiry_date(raw_val)
        if not new_iso:
            await interaction.response.defer(ephemeral=True, thinking=True)
            current_yy = str(datetime.now().year)[-2:]
            await interaction.followup.send(
                f"❌ Invalid date format. Please use `MM/YY` (e.g. `10/{(int(current_yy) + 2) % 100:02d}`) "
                f"or `DD/MM/YYYY` (e.g. `31/10/{datetime.now().year + 2}`).",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=30.0)
            return

        is_anomalous, anomaly_reason = is_expiry_date_anomalous(new_iso, student_id=self.student_id)
        if is_anomalous:
            await interaction.response.defer(ephemeral=True, thinking=True)
            embed = build_expiry_anomaly_embed(
                raw_input=raw_val,
                parsed_iso=new_iso,
                student_id=self.student_id,
                anomaly_reason=anomaly_reason or "",
            )
            view = ExpiryAnomalyConfirmView(
                service=self.service,
                db=self.db,
                student_id=self.student_id,
                raw_expiry_input=raw_val,
                parsed_iso_date=new_iso,
                anomaly_reason=anomaly_reason or "",
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            schedule_ttl_delete(interaction, delay=180.0)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        response_text = await self.service.perform_verification(
            interaction.user,
            self.student_id,
            raw_expiry_date=new_iso,
        )
        lifecycle_view = None
        if self.db and isinstance(interaction.user.id, int):
            try:
                details = await self.db.get_verification_details(interaction.user.id)
                if details and details.get("is_alumni") == 0:
                    card_exp = details.get("card_expiry_date")
                    today_iso = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")
                    if card_exp and card_exp < today_iso:
                        lifecycle_view = StudentLifecycleResolutionView(self.service, self.db)
            except Exception as e:
                logger.debug("Could not determine student lifecycle state: %s", e)

        if lifecycle_view:
            await interaction.followup.send(response_text, view=lifecycle_view, ephemeral=True)
        else:
            await interaction.followup.send(response_text, ephemeral=True)
        schedule_ttl_delete(interaction, delay=120.0 if lifecycle_view else 60.0)


class ExtendExpiryAnomalyConfirmView(discord.ui.View):
    """Interactive view presented when extending card expiry with an anomalous date."""

    def __init__(
        self,
        db: Database,
        service: VerificationService,
        raw_expiry_input: str,
        parsed_iso_date: str,
        note: str,
        anomaly_reason: str,
        timeout: float = 180.0,
    ):
        super().__init__(timeout=timeout)
        self.db = db
        self.service = service
        self.raw_expiry_input = raw_expiry_input
        self.parsed_iso_date = parsed_iso_date
        self.note = note
        self.anomaly_reason = anomaly_reason

    @discord.ui.button(
        label="Confirm This Date",
        style=discord.ButtonStyle.secondary,
        emoji="✅",
        custom_id="tarveri_extend_expiry_confirm",
        row=0,
    )
    async def on_confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.db.update_verification_profile(
            discord_user_id=interaction.user.id,
            card_expiry_date=self.parsed_iso_date,
            lifecycle_prompt_status="extended",
        )
        guild = interaction.guild
        note_str = f" ({self.note})" if self.note else ""
        await self.db.log(
            "INFO",
            "EXPIRY_EXTENDED",
            f"{interaction.user} (ID: {interaction.user.id}) extended card expiry to {self.parsed_iso_date}{note_str}",
            user_id=interaction.user.id,
            guild=guild,
        )
        display_str = format_card_expiry_display(self.parsed_iso_date)
        await interaction.followup.send(
            f"✅ **Student Card Validity Extended!**\n"
            f"Your new card expiry is set to **{display_str}**.\n"
            f"Your Digital Campus Card (`/card`) has been updated.",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)

    @discord.ui.button(
        label="Re-enter Expiry Date",
        style=discord.ButtonStyle.primary,
        emoji="✏️",
        custom_id="tarveri_extend_expiry_reenter",
        row=0,
    )
    async def on_reenter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ExtendExpiryModal(self.db, self.service))


class FurtherStudyTransitionModal(discord.ui.Modal, title="TARUMT Level Progression"):
    student_id = discord.ui.TextInput(
        label="New Student ID",
        placeholder="e.g. 24WMR12345",
        min_length=7,
        max_length=20,
        required=True,
    )
    card_expiry = discord.ui.TextInput(
        label="New Student Card Expiry (MM/YY)",
        placeholder="e.g. 10/28 (Optional)",
        min_length=4,
        max_length=12,
        required=False,
    )

    def __init__(self, service: VerificationService):
        super().__init__()
        self.service = service
        current_yy = str(datetime.now().year)[-2:]
        self.student_id.placeholder = f"e.g. {current_yy}WMR12345"
        self.card_expiry.placeholder = f"e.g. 10/{(int(current_yy) + 3) % 100:02d} (Optional)"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_expiry = self.card_expiry.value.strip() if self.card_expiry.value else None
        student_id_val = self.student_id.value.strip()
        if raw_expiry:
            iso_expiry = parse_card_expiry_date(raw_expiry)
            if not iso_expiry:
                await interaction.response.defer(ephemeral=True, thinking=True)
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
                await interaction.response.defer(ephemeral=True, thinking=True)
                embed = build_expiry_anomaly_embed(
                    raw_input=raw_expiry,
                    parsed_iso=iso_expiry,
                    student_id=student_id_val,
                    anomaly_reason=anomaly_reason or "",
                )
                view = ExpiryAnomalyConfirmView(
                    service=self.service,
                    db=self.service.db,
                    student_id=student_id_val,
                    raw_expiry_input=raw_expiry,
                    parsed_iso_date=iso_expiry,
                    anomaly_reason=anomaly_reason or "",
                )
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                schedule_ttl_delete(interaction, delay=180.0)
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        response_text = await self.service.perform_verification(
            interaction.user,
            student_id_val,
            raw_expiry_date=raw_expiry,
        )
        await interaction.followup.send(response_text, ephemeral=True)
        schedule_ttl_delete(interaction, delay=90.0)


class ExtendExpiryModal(discord.ui.Modal, title="Extend Student Card Validity"):
    expiry_date = discord.ui.TextInput(
        label="New Student Card Expiry Date (MM/YY)",
        placeholder="e.g. MM/YY",
        min_length=4,
        max_length=12,
        required=True,
    )
    note = discord.ui.TextInput(
        label="Extension Reason (Optional)",
        placeholder="e.g. Final year project extension / Delayed semester",
        max_length=100,
        required=False,
    )

    def __init__(self, db: Database, service: VerificationService):
        super().__init__()
        self.db = db
        self.service = service
        current_yy = str(datetime.now().year)[-2:]
        self.expiry_date.placeholder = f"e.g. 10/{(int(current_yy) + 1) % 100:02d}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_val = self.expiry_date.value.strip()
        iso_date = parse_card_expiry_date(raw_val)
        if not iso_date:
            await interaction.response.defer(ephemeral=True, thinking=True)
            current_yy = str(datetime.now().year)[-2:]
            await interaction.followup.send(
                f"❌ Invalid date format. Please use `MM/YY` (e.g. `10/{(int(current_yy) + 1) % 100:02d}`) or `YYYY-MM-DD`.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=30.0)
            return

        is_anomalous, anomaly_reason = is_expiry_date_anomalous(iso_date)
        if is_anomalous:
            await interaction.response.defer(ephemeral=True, thinking=True)
            embed = build_expiry_anomaly_embed(
                raw_input=raw_val,
                parsed_iso=iso_date,
                student_id="",
                anomaly_reason=anomaly_reason or "",
            )
            view = ExtendExpiryAnomalyConfirmView(
                db=self.db,
                service=self.service,
                raw_expiry_input=raw_val,
                parsed_iso_date=iso_date,
                note=self.note.value.strip() if self.note.value else "",
                anomaly_reason=anomaly_reason or "",
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            schedule_ttl_delete(interaction, delay=180.0)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.db.update_verification_profile(
            discord_user_id=interaction.user.id,
            card_expiry_date=iso_date,
            lifecycle_prompt_status="extended",
        )
        guild = interaction.guild
        note_str = f" ({self.note.value.strip()})" if self.note.value and self.note.value.strip() else ""
        await self.db.log(
            "INFO",
            "EXPIRY_EXTENDED",
            f"{interaction.user} (ID: {interaction.user.id}) extended card expiry to {iso_date}{note_str}",
            user_id=interaction.user.id,
            guild=guild,
        )
        display_str = format_card_expiry_display(iso_date)
        await interaction.followup.send(
            f"✅ **Student Card Validity Extended!**\n"
            f"Your new card expiry is set to **{display_str}**.\n"
            f"Your Digital Campus Card (`/card`) has been updated.",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)


class StudentDropoutConfirmModal(discord.ui.Modal, title="⚠️ Confirm Studies Discontinuation"):
    confirmation = discord.ui.TextInput(
        label="Type 'Yes, I am dropping out.' to confirm",
        placeholder="Yes, I am dropping out.",
        min_length=15,
        max_length=40,
        required=True,
    )
    reason = discord.ui.TextInput(
        label="Reason for Discontinuation (Optional)",
        placeholder="e.g. Transferred universities, career shift, taking a gap year",
        min_length=0,
        max_length=200,
        required=False,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, service: VerificationService, db: Database):
        super().__init__()
        self.service = service
        self.db = db

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        confirm_text = self.confirmation.value.strip()
        expected = "Yes, I am dropping out."

        if confirm_text.rstrip(".").lower() != expected.rstrip(".").lower():
            await interaction.followup.send(
                f"❌ **Confirmation phrase did not match.**\n\n"
                f"To confirm discontinuation of studies and remove your student verification, you must type exactly:\n"
                f"`{expected}`\n\n"
                f"*(Your verification status and server roles remain unchanged.)*",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=30.0)
            return

        reason_text = self.reason.value.strip() if self.reason.value else "Self-reported dropout"
        result = await self.service.process_student_dropout(interaction.user, reason=reason_text)
        if not result["success"]:
            await interaction.followup.send(
                f"❌ {result.get('error_message', 'Could not process dropout request.')}",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=30.0)
            return

        await interaction.followup.send(
            "🚪 **Student verification updated to Guest.**\n\n"
            "Your student roles (Faculty, Campus, Study Level) have been withdrawn, and you have been granted the **`Guest(Approved)`** role across mutual servers so you retain guest access to community channels.\n\n"
            "If you ever resume your studies at TARUMT in the future, you are welcome to run `/verify` again anytime.\n\n"
            "We wish you the very best in your journey ahead! 🌟",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)


class StudentLifecycleResolutionView(discord.ui.View):
    """Interactive persistent view presented to students whose card expiry is reached."""

    def __init__(
        self,
        service: VerificationService,
        db: Database,
        timeout: float | None = None,
    ):
        super().__init__(timeout=timeout)
        self.service = service
        self.db = db

    @discord.ui.button(
        label="I have Graduated",
        style=discord.ButtonStyle.success,
        emoji="🎓",
        custom_id="tarveri_lifecycle_graduated",
        row=0,
    )
    async def on_graduated(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AlumniClaimModal(self.service))

    @discord.ui.button(
        label="Further Studies at TARUMT",
        style=discord.ButtonStyle.primary,
        emoji="📚",
        custom_id="tarveri_lifecycle_further_study",
        row=0,
    )
    async def on_further_study(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(FurtherStudyTransitionModal(self.service))

    @discord.ui.button(
        label="Still Studying / Extension",
        style=discord.ButtonStyle.secondary,
        emoji="⏳",
        custom_id="tarveri_lifecycle_extend",
        row=0,
    )
    async def on_extend(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ExtendExpiryModal(self.db, self.service))

    @discord.ui.button(
        label="Discontinue Studies / Dropout",
        style=discord.ButtonStyle.danger,
        emoji="🚪",
        custom_id="tarveri_lifecycle_dropout",
        row=1,
    )
    async def on_dropout(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(StudentDropoutConfirmModal(self.service, self.db))


class VerificationModal(discord.ui.Modal, title="🎓 TARUMT Student Verification"):
    student_id = discord.ui.TextInput(
        label="Student ID",
        placeholder="e.g. 24WMD09867",
        min_length=7,
        max_length=20,
        required=True,
    )
    student_email = discord.ui.TextInput(
        label="Student Email (@student.tarc.edu.my)",
        placeholder="e.g. 24wmd09867@student.tarc.edu.my (Optional)",
        min_length=5,
        max_length=100,
        required=False,
    )
    card_expiry = discord.ui.TextInput(
        label="Student Card Expiry Date (MM/YY)",
        placeholder="e.g. MM/YY (Optional)",
        min_length=4,
        max_length=12,
        required=False,
    )

    def __init__(
        self,
        service: VerificationService,
        email_service: EmailService | None = None,
        require_email: bool = False,
    ):
        super().__init__()
        self.service = service
        self.email_service = email_service or getattr(service, "email_service", None)
        self.require_email = require_email
        current_yy = str(datetime.now().year)[-2:]
        self.student_id.placeholder = f"e.g. {current_yy}WMD09867"
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
                await interaction.response.defer(ephemeral=True, thinking=True)
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
                await interaction.response.defer(ephemeral=True, thinking=True)
                embed = build_expiry_anomaly_embed(
                    raw_input=raw_expiry,
                    parsed_iso=iso_expiry,
                    student_id=student_id_val,
                    anomaly_reason=anomaly_reason or "",
                )
                view = ExpiryAnomalyConfirmView(
                    service=self.service,
                    db=self.service.db,
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
        db = getattr(self.service, "db", None)
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
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(
                "❌ Institutional student email is required for verification in this server. Please enter your official TARUMT email (e.g. `@student.tarc.edu.my`).",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=30.0)
            return

        # 2. Trigger OTP dispatch if:
        #    - guild explicitly opted-in (guild_email_required), OR
        #    - SMTP usage is NOT restricted (restrict_smtp is False) and student provided an email
        should_send_otp = is_email_active and student_email_val and (guild_email_required or not restrict_smtp)
        if should_send_otp:
            await interaction.response.defer(ephemeral=True, thinking=True)
            # Pre-flight validation: check rate limiting, ID format & uniqueness, and email domain & uniqueness
            is_valid, preflight_err = await self.service.validate_preflight_for_otp(
                user_id=interaction.user.id,
                raw_student_id=student_id_val,
                raw_email=student_email_val,
                guild=interaction.guild,
            )
            if not is_valid:
                await interaction.followup.send(preflight_err or "❌ Pre-flight check failed.", ephemeral=True)
                schedule_ttl_delete(interaction, delay=30.0)
                return

            info = parse_student_id(student_id_val)
            iso_expiry = (
                parse_card_expiry_date(raw_expiry)
                if raw_expiry
                else estimate_student_card_expiry(student_id_val, info.level_code)
            )
            today_iso = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")

            # If student card expiry date is in the past (graduated alumni cohort), offer Alumni Confirmation Gate
            if iso_expiry and iso_expiry < today_iso:
                confirm_embed = build_alumni_email_confirm_embed(
                    student_id=student_id_val,
                    email=student_email_val,
                    iso_expiry=iso_expiry,
                )
                confirm_view = AlumniEmailConfirmationView(
                    service=self.service,
                    email_service=self.email_service,
                    student_id=student_id_val,
                    email=student_email_val,
                    raw_expiry=raw_expiry,
                    iso_expiry=iso_expiry,
                )
                await interaction.followup.send(embed=confirm_embed, view=confirm_view, ephemeral=True)
                schedule_ttl_delete(interaction, delay=180.0)
                return

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
            view = OtpVerificationPromptView(self.service, self.email_service)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            schedule_ttl_delete(interaction, delay=float(self.email_service.settings.email_otp_ttl_seconds))
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        kwargs = {"raw_expiry_date": raw_expiry}
        if student_email_val:
            kwargs["raw_email"] = student_email_val

        response_text = await self.service.perform_verification(
            interaction.user,
            student_id_val,
            **kwargs,
        )
        view = None
        if hasattr(self.service, "db") and self.service.db and isinstance(interaction.user.id, int):
            try:
                details = await self.service.db.get_verification_details(interaction.user.id)
                if details and details.get("is_alumni") == 0:
                    card_exp = details.get("card_expiry_date")
                    today_iso = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")
                    if card_exp and card_exp < today_iso:
                        view = StudentLifecycleResolutionView(self.service, self.service.db)
            except Exception as e:
                logger.debug("Could not determine student lifecycle state: %s", e)

        if view:
            await interaction.followup.send(response_text, view=view, ephemeral=True)
        else:
            await interaction.followup.send(response_text, ephemeral=True)
        schedule_ttl_delete(interaction, delay=120.0 if view else 60.0)


class StudentOtpModal(discord.ui.Modal, title="🔢 Enter Verification Code"):
    otp_code = discord.ui.TextInput(
        label="6-Digit Verification Code",
        placeholder="e.g. 123456",
        min_length=6,
        max_length=6,
        required=True,
    )

    def __init__(self, service: VerificationService, email_service: EmailService):
        super().__init__()
        self.service = service
        self.email_service = email_service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        otp_val = self.otp_code.value.strip()
        result = await self.email_service.verify_otp(interaction.user.id, otp_val)
        if not result["success"]:
            view = OtpVerificationPromptView(self.service, self.email_service)
            await interaction.followup.send(
                f"{result['error']}",
                view=view,
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=120.0)
            return

        pending = result["pending"]
        response_text = await self.service.perform_verification(
            interaction.user,
            pending.student_id,
            raw_expiry_date=pending.card_expiry_date,
            raw_email=pending.email,
        )

        embed = discord.Embed(
            title="✅ Institutional Email & Student Verified!",
            description=(
                f"🎉 **{interaction.user.mention}** has verified their institutional email (`{mask_email(pending.email)}`).\n\n"
                f"{response_text}"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="TARVeri Secure Email OTP • Authenticated & Encrypted at Rest")
        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=120.0)


class OtpVerificationPromptView(discord.ui.View):
    """Interactive view providing buttons to enter code, resend code, or cancel."""

    def __init__(self, service: VerificationService, email_service: EmailService):
        super().__init__(timeout=600)
        self.service = service
        self.email_service = email_service

    @discord.ui.button(
        label="Enter Verification Code",
        style=discord.ButtonStyle.primary,
        emoji="🔢",
        custom_id="tarveri:email_otp:enter",
    )
    async def on_enter_code(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        pending = self.email_service.get_pending_otp(interaction.user.id)
        if not pending:
            await interaction.response.send_message(
                "❌ No active verification code found or it has expired. Please run `/verify` again.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(StudentOtpModal(self.service, self.email_service))

    @discord.ui.button(
        label="Resend Code",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="tarveri:email_otp:resend",
    )
    async def on_resend_code(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        pending = self.email_service.get_pending_otp(interaction.user.id)
        if not pending:
            await interaction.response.send_message(
                "❌ No active verification request found. Please run `/verify` again.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        server_name = interaction.guild.name if interaction.guild else "TARUMT Community"
        res = await self.email_service.generate_and_send_otp(
            user_id=interaction.user.id,
            student_id=pending.student_id,
            email_address=pending.email,
            server_name=server_name,
            card_expiry_date=pending.card_expiry_date,
        )
        if res["success"]:
            expire_ts = int(time.time()) + int(self.email_service.settings.email_otp_ttl_seconds)
            embed = discord.Embed(
                title="📬 Fresh Verification Code Sent!",
                description=(
                    f"A new 6-digit one-time verification code has been dispatched to:\n"
                    f"👉 `{mask_email(pending.email)}`\n\n"
                    "**Next Steps:**\n"
                    "1️⃣ Check your student email inbox *(or Spam/Junk folder)*.\n"
                    "2️⃣ Click **Enter Verification Code** below or type `/otp <code>`.\n\n"
                    f"⏱️ **Code expires:** <t:{expire_ts}:R> *(at <t:{expire_ts}:t>)*"
                ),
                color=discord.Color.blue(),
            )
            embed.set_footer(text="TARVeri Email Security • AES-256 Encrypted at Rest")
            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"❌ {res['error']}", ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="tarveri:email_otp:cancel",
    )
    async def on_cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.email_service.cancel_pending_otp(interaction.user.id)
        await interaction.response.send_message(
            "🛑 Verification cancelled. Your pending code has been removed.",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=30.0)


class VerificationCog(commands.Cog, name="Verification"):
    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        service: VerificationService,
        rate_limiter: RateLimiter,
        settings: Settings | None = None,
        guest_service: GuestService | None = None,
        email_service: EmailService | None = None,
    ):
        self.bot = bot
        self.db = db
        self.service = service
        self.rate_limiter = rate_limiter
        self.settings = settings or getattr(bot, "settings", None)
        self.guest_service = guest_service or getattr(bot, "guest_service", None)
        self.email_service = email_service or getattr(bot, "email_service", None)
        self._tip_cooldowns: dict[int, float] = {}
        self._lifecycle_cooldowns: dict[int, float] = {}
        self._guild_channels_cache: dict[int, tuple[int | None, int | None]] = {}

    @app_commands.command(
        name="verify",
        description="Verify your TARUMT student status and receive your faculty role.",
    )
    @app_commands.describe(
        student_id="Your TARUMT student ID (e.g. 23WMD09867). Leave blank to open input window.",
        email="Your TARUMT student email (e.g. 23wmd09867@student.tarc.edu.my, optional)",
        expiry_date="Student Card Expiry Date (MM/YY, e.g. 10/26, optional)",
    )
    async def verify_slash(
        self,
        interaction: discord.Interaction,
        student_id: str | None = None,
        email: str | None = None,
        expiry_date: str | None = None,
    ) -> None:
        """Slash command for verification with optional direct input or modal prompt."""
        if student_id:
            raw_student_id = student_id.strip()
            raw_email = email.strip() if email else None
            raw_expiry = expiry_date.strip() if expiry_date else None
            if raw_expiry:
                iso_expiry = parse_card_expiry_date(raw_expiry)
                if not iso_expiry:
                    await interaction.response.defer(ephemeral=True, thinking=True)
                    current_yy = str(datetime.now().year)[-2:]
                    await interaction.followup.send(
                        f"❌ Invalid student card expiry date format. Please use `MM/YY` (e.g. `10/{(int(current_yy) + 2) % 100:02d}`) "
                        f"or `DD/MM/YYYY` (e.g. `31/10/{datetime.now().year + 2}`), or leave it blank to auto-calculate.",
                        ephemeral=True,
                    )
                    schedule_ttl_delete(interaction, delay=30.0)
                    return

                is_anomalous, anomaly_reason = is_expiry_date_anomalous(iso_expiry, student_id=raw_student_id)
                if is_anomalous:
                    await interaction.response.defer(ephemeral=True, thinking=True)
                    embed = build_expiry_anomaly_embed(
                        raw_input=raw_expiry,
                        parsed_iso=iso_expiry,
                        student_id=raw_student_id,
                        anomaly_reason=anomaly_reason or "",
                    )
                    view = ExpiryAnomalyConfirmView(
                        service=self.service,
                        db=self.db,
                        student_id=raw_student_id,
                        raw_expiry_input=raw_expiry,
                        parsed_iso_date=iso_expiry,
                        anomaly_reason=anomaly_reason or "",
                    )
                    await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                    schedule_ttl_delete(interaction, delay=180.0)
                    return

            is_email_active = (
                self.email_service is not None
                and getattr(self.email_service, "is_enabled", False) is True
            )
            restrict_smtp = bool(
                self.email_service
                and getattr(self.email_service.settings, "email_restrict_smtp_usage", True) is True
            )
            guild_email_required = False
            if interaction.guild and self.db:
                guild_email_required = await self.db.is_guild_email_verification_enabled(interaction.guild.id)

            if is_email_active and guild_email_required and not raw_email:
                # Guild mandates email verification; open prefilled modal prompting for email
                modal = VerificationModal(self.service, self.email_service, require_email=True)
                modal.student_id.default = raw_student_id
                if raw_expiry:
                    modal.card_expiry.default = raw_expiry
                await interaction.response.send_modal(modal)
                return

            should_send_otp = is_email_active and raw_email and (guild_email_required or not restrict_smtp)
            if should_send_otp:
                await interaction.response.defer(ephemeral=True, thinking=True)
                # Pre-flight validation: check rate limiting, ID format & uniqueness, and email domain & uniqueness
                is_valid, preflight_err = await self.service.validate_preflight_for_otp(
                    user_id=interaction.user.id,
                    raw_student_id=raw_student_id,
                    raw_email=raw_email,
                    guild=interaction.guild,
                )
                if not is_valid:
                    await interaction.followup.send(preflight_err or "❌ Pre-flight check failed.", ephemeral=True)
                    schedule_ttl_delete(interaction, delay=30.0)
                    return

                info = parse_student_id(raw_student_id)
                iso_expiry = (
                    parse_card_expiry_date(raw_expiry)
                    if raw_expiry
                    else estimate_student_card_expiry(raw_student_id, info.level_code)
                )
                today_iso = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")

                # If student card expiry date is in the past (graduated alumni cohort), offer Alumni Confirmation Gate
                if iso_expiry and iso_expiry < today_iso:
                    confirm_embed = build_alumni_email_confirm_embed(
                        student_id=raw_student_id,
                        email=raw_email,
                        iso_expiry=iso_expiry,
                    )
                    confirm_view = AlumniEmailConfirmationView(
                        service=self.service,
                        email_service=self.email_service,
                        student_id=raw_student_id,
                        email=raw_email,
                        raw_expiry=raw_expiry,
                        iso_expiry=iso_expiry,
                    )
                    await interaction.followup.send(embed=confirm_embed, view=confirm_view, ephemeral=True)
                    schedule_ttl_delete(interaction, delay=180.0)
                    return

                server_name = interaction.guild.name if interaction.guild else "TARUMT Community"
                send_result = await self.email_service.generate_and_send_otp(
                    user_id=interaction.user.id,
                    student_id=raw_student_id,
                    email_address=raw_email,
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
                        f"👉 `{mask_email(raw_email)}`\n\n"
                        "**Next Steps:**\n"
                        "1️⃣ Check your student email inbox *(or Spam/Junk folder)*.\n"
                        "2️⃣ Click **Enter Verification Code** below or type `/otp <code>`.\n\n"
                        f"⏱️ **Code expires:** <t:{expire_ts}:R> *(at <t:{expire_ts}:t>)*"
                    ),
                    color=discord.Color.blue(),
                )
                embed.set_footer(text="TARVeri Email Security • AES-256 Encrypted at Rest")
                view = OtpVerificationPromptView(self.service, self.email_service)
                await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                schedule_ttl_delete(interaction, delay=float(self.email_service.settings.email_otp_ttl_seconds))
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            kwargs = {"raw_expiry_date": raw_expiry}
            if raw_email:
                kwargs["raw_email"] = raw_email
            response_text = await self.service.perform_verification(
                interaction.user,
                raw_student_id,
                **kwargs,
            )
            view = None
            if self.db and isinstance(interaction.user.id, int):
                try:
                    details = await self.db.get_verification_details(interaction.user.id)
                    if details and details.get("is_alumni") == 0:
                        card_exp = details.get("card_expiry_date")
                        today_iso = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")
                        if card_exp and card_exp < today_iso:
                            view = StudentLifecycleResolutionView(self.service, self.db)
                except Exception as e:
                    logger.debug("Could not determine student lifecycle state: %s", e)

            if view:
                await interaction.followup.send(response_text, view=view, ephemeral=True)
            else:
                await interaction.followup.send(response_text, ephemeral=True)
            schedule_ttl_delete(interaction, delay=120.0 if view else 60.0)
            return

        # Check if already verified — if so, resync silently without modal
        details = await self.db.get_verification_details(interaction.user.id)
        if details:
            await interaction.response.defer(ephemeral=True, thinking=True)
            stored_faculty = details.get("faculty_code")
            faculty_role = FACULTY_ROLES.get(stored_faculty)
            if faculty_role:
                campus_code = details.get("campus_code") or "W"
                campus_role = CAMPUS_ROLES.get(campus_code, "KL Main Campus")
                level_code = details.get("level_code") or "R"
                level_role = STUDY_LEVEL_ROLES.get(level_code, "Degree")
                mutual_guilds = await self.service.get_mutual_guilds_for_user(interaction.user.id)
                result = await self.service.assign_role_across_guilds(
                    interaction.user.id,
                    faculty_role,
                    mutual_guilds,
                    campus_role_name=campus_role,
                    level_role_name=level_role,
                )
                if details.get("is_alumni"):
                    try:
                        await self.service.sync_alumni_role_across_guilds(
                            interaction.user.id,
                            mutual_guilds,
                            reason="TARVeri: Resync alumni role",
                        )
                    except Exception as e:
                        logger.warning(f"Could not resync alumni role for {interaction.user}: {e}")

                summary = self.service.format_role_summary(result)
                msg = summary or "ℹ️ You're already verified and up to date in every server I share with you."
                msg += "\n💡 *If you are progressing to a new study level (e.g. Diploma -> Degree), run `/verify student_id:<your_new_id>` to transition.*"
                await interaction.followup.send(
                    msg,
                    ephemeral=True,
                )
                schedule_ttl_delete(interaction, delay=60.0)
                return

        guild_email_required = False
        if interaction.guild and self.db:
            guild_email_required = await self.db.is_guild_email_verification_enabled(interaction.guild.id)

        # Open the interactive modal dialog
        await interaction.response.send_modal(
            VerificationModal(self.service, self.email_service, require_email=guild_email_required)
        )

    @app_commands.command(
        name="otp",
        description="Submit your 6-digit email verification code to complete student verification.",
    )
    @app_commands.describe(
        code="The 6-digit one-time verification code sent to your student email inbox.",
    )
    async def otp_slash(
        self,
        interaction: discord.Interaction,
        code: str,
    ) -> None:
        """Slash command to verify OTP code directly."""
        if not self.email_service or not getattr(self.email_service, "is_enabled", False):
            await interaction.response.send_message(
                "❌ Email verification is currently disabled.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=30.0)
            return

        # Check if server opted in, SMTP is unrestricted, or user has a pending verification code
        if interaction.guild and hasattr(interaction.guild, "id") and isinstance(interaction.guild.id, int) and self.db:
            guild_email_opted_in = await self.db.is_guild_email_verification_enabled(interaction.guild.id)
            restrict_smtp = bool(
                self.email_service
                and getattr(self.email_service.settings, "email_restrict_smtp_usage", True) is True
            )
            pending = self.email_service.get_pending_otp(interaction.user.id)
            if restrict_smtp and not guild_email_opted_in and not pending:
                await interaction.response.send_message(
                    "ℹ️ This server has not mandated email verification. You can verify directly with `/verify`.",
                    ephemeral=True,
                )
                schedule_ttl_delete(interaction, delay=30.0)
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        otp_val = code.strip()
        result = await self.email_service.verify_otp(interaction.user.id, otp_val)
        if not result["success"]:
            view = OtpVerificationPromptView(self.service, self.email_service)
            await interaction.followup.send(
                f"{result['error']}",
                view=view,
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=120.0)
            return

        pending = result["pending"]
        response_text = await self.service.perform_verification(
            interaction.user,
            pending.student_id,
            raw_expiry_date=pending.card_expiry_date,
            raw_email=pending.email,
        )

        embed = discord.Embed(
            title="✅ Institutional Email & Student Verified!",
            description=(
                f"🎉 **{interaction.user.mention}** has verified their institutional email (`{mask_email(pending.email)}`).\n\n"
                f"{response_text}"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="TARVeri Secure Email OTP • Authenticated & Encrypted at Rest")
        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=120.0)

    @app_commands.command(
        name="graduate",
        description="Claim your official TARUMT Alumni status and unlock the Alumni role & card badge.",
    )
    @app_commands.describe(
        year="Your 4-digit graduation year. Leave blank to open input form.",
        programme="Your completed programme / degree (optional, e.g. Bachelor of Software Engineering)",
    )
    async def graduate_slash(
        self,
        interaction: discord.Interaction,
        year: int | None = None,
        programme: str | None = None,
    ) -> None:
        """Slash command to transition from student to verified alumni."""
        # 1. Preflight check: user must be verified in database first
        existing = await self.db.get_verification_by_user(interaction.user.id)
        if not existing:
            await interaction.response.send_message(
                "❌ You must be a verified TARUMT student before claiming Alumni status. "
                "Please run `/verify` first to verify your student account.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        # 2. If year not provided, open modal form
        if year is None:
            await interaction.response.send_modal(AlumniClaimModal(self.service))
            return

        # 3. Direct argument submission
        await interaction.response.defer(ephemeral=False, thinking=True)
        result = await self.service.claim_alumni_status(
            user_id=interaction.user.id,
            user_display_name=interaction.user.display_name,
            graduated_year=year,
            programme=programme,
            current_guild=interaction.guild,
        )

        if not result["success"]:
            await interaction.followup.send(result["message"], ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🎓 Congratulations on Graduating, {interaction.user.display_name}!",
            description=(
                f"🎉 **{interaction.user.mention}** has successfully registered their **TARUMT Alumni** status!\n\n"
                f"• **Class Cohort**: Class of {result['graduated_year']}\n"
                f"• **Faculty**: {result['faculty_name']}\n"
                + (f"• **Programme**: {result['programme']}\n" if result['programme'] else "")
                + f"\n🏷️ **`TARUMT Alumni`** role assigned in {result['guilds_updated']} server(s).\n"
                f"🪪 **`❖ ALUMNI`** badge unlocked on your Digital Campus Card (`/card`)."
            ),
            color=discord.Color.from_rgb(212, 175, 55),
        )
        embed.set_footer(text="TARVeri Alumni Verification • Instant & Tamper-Proof")
        await interaction.followup.send(embed=embed, ephemeral=False)

    @app_commands.command(
        name="dropout",
        description="Discontinue your student status and withdraw student verification roles.",
    )
    async def dropout_slash(self, interaction: discord.Interaction) -> None:
        """Slash command for a verified student to withdraw and remove their student verification."""
        existing = await self.db.get_verification_by_user(interaction.user.id)
        if not existing:
            await interaction.response.send_message(
                "❌ You do not have an active student verification record in the database.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=30.0)
            return

        await interaction.response.send_modal(StudentDropoutConfirmModal(self.service, self.db))

    def invalidate_guild_cache(self, guild_id: int | None = None) -> None:
        """Clears cached channel settings for a guild or all guilds."""
        if guild_id is not None:
            self._guild_channels_cache.pop(guild_id, None)
        else:
            self._guild_channels_cache.clear()

    async def get_guild_channel_ids(self, guild_id: int) -> tuple[int | None, int | None]:
        """Fetches (welcome_channel_id, help_channel_id) for a guild with memory caching."""
        if not isinstance(guild_id, int):
            return (None, None)
        if guild_id in self._guild_channels_cache:
            return self._guild_channels_cache[guild_id]

        row = await self.db.get_guild_settings(guild_id)
        settings = (row[0], row[1]) if row else (None, None)
        self._guild_channels_cache[guild_id] = settings
        return settings

    async def is_help_channel(self, channel: discord.TextChannel) -> bool:
        """Determines if a channel is the designated help channel (per-guild DB, env setting, or keyword)."""
        if not isinstance(channel, discord.TextChannel) or not channel.guild:
            return False

        # 1. Per-server configured help channel in database
        _, guild_help_id = await self.get_guild_channel_ids(channel.guild.id)
        if guild_help_id is not None:
            configured_ch = channel.guild.get_channel(guild_help_id)
            if configured_ch is not None:
                return channel.id == guild_help_id
            else:
                # Channel was deleted on Discord — self-heal database setting
                await self.db.clear_stale_channel_setting(channel.guild.id, "help")
                self.invalidate_guild_cache(channel.guild.id)

        # 2. Global fallback setting from environment
        if self.settings and self.settings.help_channel_id:
            if channel.id == self.settings.help_channel_id:
                return True

        # 3. Autodetect: keywords: help, support, bantuan, faq, verify, verification, ask, question
        keywords = ("help", "support", "bantuan", "faq", "verify", "verification", "ask", "question")
        name_lower = channel.name.lower()
        matches_keyword = any(k in name_lower for k in keywords)

        if not matches_keyword:
            return False

        # Verify default role (@everyone) can view and send messages (unverified users can chat)
        if hasattr(channel, "permissions_for") and hasattr(channel.guild, "default_role"):
            everyone_perms = channel.permissions_for(channel.guild.default_role)
            if hasattr(everyone_perms, "view_channel") and hasattr(everyone_perms, "send_messages"):
                if not (everyone_perms.view_channel and everyone_perms.send_messages):
                    return False

        return True

    async def get_welcome_or_verify_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Finds the best channel to tag newly joined members for verification."""
        def _can_bot_send(c: discord.TextChannel) -> bool:
            if not hasattr(c, "permissions_for") or not hasattr(guild, "me") or not guild.me:
                return True
            perms = c.permissions_for(guild.me)
            return bool(perms.view_channel and perms.send_messages)

        # 1. Per-server configured welcome channel in database
        guild_welcome_id, _ = await self.get_guild_channel_ids(guild.id)
        if guild_welcome_id is not None:
            ch = guild.get_channel(guild_welcome_id)
            if isinstance(ch, discord.TextChannel) and _can_bot_send(ch):
                return ch
            elif ch is None:
                # Channel was deleted on Discord — self-heal database setting
                await self.db.clear_stale_channel_setting(guild.id, "welcome")
                self.invalidate_guild_cache(guild.id)

        # 2. Global configured welcome channel ID from settings (.env)
        if self.settings and self.settings.welcome_channel_id:
            ch = guild.get_channel(self.settings.welcome_channel_id)
            if isinstance(ch, discord.TextChannel) and _can_bot_send(ch):
                return ch

        # 3. Global configured help channel ID from settings (.env)
        if self.settings and self.settings.help_channel_id:
            ch = guild.get_channel(self.settings.help_channel_id)
            if isinstance(ch, discord.TextChannel) and _can_bot_send(ch):
                return ch

        # 4. Autodetect channel by priority keywords: welcome, verify, verification, start-here, help
        keywords = ("welcome", "verify", "verification", "start-here", "gate", "rules", "help")
        for kw in keywords:
            for ch in guild.text_channels:
                if kw in ch.name.lower() and _can_bot_send(ch):
                    return ch

        # 5. Guild system channel (standard Discord welcome channel)
        if guild.system_channel and _can_bot_send(guild.system_channel):
            return guild.system_channel

        # 6. First text channel bot can send to
        for ch in guild.text_channels:
            if _can_bot_send(ch):
                return ch

        return None

    def is_unverified_member(self, member: discord.Member) -> bool:
        """Checks if a member does not hold any TARVeri faculty role or approved guest role."""
        member_roles = getattr(member, "roles", [])
        if isinstance(member_roles, (list, tuple)):
            for r in member_roles:
                r_name = getattr(r, "name", "")
                if not r_name:
                    continue
                # Dynamic faculty role check
                if any(VerificationService._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES):
                    return False
                # Dynamic guest / visitor role check
                if GUEST_ROLE_PATTERN.search(r_name):
                    return False
        return True

    async def handle_help_channel_message(self, message: discord.Message) -> None:
        """Alerts unverified members asking about roles or verification with helpful interactive gateway buttons."""
        if not message.content or not isinstance(message.author, discord.Member) or message.author.bot:
            return

        # Do not respond to commands or prefixes
        if message.content.startswith("/") or message.content.startswith("!"):
            return

        # Only trigger for members who do not hold a faculty role or guest role in this server
        if not self.is_unverified_member(message.author):
            return

        # Check if message contains role or verification inquiry keywords
        if not ROLE_HELP_KEYWORDS_PATTERN.search(message.content):
            return

        # Rate limit tips per user (60-second cooldown) to avoid spamming chat
        now = time.monotonic()
        last_time = self._tip_cooldowns.get(message.author.id)
        if last_time is not None and (now - last_time < 60.0):
            return
        self._tip_cooldowns[message.author.id] = now

        if len(self._tip_cooldowns) > 1000:
            self._tip_cooldowns = {uid: t for uid, t in self._tip_cooldowns.items() if now - t < 60.0}

        tip_embed = discord.Embed(
            title="🎓 TARUMT Student & Guest Verification",
            description=(
                f"👋 Hello {message.author.mention}! Looking to get your student or guest role?\n\n"
                f"Choose an option below to get verified:\n"
                f"• 🎓 **TARUMT Students:** Click **Verify TARUMT Student** or type `/verify` to enter your Student ID.\n"
                f"• 🎟️ **Referral Code:** Click **Enter Referral Code** if you received an invite code.\n"
                f"• 🌐 **Outside Guests:** Click **Apply as Guest** to request staff approval."
            ),
            color=discord.Color.blue(),
        )
        tip_embed.set_footer(text="TARVeri • Click a button below to get verified")

        view = (
            VerificationGatewayView(self.service, self.guest_service)
            if self.guest_service
            else None
        )

        try:
            await message.reply(embed=tip_embed, view=view, mention_author=True)
        except (discord.HTTPException, discord.Forbidden):
            try:
                await message.channel.send(embed=tip_embed, view=view)
            except (discord.HTTPException, discord.Forbidden) as e:
                logger.warning(f"Could not send role help tip in #{message.channel.name}: {e}")
                return

        if message.guild:
            await self.db.log(
                "INFO",
                "ROLE_HELP_TIP",
                f"Alerted user {message.author} (ID: {message.author.id}) with role tips in #{message.channel.name} of '{message.guild.name}' (Guild ID: {message.guild.id})",
                guild=message.guild,
                user_id=message.author.id,
            )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Automatically assigns faculty, campus, study level, and alumni roles if member is already verified, else prompts and tags."""
        # 0. Blacklist guard: ignore blacklisted users
        is_bl, _ = await self.db.is_blacklisted(member.guild.id, user_id=member.id)
        if is_bl:
            return

        details = await self.db.get_verification_details(member.id)
        if details:
            is_bl_hashes, _ = await self.db.is_blacklisted(
                member.guild.id,
                user_id=member.id,
                student_id_hash=details.get("student_id_hash"),
                email_hash=details.get("student_email_hash"),
            )
            if is_bl_hashes:
                return

            is_email_verified = bool(details.get("student_email_hash"))
            guild_email_required = await self.db.is_guild_email_verification_enabled(member.guild.id)

            if guild_email_required and not is_email_verified:
                # User is Fast Verified (Tier 1), but server mandates institutional email OTP (Tier 2).
                welcome_channel = await self.get_welcome_or_verify_channel(member.guild)
                if welcome_channel:
                    welcome_embed = discord.Embed(
                        title="📧 Institutional Email Verification Required",
                        description=(
                            f"Welcome {member.mention} to **{member.guild.name}**!\n\n"
                            "You are currently verified with TARVeri, but this server mandates "
                            "**institutional email OTP verification** (`@student.tarc.edu.my`) for student access.\n\n"
                            "Please click **Verify TARUMT Student** below or run `/verify` to link your institutional email."
                        ),
                        color=discord.Color.gold(),
                    )
                    welcome_embed.set_footer(text="TARVeri Verification System • Tiered Trust")
                    view = (
                        VerificationGatewayView(self.service, self.guest_service)
                        if self.guest_service
                        else None
                    )
                    try:
                        await welcome_channel.send(
                            content=f"👋 Welcome {member.mention}!",
                            embed=welcome_embed,
                            view=view,
                        )
                        await self.db.log(
                            "INFO",
                            "EMAIL_VERIFY_PROMPTED",
                            f"Prompted returning student {member} (ID: {member.id}) for required email verification in '{member.guild.name}'",
                            guild=member.guild,
                            user_id=member.id,
                        )
                    except discord.HTTPException:
                        pass
                return

            stored_faculty = details.get("faculty_code")
            faculty_role = FACULTY_ROLES.get(stored_faculty)
            if faculty_role:
                campus_code = details.get("campus_code") or "W"
                campus_role = CAMPUS_ROLES.get(campus_code, "KL Main Campus")
                level_code = details.get("level_code") or "R"
                level_role = STUDY_LEVEL_ROLES.get(level_code, "Degree")

                result = await self.service.assign_role_across_guilds(
                    member.id,
                    faculty_role,
                    [member.guild],
                    campus_role_name=campus_role,
                    level_role_name=level_role,
                    is_email_verified=is_email_verified,
                )

                if details.get("is_alumni"):
                    try:
                        await self.service.sync_alumni_role_across_guilds(
                            member.id,
                            [member.guild],
                            reason="TARVeri: Auto-assigned returning alumni role on join",
                        )
                    except Exception as e:
                        logger.warning(f"Could not auto-sync alumni role for {member} on join: {e}")

                if result.verified_in:
                    assigned_labels = [item[2] if len(item) == 3 else item[1] for item in result.verified_in]
                    assigned_str = ", ".join(dict.fromkeys(assigned_labels)) if assigned_labels else faculty_role
                    await self.db.log(
                        "INFO",
                        "AUTO_SYNC_JOIN",
                        f"Auto-assigned '{assigned_str}' to returning verified member {member} in '{member.guild.name}'",
                        guild=member.guild,
                        user_id=member.id,
                    )
                    try:
                        roles_display = f"**{assigned_str}**" if assigned_str else f"**{faculty_role}**"
                        await member.send(
                            f"🎓 Welcome to **{member.guild.name}**! Because you are already verified with TARVeri, "
                            f"you have automatically received your {roles_display} role(s)."
                        )
                    except discord.Forbidden:
                        pass
                return

        # New unverified member: Tag them in the server welcome/verification channel with embed and buttons
        welcome_channel = await self.get_welcome_or_verify_channel(member.guild)
        if welcome_channel:
            welcome_embed = discord.Embed(
                title="🎓 Welcome to the Server!",
                description=(
                    f"Welcome {member.mention} to **{member.guild.name}**!\n\n"
                    "Please choose an option below to gain access to the server:\n\n"
                    "• 🎓 **TARUMT Students:** Click **Verify TARUMT Student** to enter your Student ID and receive your Faculty Role.\n"
                    "• 🎟️ **Referral Code:** Click **Enter Referral Code** if a current student gave you an invite code.\n"
                    "• 🌐 **Outside Guests / Speakers:** Click **Apply as Guest** to request access from server staff."
                ),
                color=discord.Color.blue(),
            )
            welcome_embed.set_footer(text="TARVeri Student & Guest Verification • Instant & Secure")

            view = (
                VerificationGatewayView(self.service, self.guest_service)
                if self.guest_service
                else None
            )

            try:
                await welcome_channel.send(
                    content=f"👋 Welcome {member.mention}!",
                    embed=welcome_embed,
                    view=view,
                )
                await self.db.log(
                    "INFO",
                    "MEMBER_JOIN_TAGGED",
                    f"Tagged new member {member} (ID: {member.id}) for verification in #{welcome_channel.name} of '{member.guild.name}' (Guild ID: {member.guild.id})",
                    guild=member.guild,
                    user_id=member.id,
                )
            except (discord.HTTPException, discord.Forbidden) as e:
                logger.warning(
                    f"Failed to tag new member {member} in #{welcome_channel.name} ({member.guild.name}): {e}"
                )

        try:
            dm_embed = discord.Embed(
                title=f"🎓 Welcome to {member.guild.name}!",
                description=(
                    "Please choose an option below to verify and unlock your server access, "
                    "or reply directly with your student ID (e.g. `23WMD09867`):"
                ),
                color=discord.Color.blue(),
            )
            dm_view = (
                VerificationGatewayView(self.service, self.guest_service)
                if self.guest_service
                else None
            )
            await member.send(embed=dm_embed, view=dm_view)
        except discord.Forbidden:
            await self.db.log(
                "INFO",
                "DM_BLOCKED_JOIN",
                f"Couldn't send welcome DM to {member} (ID: {member.id}) in '{member.guild.name}' — DMs closed.",
                guild=member.guild,
                user_id=member.id,
            )

    async def handle_expired_student_activity(self, message: discord.Message) -> None:
        """Checks if an active student has an expired student card and needs a lifecycle resolution prompt."""
        if not message.author or message.author.bot or message.guild is None:
            return

        now = time.monotonic()
        last_time = self._lifecycle_cooldowns.get(message.author.id)
        # In-memory cooldown: 1 day per user session
        if last_time is not None and (now - last_time < 86400.0):
            return

        details = await self.db.get_verification_details(message.author.id)
        if not details or details.get("is_alumni") == 1:
            return

        card_expiry = details.get("card_expiry_date")
        if not card_expiry:
            return

        from datetime import datetime, timedelta
        now_dt = datetime.now(get_configured_tz())
        today_iso = now_dt.strftime("%Y-%m-%d")
        if card_expiry >= today_iso:
            return

        # Expired! Record in-memory cooldown
        self._lifecycle_cooldowns[message.author.id] = now
        if len(self._lifecycle_cooldowns) > 1000:
            self._lifecycle_cooldowns = {uid: t for uid, t in self._lifecycle_cooldowns.items() if now - t < 86400.0}

        # Check DB prompt cooldown (7 days)
        last_prompt_str = details.get("last_lifecycle_prompt_at")
        if last_prompt_str:
            last_prompt_dt = parse_db_timestamp(last_prompt_str)
            if last_prompt_dt and (now_dt - last_prompt_dt < timedelta(days=7)):
                return

        expiry_disp = format_card_expiry_display(card_expiry)
        embed = discord.Embed(
            title="🎓 TARUMT Student Card Expiry & Academic Status Confirmation",
            description=(
                f"Hello {message.author.mention}!\n\n"
                f"According to TARVeri records, your TARUMT student card reached its validity date (**{expiry_disp}**).\n\n"
                "Please confirm your current academic status:\n\n"
                "• 🎓 **I have Graduated:** Claim your official **TARUMT Alumni** role & card badge.\n"
                "• 📚 **Further Studies at TARUMT:** Progressing to Degree / Masters? Update your student ID & study level.\n"
                "• ⏳ **Still Studying / Extension:** Extending semester or final year project? Update your card expiry date.\n"
                "• 🚪 **Discontinue Studies / Dropout:** Discontinuing studies? Withdraw your student verification."
            ),
            color=discord.Color.from_rgb(212, 175, 55),
        )
        embed.set_footer(text="TARVeri Academic Lifecycle Engine • Click an option below to update")
        view = StudentLifecycleResolutionView(self.service, self.db)

        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        try:
            await message.author.send(embed=embed, view=view)
            await self.db.update_verification_profile(
                discord_user_id=message.author.id,
                lifecycle_prompt_status="prompted",
                last_lifecycle_prompt_at=now_iso,
            )
            await self.db.log(
                "INFO",
                "GRADUATION_PROMPT_SENT",
                f"Sent on-active-chat lifecycle prompt to {message.author} (ID: {message.author.id}, card expired: {expiry_disp})",
                user_id=message.author.id,
                guild=message.guild,
            )
        except discord.Forbidden:
            await self.db.update_verification_profile(
                discord_user_id=message.author.id,
                last_lifecycle_prompt_at=now_iso,
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Handles student ID messages in direct messages (DMs) and role tips in help channels."""
        if message.author.bot:
            return

        if message.guild is None:
            content = message.content.strip()
            if content:
                parts = content.split()
                student_id_input = parts[0]
                expiry_input = parts[1] if len(parts) > 1 else None
                if expiry_input:
                    response = await self.service.perform_verification(
                        message.author, student_id_input, raw_expiry_date=expiry_input
                    )
                else:
                    response = await self.service.perform_verification(message.author, student_id_input)
                if response:
                    view = None
                    if self.db and isinstance(message.author.id, int):
                        try:
                            details = await self.db.get_verification_details(message.author.id)
                            if details and details.get("is_alumni") == 0:
                                card_exp = details.get("card_expiry_date")
                                today_iso = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")
                                if card_exp and card_exp < today_iso:
                                    view = StudentLifecycleResolutionView(self.service, self.db)
                        except Exception as e:
                            logger.debug("Could not determine student lifecycle state: %s", e)
                    if view:
                        await message.author.send(response, view=view)
                    else:
                        await message.author.send(response)
        else:
            if isinstance(message.channel, discord.TextChannel) and await self.is_help_channel(message.channel):
                await self.handle_help_channel_message(message)
            await self.handle_expired_student_activity(message)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.db.log(
            "INFO", "GUILD_JOIN", f"Joined server '{guild.name}' (members: {guild.member_count})", guild=guild
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.db.log("INFO", "GUILD_REMOVE", f"Left server '{guild.name}'", guild=guild)
