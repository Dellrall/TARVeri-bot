"""
Discord UI Modals and Views for Student Verification and Academic Lifecycle Management.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import discord

from tarveri.config import (
    estimate_student_card_expiry,
    format_card_expiry_display,
    get_configured_tz,
    is_expiry_date_anomalous,
    mask_email,
    parse_card_expiry_date,
    parse_student_id,
)
from tarveri.database import Database
from tarveri.services.email_service import EmailService
from tarveri.services.verification_service import VerificationService
from tarveri.utils import schedule_ttl_delete

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
                + (f"• **Programme**: {result['programme']}\n" if result["programme"] else "")
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
        placeholder="e.g. name-wm24@student.tarc.edu.my (Optional)",
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

        is_global_email_active = bool(self.email_service and getattr(self.email_service, "is_enabled", False) is True)
        if is_global_email_active and self.require_email:
            self.student_email.required = True
            self.student_email.placeholder = f"e.g. name-wm{current_yy}@student.tarc.edu.my (Required)"
        else:
            self.student_email.required = False
            self.student_email.placeholder = f"e.g. name-wm{current_yy}@student.tarc.edu.my (Optional)"

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

        is_email_active = bool(self.email_service and getattr(self.email_service, "is_enabled", False) is True)
        restrict_smtp = bool(
            self.email_service and getattr(self.email_service.settings, "email_restrict_smtp_usage", True) is True
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

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any]) -> None:
        if isinstance(error, (discord.NotFound, discord.InteractionResponded)) or (
            isinstance(error, discord.HTTPException) and error.code in (10062, 40060)
        ):
            logger.debug(
                "Transient Discord interaction timeout (10062 / 404) on item '%s' for user ID %s: %s",
                getattr(item, "label", item),
                getattr(interaction.user, "id", "Unknown"),
                error,
            )
            return
        logger.error("Unhandled error in OtpVerificationPromptView for item %s: %s", item, error, exc_info=error)
