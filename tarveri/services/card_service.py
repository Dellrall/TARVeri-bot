"""
Digital Student & Guest Card generator service using Pillow.
Renders high-quality, privacy-safe campus ID cards and passports.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import os
from datetime import datetime
from typing import Any

import discord
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from tarveri.config import (
    CAMPUS_ROLE_NAMES,
    CAMPUS_ROLES,
    FACULTY_ROLES,
    GUEST_ROLE_PATTERN,
    STUDY_LEVEL_ROLE_NAMES,
    STUDY_LEVEL_ROLES,
    format_card_expiry_display,
    get_configured_tz,
)
from tarveri.database import Database
from tarveri.utils import parse_db_timestamp

logger = logging.getLogger("tarveri")

# Faculty full name mapping for aesthetic card display
FACULTY_FULL_NAMES: dict[str, str] = {
    "FOCS": "Faculty of Computing and Information Technology",
    "FAFB": "Faculty of Accountancy, Finance and Business",
    "FCCI": "Faculty of Communication and Creative Industries",
    "FOAS": "Faculty of Applied Sciences",
    "FOBE": "Faculty of Built Environment",
    "FOET": "Faculty of Engineering and Technology",
    "FSSH": "Faculty of Social Science and Humanities",
    "CPUS": "Centre for Pre-University Studies",
}

# Rich RGB Faculty Color Themes (Primary, Accent, Gradient)
FACULTY_THEMES: dict[str, dict[str, tuple[int, int, int]]] = {
    "FAFB": {
        "primary": (169, 50, 38),  # Dark Red
        "accent": (235, 150, 140),
        "glow": (217, 83, 79),
    },
    "CPUS": {
        "primary": (22, 130, 115),  # Dark Teal
        "accent": (115, 205, 195),
        "glow": (26, 188, 156),
    },
    "FOCS": {
        "primary": (212, 140, 10),  # Golden Amber
        "accent": (250, 215, 120),
        "glow": (241, 196, 15),
    },
    "FCCI": {
        "primary": (113, 54, 138),  # Violet Purple
        "accent": (195, 155, 220),
        "glow": (142, 68, 173),
    },
    "FOAS": {
        "primary": (192, 57, 43),  # Coral Crimson
        "accent": (245, 160, 150),
        "glow": (231, 76, 60),
    },
    "FOBE": {
        "primary": (30, 132, 73),  # Emerald Green
        "accent": (130, 224, 170),
        "glow": (46, 204, 113),
    },
    "FSSH": {
        "primary": (36, 113, 163),  # Royal Blue
        "accent": (145, 195, 235),
        "glow": (52, 152, 219),
    },
    "FOET": {
        "primary": (140, 175, 45),  # Lime Green
        "accent": (215, 240, 140),
        "glow": (186, 233, 115),
    },
    "GUEST": {
        "primary": (26, 150, 120),  # Forest Mint
        "accent": (135, 225, 205),
        "glow": (46, 204, 113),
    },
    "UNVERIFIED": {
        "primary": (60, 75, 95),  # Slate Gray
        "accent": (160, 175, 195),
        "glow": (100, 115, 140),
    },
}

_CUSTOM_FONT = os.getenv("TARVERI_FONT_PATH", "").strip()

FONT_CANDIDATES = ([_CUSTOM_FONT] if _CUSTOM_FONT else []) + [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:\\Windows\\Fonts\\arial.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
]


@functools.lru_cache(maxsize=32)
def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Safely loads a TrueType font with graceful fallback to Pillow default font."""
    for path in FONT_CANDIDATES:
        if bold and ("Bold" in path or "-Bold" in path):
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size)
                except (OSError, Exception) as exc:
                    logger.debug("Failed loading candidate bold font %s: %s", path, exc)
        elif not bold and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except (OSError, Exception) as exc:
                logger.debug("Failed loading candidate font %s: %s", path, exc)

    # Fallback to any existing candidate
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except (OSError, Exception) as exc:
                logger.debug("Failed loading fallback font %s: %s", path, exc)

    return ImageFont.load_default()


class CardService:
    def __init__(self, db: Database, admin_role_name: str = "TARVeri Admin"):
        self.db = db
        self.admin_role_name = admin_role_name

    async def get_user_card_data(self, guild: discord.Guild, member: discord.Member | discord.User) -> dict[str, Any]:
        """Gathers verified student / guest record and badges for card generation."""
        user_id = member.id
        verification = await self.db.get_verification_by_user(user_id)

        is_verified_student = verification is not None
        faculty_code = None
        faculty_name = None
        verified_at_str = None
        hash_preview = None

        if verification:
            stored_hash, stored_faculty, verified_at_str = verification
            faculty_code = stored_faculty
            faculty_name = FACULTY_ROLES.get(stored_faculty, stored_faculty)
            hash_preview = f"TRV-{stored_hash[:4].upper()}-{stored_hash[-4:].upper()}"
        else:
            # Check if user is an approved guest in this guild
            guild_id = getattr(guild, "id", None)
            guest_ticket = (
                await self.db.get_latest_guest_ticket_for_user(guild_id, user_id) if isinstance(guild_id, int) else None
            )
            if guest_ticket and guest_ticket.get("status") == "APPROVED":
                faculty_code = "GUEST"
                faculty_name = "Guest(Approved)"
                verified_at_str = guest_ticket.get("closed_at") or guest_ticket.get("created_at")
                t_id = guest_ticket.get("ticket_seq") or guest_ticket.get("ticket_id")
                hash_preview = f"GST-{t_id:04d}" if isinstance(t_id, int) else f"GST-{t_id}"
            else:
                has_guest_role = False
                if isinstance(member, discord.Member):
                    for r in getattr(member, "roles", []):
                        r_name = getattr(r, "name", "").strip()
                        if GUEST_ROLE_PATTERN.search(r_name):
                            faculty_code = "GUEST"
                            faculty_name = r_name
                            hash_preview = f"GST-{user_id % 10000:04d}"
                            has_guest_role = True
                            break
                if not has_guest_role:
                    faculty_code = "UNVERIFIED"
                    faculty_name = "Unverified"
                    hash_preview = f"UNV-{user_id % 10000:04d}"

        # Alumni check
        alumni_info = await self.db.get_alumni_info_by_user(user_id) if is_verified_student else None
        is_alumni = bool(alumni_info and alumni_info.get("is_alumni"))
        graduated_year = alumni_info.get("graduated_year") if alumni_info else None
        programme = alumni_info.get("programme") if alumni_info else None

        # Badges extraction
        badges: list[str] = []
        if is_verified_student:
            badges.append(f"✦ {faculty_name}")
            if is_alumni:
                badges.append("❖ ALUMNI")
            badges.append("✓ VERIFIED")
        elif faculty_code == "GUEST":
            badges.append("◈ GUEST")
            badges.append("✓ APPROVED")
        else:
            badges.append("○ UNVERIFIED")

        # Admin / Staff badge
        if isinstance(member, discord.Member):
            if member.guild_permissions.administrator or any(
                getattr(r, "name", "") == self.admin_role_name for r in member.roles
            ):
                badges.append("★ STAFF")

            # Server Booster badge
            if getattr(member, "premium_since", None) is not None:
                badges.append("▲ BOOSTER")

            # Veteran badge (joined > 180 days ago)
            joined_at = member.joined_at
            if joined_at:
                now = datetime.now(get_configured_tz())
                if (now - joined_at).days > 180:
                    badges.append("⚡ VETERAN")

        # Super voucher badge (vouched for 2+ successful guest referrals)
        vouch_count = await self.db.count_successful_referrals_by_user(user_id)
        if vouch_count >= 2:
            badges.append(f"◈ VOUCHER x{vouch_count}")

        # Server join date string
        joined_str = "N/A"
        if isinstance(member, discord.Member) and member.joined_at:
            joined_str = member.joined_at.strftime("%d %b %Y")

        # Verification date string
        verified_date_str = "Not Verified"
        if verified_at_str:
            dt = parse_db_timestamp(verified_at_str)
            if dt:
                verified_date_str = dt.strftime("%d %b %Y")
            else:
                verified_date_str = verified_at_str.split(" ")[0]

        # Campus cohort string and dynamic branch/level detection
        campus_name = None
        level_name = None
        card_expiry_date = None
        if is_verified_student:
            details = await self.db.get_verification_details(user_id)
            if details:
                c_code = details.get("campus_code")
                l_code = details.get("level_code")
                card_expiry_date = details.get("card_expiry_date")
                if c_code:
                    campus_name = CAMPUS_ROLES.get(c_code)
                if l_code:
                    level_name = STUDY_LEVEL_ROLES.get(l_code)

        # Fallback to scanning member roles if not recorded in DB
        if not campus_name and isinstance(member, discord.Member):
            for r in getattr(member, "roles", []):
                for c_target in CAMPUS_ROLE_NAMES:
                    if r.name.strip().lower() == c_target.lower():
                        campus_name = c_target
                        break
                if campus_name:
                    break

        if not level_name and isinstance(member, discord.Member):
            for r in getattr(member, "roles", []):
                for l_target in STUDY_LEVEL_ROLE_NAMES:
                    if r.name.strip().lower() == l_target.lower():
                        level_name = l_target
                        break
                if level_name:
                    break

        expiry_display = format_card_expiry_display(card_expiry_date)
        is_card_expired = False
        if card_expiry_date:
            today_str = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")
            is_card_expired = card_expiry_date <= today_str

        if is_alumni:
            cohort_str = (
                f"Class of {graduated_year} • {faculty_name} Alumni" if graduated_year else f"{faculty_name} Alumni"
            )
        elif is_verified_student:
            parts = [campus_name or "TARUMT Main Campus"]
            if level_name:
                parts.append(level_name)
            cohort_str = " • ".join(parts)
        elif faculty_code == "GUEST":
            cohort_str = "Verified Affiliate"
        else:
            cohort_str = "Public Guest"

        return {
            "user_id": user_id,
            "display_name": getattr(member, "display_name", str(member)),
            "username": getattr(member, "name", str(member)),
            "is_verified": is_verified_student or faculty_code == "GUEST",
            "is_student": is_verified_student,
            "is_guest": faculty_code == "GUEST",
            "is_alumni": is_alumni,
            "graduated_year": graduated_year,
            "programme": programme,
            "faculty_code": faculty_code,
            "faculty_name": faculty_name,
            "faculty_full": FACULTY_FULL_NAMES.get(faculty_name, "TARUMT Student Community"),
            "hash_preview": hash_preview,
            "verified_at": verified_date_str,
            "joined_at": joined_str,
            "cohort_str": cohort_str,
            "card_expiry_date": card_expiry_date,
            "expiry_display": expiry_display,
            "is_card_expired": is_card_expired,
            "guild_name": guild.name if guild else "TARUMT Community",
            "badges": badges[:6],  # Allow up to 6 badges
        }

    def get_card_color(self, card_data: dict[str, Any]) -> discord.Color:
        """Returns the discord.Color corresponding to the card's faculty theme."""
        theme_key = card_data.get("faculty_name", "UNVERIFIED")
        theme = FACULTY_THEMES.get(
            theme_key, FACULTY_THEMES.get(card_data.get("faculty_code", "UNVERIFIED"), FACULTY_THEMES["UNVERIFIED"])
        )
        r, g, b = theme["primary"]
        return discord.Color.from_rgb(r, g, b)

    async def render_card_from_data(
        self,
        card_data: dict[str, Any],
        avatar_bytes: bytes | None = None,
    ) -> io.BytesIO:
        """Renders card directly from pre-fetched card data dictionary."""
        return await asyncio.to_thread(_draw_card_image, card_data, avatar_bytes)

    async def render_card(
        self,
        guild: discord.Guild,
        member: discord.Member | discord.User,
        avatar_bytes: bytes | None = None,
    ) -> io.BytesIO:
        """Asynchronously renders a digital student card image, returning a BytesIO PNG buffer."""
        card_data = await self.get_user_card_data(guild, member)
        return await self.render_card_from_data(card_data, avatar_bytes)


def _draw_card_image(data: dict[str, Any], avatar_bytes: bytes | None) -> io.BytesIO:
    """Synchronous Pillow rendering function executed in thread pool."""
    width, height = 920, 530
    theme_key = data.get("faculty_name", "UNVERIFIED")
    theme = FACULTY_THEMES.get(
        theme_key, FACULTY_THEMES.get(data.get("faculty_code", "UNVERIFIED"), FACULTY_THEMES["UNVERIFIED"])
    )

    primary_color = theme["primary"]
    accent_color = theme["accent"]
    glow_color = theme["glow"]

    # Base background (Dark high-tech glassmorphism canvas)
    base = Image.new("RGBA", (width, height), (15, 17, 26, 255))

    # 1. Background geometric gradient ribbons
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    # Glowing decorative curves & mesh gradients in faculty theme color
    overlay_draw.ellipse(
        (-100, -100, 480, 480),
        fill=(primary_color[0], primary_color[1], primary_color[2], 40),
    )
    overlay_draw.ellipse(
        (width - 350, height - 300, width + 150, height + 150),
        fill=(glow_color[0], glow_color[1], glow_color[2], 30),
    )
    overlay_draw.polygon(
        [(width - 280, 0), (width, 0), (width, 320), (width - 120, 320)],
        fill=(primary_color[0], primary_color[1], primary_color[2], 25),
    )

    # Blur overlay for smooth radiant ambient lighting
    overlay = overlay.filter(ImageFilter.GaussianBlur(30))
    base = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(base)

    # 2. Outer Card Frame & Rounded Border
    card_rect = [(18, 18), (width - 18, height - 18)]
    draw.rounded_rectangle(card_rect, radius=24, outline=(45, 52, 75, 255), width=2)
    # Inner accent top bar
    draw.rounded_rectangle([(20, 20), (width - 20, 29)], radius=4, fill=(*primary_color, 255))

    # 3. Header Section
    font_header_sub = _load_font(13, bold=True)
    font_header_main = _load_font(22, bold=True)

    draw.text(
        (45, 38),
        "TUNKU ABDUL RAHMAN UNIVERSITY OF MANAGEMENT AND TECHNOLOGY",
        fill=(195, 205, 230, 255),
        font=font_header_sub,
    )
    draw.text((45, 58), "TARUMT DIGITAL CAMPUS PASSPORT", fill=(255, 255, 255, 255), font=font_header_main)

    # Watermark / Server tag on top right
    guild_tag = data["guild_name"]
    if len(guild_tag) > 26:
        guild_tag = guild_tag[:23] + "..."
    draw.text((width - 45, 58), guild_tag, fill=(*accent_color, 255), font=_load_font(14, bold=True), anchor="ra")

    # Header separator line with glowing gradient
    draw.line([(45, 92), (width - 45, 92)], fill=(55, 65, 95, 255), width=1)
    draw.line([(45, 92), (320, 92)], fill=(*glow_color, 255), width=2)

    # 4. Avatar Portrait Area (Left Side)
    avatar_size = 170
    avatar_box = (45, 110, 45 + avatar_size, 110 + avatar_size)

    # Background frame behind avatar
    draw.rounded_rectangle(
        (avatar_box[0] - 5, avatar_box[1] - 5, avatar_box[2] + 5, avatar_box[3] + 5),
        radius=20,
        fill=(25, 29, 44, 255),
        outline=(*primary_color, 220),
        width=3,
    )

    avatar_img = None
    if avatar_bytes:
        try:
            raw_av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            raw_av = raw_av.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)

            # Circular / rounded mask for avatar
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle((0, 0, avatar_size, avatar_size), radius=16, fill=255)

            avatar_img = Image.new("RGBA", (avatar_size, avatar_size), (0, 0, 0, 0))
            avatar_img.paste(raw_av, (0, 0), mask)
        except Exception as e:
            logger.debug(f"Failed to process avatar bytes for card: {e}")
            avatar_img = None

    if avatar_img:
        base.paste(avatar_img, (avatar_box[0], avatar_box[1]), avatar_img)
    else:
        # Default placeholder avatar with initial
        draw.rounded_rectangle(avatar_box, radius=16, fill=(*primary_color, 120))
        initial = (data["display_name"][:1] or "?").upper()
        font_initial = _load_font(80, bold=True)
        draw.text(
            (avatar_box[0] + avatar_size // 2, avatar_box[1] + avatar_size // 2),
            initial,
            fill=(255, 255, 255, 255),
            font=font_initial,
            anchor="mm",
        )

    # Status Pill underneath Avatar (with dynamic high-contrast font color)
    status_y = avatar_box[3] + 14
    if data.get("is_alumni"):
        status_text = "GRADUATED ALUMNI"
    elif data.get("is_student"):
        status_text = "VERIFIED STUDENT"
    elif data.get("is_guest"):
        status_text = "APPROVED GUEST"
    else:
        status_text = "UNVERIFIED"

    status_bg = (*primary_color, 255) if data["is_verified"] else (60, 70, 90, 255)
    font_status = _load_font(13, bold=True)

    # Calculate luminance of background color to choose black or white text for maximum readability
    lum = 0.299 * status_bg[0] + 0.587 * status_bg[1] + 0.114 * status_bg[2]
    status_fg = (20, 20, 20, 255) if (lum > 145 and data["is_verified"]) else (255, 255, 255, 255)

    draw.rounded_rectangle((45, status_y, 45 + avatar_size, status_y + 36), radius=10, fill=status_bg)
    draw.text((45 + avatar_size // 2, status_y + 18), status_text, fill=status_fg, font=font_status, anchor="mm")

    # 5. User Information Section (Center/Right Grid)
    info_x = 245
    font_name = _load_font(30, bold=True)
    font_username = _load_font(16, bold=False)
    font_label = _load_font(13, bold=True)
    font_value = _load_font(18, bold=True)

    # Display Name
    name_str = data["display_name"]
    if len(name_str) > 22:
        name_str = name_str[:20] + "..."
    draw.text((info_x, 108), name_str, fill=(255, 255, 255, 255), font=font_name)

    # Username Tag
    draw.text((info_x, 148), f"@{data['username']}", fill=(155, 170, 200, 255), font=font_username)

    # Faculty Banner / Tag Box
    fac_name = data["faculty_name"]
    fac_full = data["faculty_full"]
    if len(fac_full) > 65:
        fac_full = fac_full[:62] + "..."

    box_y = 178
    font_fac_bold = _load_font(18, bold=True)
    font_fac_sub = _load_font(15, bold=False)
    fac_bbox = draw.textbbox((0, 0), fac_name, font=font_fac_bold)
    fac_w = fac_bbox[2] - fac_bbox[0]

    draw.rounded_rectangle(
        (info_x, box_y, width - 45, box_y + 46),
        radius=10,
        fill=(22, 26, 40, 255),
        outline=(*primary_color, 160),
        width=1,
    )
    draw.text((info_x + 14, box_y + 13), fac_name, fill=(*glow_color, 255), font=font_fac_bold)
    draw.text((info_x + 14 + fac_w + 12, box_y + 14), f"•  {fac_full}", fill=(215, 225, 245, 255), font=font_fac_sub)

    # Details Grid (Two columns)
    col1_x = info_x
    col2_x = info_x + 320
    row1_y = 242
    row2_y = 302

    # Row 1: Verified Since & Server Member Since
    draw.text((col1_x, row1_y), "STATUS ISSUED", fill=(145, 160, 190, 255), font=font_label)
    draw.text((col1_x, row1_y + 19), data["verified_at"], fill=(255, 255, 255, 255), font=font_value)

    draw.text((col2_x, row1_y), "JOINED SERVER", fill=(145, 160, 190, 255), font=font_label)
    draw.text((col2_x, row1_y + 19), data["joined_at"], fill=(255, 255, 255, 255), font=font_value)

    # Row 2: Security ID & Campus Affiliation
    draw.text((col1_x, row2_y), "SECURITY TOKEN", fill=(145, 160, 190, 255), font=font_label)
    draw.text((col1_x, row2_y + 19), data["hash_preview"], fill=(*accent_color, 255), font=_load_font(20, bold=True))

    draw.text((col2_x, row2_y), "CAMPUS COHORT", fill=(145, 160, 190, 255), font=font_label)
    cohort_str = data.get("cohort_str") or (
        "TARUMT Main Campus" if data["is_student"] else ("Verified Affiliate" if data["is_guest"] else "Public Guest")
    )
    if len(cohort_str) > 34:
        cohort_str = cohort_str[:32] + "..."
    draw.text((col2_x, row2_y + 19), cohort_str, fill=(255, 255, 255, 255), font=font_value)

    # 6. Badges Ribbon (Bottom Left to Center)
    badges_y = 378
    draw.text((45, badges_y), "ACHIEVEMENTS & BADGES", fill=(145, 160, 190, 255), font=font_label)

    bx = 45
    by = badges_y + 22
    font_b = _load_font(13, bold=True)
    chip_x = width - 170

    for badge in data["badges"]:
        t_bbox = draw.textbbox((0, 0), badge, font=font_b)
        t_width = t_bbox[2] - t_bbox[0]
        b_width = max(t_width + 20, 70)
        # Prevent overlapping the chip
        if bx + b_width > chip_x - 12:
            break
        draw.rounded_rectangle(
            (bx, by, bx + b_width, by + 34), radius=9, fill=(28, 34, 52, 255), outline=(60, 72, 105, 255), width=1
        )
        draw.text((bx + b_width // 2, by + 17), badge, fill=(235, 245, 255, 255), font=font_b, anchor="mm")
        bx += b_width + 8

    # 7. Holographic Chip & Barcode Simulation (Bottom Right)
    chip_y = 380

    # Gold Security Smart Chip
    draw.rounded_rectangle(
        (chip_x, chip_y, chip_x + 70, chip_y + 52),
        radius=6,
        fill=(218, 180, 60, 250),
        outline=(190, 150, 35, 255),
        width=1,
    )
    # Chip circuit patterns
    draw.line([(chip_x + 10, chip_y + 26), (chip_x + 60, chip_y + 26)], fill=(160, 125, 25, 255), width=2)
    draw.line([(chip_x + 35, chip_y + 8), (chip_x + 35, chip_y + 44)], fill=(160, 125, 25, 255), width=2)
    draw.rounded_rectangle(
        (chip_x + 24, chip_y + 17, chip_x + 46, chip_y + 35), radius=4, outline=(160, 125, 25, 255), width=1
    )

    # Barcode lines next to chip
    barcode_x = chip_x + 82
    for i in range(12):
        bx_line = barcode_x + i * 4
        bar_w = 2 if i % 3 == 0 else 1
        draw.line([(bx_line, chip_y + 4), (bx_line, chip_y + 48)], fill=(160, 175, 205, 220), width=bar_w)

    # Footer verification watermark
    draw.text(
        (width - 45, height - 32),
        "TARVeri Verified • Instant & Tamper-Proof",
        fill=(110, 125, 155, 255),
        font=_load_font(12, bold=False),
        anchor="ra",
    )

    # Output to BytesIO PNG
    buffer = io.BytesIO()
    base.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer
