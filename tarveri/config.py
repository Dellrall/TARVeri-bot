"""
Configuration management, constants, and cryptographic/formatting utilities.
"""

from __future__ import annotations

import calendar
import hashlib
import hmac
import logging
import os
import re
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from typing import Any, Final

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

# Faculty code mapping (index 3 of student ID) -> Role Name
FACULTY_ROLES: Final[dict[str, str]] = {
    "B": "FAFB",
    "K": "FCCI",
    "L": "FOAS",
    "J": "FSSH",
    "V": "FOBE",
    "P": "CPUS",
    "M": "FOCS",
    "G": "FOET",
}
FACULTY_ROLE_NAMES: Final[set[str]] = set(FACULTY_ROLES.values())

# Rich dynamic synonyms, expansions, and aliases for each faculty
FACULTY_ALIASES: Final[dict[str, list[str]]] = {
    "FAFB": [
        "Faculty of Accountancy, Finance and Business",
        "Faculty of Accountancy, Finance & Business",
        "Faculty of Accountancy",
        "Accountancy, Finance and Business",
        "Accountancy, Finance & Business",
        "Accountancy & Finance",
        "Accountancy",
        "Finance",
        "FAFB",
    ],
    "CPUS": [
        "Centre for Pre-University Studies",
        "Centre for Pre-U Studies",
        "Center for Pre-University Studies",
        "Pre-University Studies",
        "Pre-University",
        "Pre-U Studies",
        "Pre-U",
        "CPUS",
    ],
    "FOCS": [
        "Faculty of Computing and Information Technology",
        "Faculty of Computing & Information Technology",
        "Faculty of Computing",
        "Computing and Information Technology",
        "Computing & Information Technology",
        "Computing & IT",
        "Computing",
        "Computer Science",
        "Information Technology",
        "FOCS",
        "FCIT",
    ],
    "FCCI": [
        "Faculty of Communication and Creative Industries",
        "Faculty of Communication & Creative Industries",
        "Faculty of Communication",
        "Communication and Creative Industries",
        "Communication & Creative Industries",
        "Creative Industries",
        "Communication",
        "FCCI",
    ],
    "FOAS": [
        "Faculty of Applied Sciences",
        "Faculty of Applied Science",
        "Applied Sciences",
        "Applied Science",
        "FOAS",
        "FAS",
    ],
    "FOBE": [
        "Faculty of Built Environment",
        "Built Environment",
        "Architecture",
        "Surveying",
        "FOBE",
    ],
    "FSSH": [
        "Faculty of Social Science and Humanities",
        "Faculty of Social Science & Humanities",
        "Faculty of Social Science",
        "Social Science and Humanities",
        "Social Science & Humanities",
        "Social Science",
        "Humanities",
        "FSSH",
        "FSS",
    ],
    "FOET": [
        "Faculty of Engineering and Technology",
        "Faculty of Engineering & Technology",
        "Faculty of Engineering",
        "Engineering and Technology",
        "Engineering & Technology",
        "Engineering",
        "FOET",
        "FOE",
    ],
}

# Faculty SRC (Student Representative Council) roles
SRC_ROLES: Final[dict[str, str]] = {
    "FAFB": "FAFB SRC",
    "CPUS": "CPUS SRC",
    "FOCS": "FOCS SRC",
    "FCCI": "FCCI SRC",
    "FOAS": "FOAS SRC",
    "FOBE": "FOBE SRC",
    "FSSH": "FSSH SRC",
    "FOET": "FOET SRC",
}
SRC_ROLE_NAMES: Final[set[str]] = set(SRC_ROLES.values())

# Organizational, council, and functional role qualifiers that must NEVER be matched as general faculty roles
ROLE_QUALIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(src|council|committee|exco|reps?|representatives?|staff|admins?|leads?|mentors?|tutors?|lecturers?|societ(?:y|ies)|clubs?|presidents?|vp|secretar(?:y|ies)|treasurers?|bureaus?|alumni|seniors?|juniors?|sub[\s\-_]*committee)\b",
    re.IGNORECASE,
)

# Dynamic pattern matching for guest and visitor roles
GUEST_ROLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(guests?|visitors?|external|non[\s\-_]*students?)\b",
    re.IGNORECASE,
)

# Default role colors matching server design palette
FACULTY_COLORS: Final[dict[str, int]] = {
    "FAFB": 0x992D22,  # Dark Red (#992D22)
    "CPUS": 0x1F8673,  # Dark Teal (#1F8673)
    "FOCS": 0xF1C40F,  # Yellow / Gold (#F1C40F)
    "FCCI": 0x71368A,  # Dark Purple (#71368A)
    "FOAS": 0xE74C3C,  # Red / Coral Red (#E74C3C)
    "FOBE": 0x2ECC71,  # Green / Emerald (#2ECC71)
    "FSSH": 0x3498DB,  # Blue (#3498DB)
    "FOET": 0xBAE973,  # Lime Green (#BAE973)
}
GUEST_ROLE_COLOR: Final[int] = 0x2ECC71  # Green / Emerald (#2ECC71)

# Alumni role configurations
ALUMNI_ROLE_NAME: Final[str] = "TARUMT Alumni"
ALUMNI_ROLE_COLOR: Final[int] = 0xD4AF37  # Academic Gold (#D4AF37)
ALUMNI_ROLE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(alumni|graduates?|alumnus|alumna|graduate|graduated)\b",
    re.IGNORECASE,
)
ALUMNI_ALIASES: Final[list[str]] = [
    "TARUMT Alumni",
    "TAR UMT Alumni",
    "Alumni",
    "TARUC Alumni",
    "TAR UC Alumni",
    "TARC Alumni",
    "TAR College Alumni",
    "Alumni TARUMT",
    "TARUMT Graduate",
    "TARUMT Graduates",
    "TARUMT Graduated",
    "Graduated",
    "Graduate",
    "Graduates",
    "Alumnus",
    "Alumna",
    "Alumni Member",
    "Alumni Members",
]

# Branch campus code mapping (index 2 of student ID) -> Role Name
CAMPUS_ROLES: Final[dict[str, str]] = {
    "W": "KL Main Campus",
    "P": "Penang Branch",
    "A": "Perak Branch",
    "J": "Johor Branch",
    "C": "Pahang Branch",
    "K": "Pahang Branch",
    "S": "Sabah Branch",
}
CAMPUS_ROLE_NAMES: Final[set[str]] = set(CAMPUS_ROLES.values())

CAMPUS_ALIASES: Final[dict[str, list[str]]] = {
    "KL Main Campus": [
        "KL Main Campus",
        "KL Campus",
        "Kuala Lumpur Campus",
        "Kuala Lumpur Main Campus",
        "Main Campus",
        "KL",
        "Setapak Campus",
    ],
    "Penang Branch": [
        "Penang Branch",
        "Penang Campus",
        "Penang Branch Campus",
        "Pulau Pinang Campus",
        "Pulau Pinang Branch",
        "Penang",
    ],
    "Perak Branch": [
        "Perak Branch",
        "Perak Campus",
        "Perak Branch Campus",
        "Kampar Campus",
        "Kampar Branch",
        "Perak",
    ],
    "Johor Branch": [
        "Johor Branch",
        "Johor Campus",
        "Johor Branch Campus",
        "Segamat Campus",
        "Segamat Branch",
        "Johor",
    ],
    "Pahang Branch": [
        "Pahang Branch",
        "Pahang Campus",
        "Pahang Branch Campus",
        "Kuantan Campus",
        "Kuantan Branch",
        "Pahang",
    ],
    "Sabah Branch": [
        "Sabah Branch",
        "Sabah Campus",
        "Sabah Branch Campus",
        "Kota Kinabalu Campus",
        "Kota Kinabalu Branch",
        "Sabah",
    ],
}

CAMPUS_COLORS: Final[dict[str, int]] = {
    "KL Main Campus": 0x3498DB,  # Sky Blue (#3498DB)
    "Penang Branch": 0x1ABC9C,   # Turquoise (#1ABC9C)
    "Perak Branch": 0xE67E22,    # Orange (#E67E22)
    "Johor Branch": 0x9B59B6,    # Amethyst (#9B59B6)
    "Pahang Branch": 0x27AE60,   # Green (#27AE60)
    "Sabah Branch": 0xF39C12,    # Sun Yellow (#F39C12)
}

# Study level code mapping (index 4 of student ID) -> Role Name
STUDY_LEVEL_ROLES: Final[dict[str, str]] = {
    "D": "Diploma",
    "R": "Degree",
    "F": "Foundation",
    "P": "Postgraduate",
}
STUDY_LEVEL_ROLE_NAMES: Final[set[str]] = set(STUDY_LEVEL_ROLES.values())

STUDY_LEVEL_ALIASES: Final[dict[str, list[str]]] = {
    "Diploma": [
        "Diploma",
        "Diploma Student",
        "Diploma Students",
        "Dip",
    ],
    "Degree": [
        "Degree",
        "Degree Student",
        "Degree Students",
        "Bachelor",
        "Bachelor's Degree",
        "Bachelors Degree",
        "Undergraduate",
    ],
    "Foundation": [
        "Foundation",
        "Foundation Student",
        "Foundation Students",
        "Foundation Studies",
        "Foundation Programme",
        "Foundation Program",
        "Pre-U Student",
        "Pre-University Student",
    ],
    "Postgraduate": [
        "Postgraduate",
        "Postgrad",
        "Master",
        "Masters",
        "Master's",
        "PhD",
        "Doctorate",
    ],
}

STUDY_LEVEL_COLORS: Final[dict[str, int]] = {
    "Degree": 0x2980B9,        # Belize Blue (#2980B9)
    "Diploma": 0x16A085,       # Green Sea (#16A085)
    "Foundation": 0x8E44AD,    # Wisteria (#8E44AD)
    "Postgraduate": 0xD35400,  # Pumpkin (#D35400)
}


def resolve_faculty_role(faculty_code: str | None) -> str | None:
    """
    Robustly resolves a faculty role name (e.g. 'FOCS', 'FAFB') from any historical or modern faculty code:
    - Single-letter code: 'M' -> 'FOCS'
    - Full role name: 'FOCS' -> 'FOCS'
    - 2-letter combo (e.g. 'WM', 'PK'): extracts faculty char 'M' / 'K' -> 'FOCS' / 'FCCI'
    - 3-letter programme code (e.g. 'WMR', 'PKD'): extracts index 1 faculty char 'M' / 'K' -> 'FOCS' / 'FCCI'
    - Full alias lookup in FACULTY_ALIASES
    """
    if not faculty_code:
        return None
    code_clean = str(faculty_code).strip().upper()
    if code_clean in FACULTY_ROLES:
        return FACULTY_ROLES[code_clean]
    if code_clean in FACULTY_ROLE_NAMES:
        return code_clean
    if len(code_clean) == 2 and code_clean[1] in FACULTY_ROLES:
        return FACULTY_ROLES[code_clean[1]]
    if len(code_clean) == 3 and code_clean[1] in FACULTY_ROLES:
        return FACULTY_ROLES[code_clean[1]]
    for fac, aliases in FACULTY_ALIASES.items():
        if any(code_clean == a.upper() for a in aliases):
            return fac
    return None


def resolve_campus_role(campus_code: str | None) -> str:
    """
    Robustly resolves a branch campus role name (e.g. 'KL Main Campus', 'Penang Branch')
    from any campus code, prefix, or alias. Defaults to 'KL Main Campus'.
    """
    if not campus_code:
        return "KL Main Campus"
    code_clean = str(campus_code).strip().upper()
    if code_clean in CAMPUS_ROLES:
        return CAMPUS_ROLES[code_clean]
    if code_clean in CAMPUS_ROLE_NAMES:
        return code_clean
    if len(code_clean) in (2, 3) and code_clean[0] in CAMPUS_ROLES:
        return CAMPUS_ROLES[code_clean[0]]
    for campus_name, aliases in CAMPUS_ALIASES.items():
        if any(code_clean == a.upper() for a in aliases):
            return campus_name
    return "KL Main Campus"


def resolve_study_level_role(level_code: str | None) -> str:
    """
    Robustly resolves a study level role name (e.g. 'Degree', 'Diploma')
    from any level code or abbreviation. Defaults to 'Degree'.
    """
    if not level_code:
        return "Degree"
    code_clean = str(level_code).strip().upper()
    if code_clean in STUDY_LEVEL_ROLES:
        return STUDY_LEVEL_ROLES[code_clean]
    if code_clean in STUDY_LEVEL_ROLE_NAMES:
        return code_clean
    if len(code_clean) == 3 and code_clean[2] in STUDY_LEVEL_ROLES:
        return STUDY_LEVEL_ROLES[code_clean[2]]
    for lvl_name, aliases in STUDY_LEVEL_ALIASES.items():
        if any(code_clean == a.upper() for a in aliases):
            return lvl_name
    return "Degree"


# Pattern: 2 digits + 3 uppercase letters + 2 digits + 3 digits (e.g. 23WMD09867)
STUDENT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{2}[A-Z]{3}\d{2}\d{3}$")

# Pattern matching role inquiries or help queries from members
ROLE_HELP_KEYWORDS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b("
    r"help|bantuan|tolong|support|faq|"
    r"verif(?:y|ied|ication|ikasi)?|"
    r"roles?|faculty|faculty\s+role|"
    r"student(?:\s*id)?|matrik?|id\s+number|"
    r"guests?|referrals?|invite(?:\s*code)?|"
    r"tarveri"
    r")\b|"
    r"(?:how|where|macam\s+mana|camne|nak)\s+(?:to|do\s+i|can\s+i|nak)?\s*(?:get|join|verify|enter|claim|access)",
    re.IGNORECASE,
)


def get_configured_tz(tz_name: str | None = None) -> zoneinfo.ZoneInfo | timezone:
    """Resolves the configured timezone (defaults to Asia/Kuala_Lumpur or local system time)."""
    raw = (
        tz_name
        or os.getenv("TARVERI_TIMEZONE")
        or os.getenv("TIMEZONE")
        or os.getenv("TZ")
        or "Asia/Kuala_Lumpur"
    ).strip()
    if raw.lower() in ("auto", "local", "system", ""):
        return datetime.now().astimezone().tzinfo or UTC
    try:
        return zoneinfo.ZoneInfo(raw)
    except Exception:
        return datetime.now().astimezone().tzinfo or UTC


def now_formatted(tz_name: str | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Returns current timestamp string formatted in the configured timezone."""
    tz = get_configured_tz(tz_name)
    return datetime.now(tz).strftime(fmt)


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    id_hash_secret: str
    db_path: str = "tarveri.db"
    admin_role_name: str = "TARVeri Admin"
    logs_dir: str = "logs"
    log_file: str = "tarveri.log"
    log_max_bytes: int = 2_000_000
    log_backup_count: int = 5
    log_archive_days: int = 10
    enable_log_rotator: bool = True
    rate_limit_max_attempts: int = 5
    rate_limit_window_seconds: int = 600
    hoster_discord_id: int | None = None
    enable_update_checker: bool = True
    update_check_interval_hours: int = 24
    update_stream: str = "auto"
    help_channel_id: int | None = None
    welcome_channel_id: int | None = None
    timezone_name: str = "Asia/Kuala_Lumpur"
    backup_dir: str = "backups"
    max_backups: int = 10
    enable_outage_watchdog: bool = True
    outage_timeout_seconds: int = 300
    outage_probe_interval_seconds: int = 15
    outage_alert_grace_seconds: int = 20
    enable_graduation_watchdog: bool = True
    graduation_check_interval_hours: int = 24
    graduation_prompt_cooldown_days: int = 7
    enable_email_verification: bool = False
    enable_email_role_enforcement: bool = False
    email_allowed_domains: tuple[str, ...] = ("student.tarc.edu.my", "tarc.edu.my")
    email_encryption_key: str = ""
    smtp_host: str = "mail.smtp2go.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@muwa.work"
    smtp_from_name: str = "TARVeri Student Verification"
    smtp_use_tls: bool = True
    smtp_fallback_host: str = ""
    smtp_fallback_port: int = 587
    smtp_fallback_user: str = ""
    smtp_fallback_password: str = ""
    smtp_fallback_from_email: str = ""
    smtp_fallback_from_name: str = ""
    smtp_fallback_use_tls: bool = True
    email_otp_ttl_seconds: int = 600
    email_otp_max_attempts: int = 3
    email_otp_resend_cooldown_seconds: int = 60
    email_restrict_smtp_usage: bool = True
    sentry_dsn: str = ""
    circuit_breaker_fail_max: int = 3
    circuit_breaker_reset_timeout: int = 300
    max_storage_mb: int = 500
    enable_storage_guard: bool = True
    storage_check_interval_hours: int = 6
    mass_revocation_threshold: int = 5

    @property
    def restrict_smtp_usage(self) -> bool:
        return self.email_restrict_smtp_usage

    @property
    def database_path(self) -> str:
        return self.db_path

    @property
    def rate_limit_requests(self) -> int:
        return self.rate_limit_max_attempts

    @classmethod
    def from_env(cls, validate: bool = True) -> Settings:
        def _env_str(*keys: str, default: str = "") -> str:
            for key in keys:
                val = os.getenv(key)
                if val is not None and val.strip():
                    return val.strip()
            return default

        def _env_int(*keys: str, default: int = 0) -> int:
            for key in keys:
                val = os.getenv(key)
                if val is not None and val.strip():
                    cleaned = val.strip()
                    if cleaned.isdigit() or (cleaned.startswith("-") and cleaned[1:].isdigit()):
                        return int(cleaned)
            return default

        def _env_optional_int(*keys: str) -> int | None:
            for key in keys:
                val = os.getenv(key)
                if val is not None and val.strip():
                    cleaned = val.strip()
                    if cleaned.isdigit():
                        return int(cleaned)
            return None

        def _env_bool(*keys: str, default: bool = False) -> bool:
            for key in keys:
                val = os.getenv(key)
                if val is not None and val.strip():
                    return val.strip().lower() in ("true", "1", "yes", "t")
            return default

        bot_token = _env_str("TARVERI_BOT_TOKEN", "DISCORD_BOT_TOKEN", "BOT_TOKEN", "DISCORD_TOKEN")
        id_hash_secret = _env_str("TARVERI_ID_HASH_SECRET", "ID_HASH_SECRET", "HASH_SECRET")
        db_path = _env_str("TARVERI_DB_PATH", "DB_PATH", "DATABASE_PATH", default="tarveri.db")
        admin_role_name = _env_str("TARVERI_ADMIN_ROLE_NAME", "ADMIN_ROLE_NAME", "ADMIN_ROLE", default="TARVeri Admin")
        logs_dir = _env_str("TARVERI_LOGS_DIR", "LOGS_DIR", default="logs")
        log_file = _env_str("TARVERI_LOG_FILE", "LOG_FILE", default="tarveri.log")
        log_archive_days = _env_int("TARVERI_LOG_ARCHIVE_DAYS", "LOG_ARCHIVE_DAYS", default=10)
        enable_log_rotator = _env_bool("TARVERI_ENABLE_LOG_ROTATOR", "ENABLE_LOG_ROTATOR", default=True)
        hoster_discord_id = _env_optional_int("TARVERI_HOSTER_DISCORD_ID", "HOSTER_DISCORD_ID", "HOSTER_ID")

        enable_update_checker = _env_bool("TARVERI_ENABLE_UPDATE_CHECKER", "ENABLE_UPDATE_CHECKER", default=True)
        update_check_interval_hours = _env_int("TARVERI_UPDATE_CHECK_INTERVAL_HOURS", "UPDATE_CHECK_INTERVAL_HOURS", default=24)
        update_stream = _env_str("TARVERI_UPDATE_STREAM", "TARVERI_UPDATE_BRANCH", "UPDATE_STREAM", "UPDATE_BRANCH", default="auto")

        help_channel_id = _env_optional_int("TARVERI_HELP_CHANNEL_ID", "HELP_CHANNEL_ID")
        welcome_channel_id = _env_optional_int("TARVERI_WELCOME_CHANNEL_ID", "WELCOME_CHANNEL_ID")
        timezone_name = _env_str("TARVERI_TIMEZONE", "TIMEZONE", "TZ", default="Asia/Kuala_Lumpur")

        backup_dir = _env_str("TARVERI_BACKUP_DIR", "BACKUP_DIR", default="backups")
        max_backups = _env_int("TARVERI_MAX_BACKUPS", "MAX_BACKUPS", default=10)

        enable_outage_watchdog = _env_bool("TARVERI_ENABLE_OUTAGE_WATCHDOG", "ENABLE_OUTAGE_WATCHDOG", default=True)
        outage_timeout_seconds = _env_int("TARVERI_OUTAGE_TIMEOUT_SECONDS", "OUTAGE_TIMEOUT_SECONDS", default=300)
        outage_probe_interval_seconds = _env_int("TARVERI_OUTAGE_PROBE_INTERVAL_SECONDS", "OUTAGE_PROBE_INTERVAL_SECONDS", default=15)
        outage_alert_grace_seconds = _env_int("TARVERI_OUTAGE_ALERT_GRACE_SECONDS", "OUTAGE_ALERT_GRACE_SECONDS", default=20)

        enable_graduation_watchdog = _env_bool("TARVERI_ENABLE_GRADUATION_WATCHDOG", "ENABLE_GRADUATION_WATCHDOG", default=True)
        graduation_check_interval_hours = _env_int("TARVERI_GRADUATION_CHECK_INTERVAL_HOURS", "GRADUATION_CHECK_INTERVAL_HOURS", default=24)
        graduation_prompt_cooldown_days = _env_int("TARVERI_GRADUATION_PROMPT_COOLDOWN_DAYS", "GRADUATION_PROMPT_COOLDOWN_DAYS", default=7)

        enable_email_verification = _env_bool("TARVERI_EMAIL_VERIFICATION_ENABLED", "EMAIL_VERIFICATION_ENABLED", "ENABLE_EMAIL_VERIFICATION", default=False)
        enable_email_role_enforcement = _env_bool("TARVERI_ENABLE_EMAIL_ROLE_ENFORCEMENT", "ENABLE_EMAIL_ROLE_ENFORCEMENT", "TARVERI_EMAIL_ROLE_ENFORCEMENT", default=False)
        email_domains_raw = _env_str("TARVERI_EMAIL_ALLOWED_DOMAINS", "EMAIL_ALLOWED_DOMAINS", default="student.tarc.edu.my,tarc.edu.my")
        email_allowed_domains = tuple(
            d.strip().lower() for d in email_domains_raw.split(",") if d.strip()
        ) or ("student.tarc.edu.my", "tarc.edu.my")
        email_encryption_key = _env_str("TARVERI_EMAIL_ENCRYPTION_KEY", "EMAIL_ENCRYPTION_KEY")

        smtp_host = _env_str("TARVERI_SMTP_HOST", "SMTP_HOST", default="mail.smtp2go.com")
        smtp_port = _env_int("TARVERI_SMTP_PORT", "SMTP_PORT", default=587)
        smtp_user = _env_str("TARVERI_SMTP_USER", "SMTP_USER")
        smtp_password = _env_str("TARVERI_SMTP_PASSWORD", "SMTP_PASSWORD")
        smtp_from_email = _env_str("TARVERI_SMTP_FROM_EMAIL", "SMTP_FROM_EMAIL", default="noreply@muwa.work")
        smtp_from_name = _env_str("TARVERI_SMTP_FROM_NAME", "SMTP_FROM_NAME", default="TARVeri Student Verification").strip('"').strip("'")
        smtp_use_tls = _env_bool("TARVERI_SMTP_USE_TLS", "SMTP_USE_TLS", default=True)

        smtp_fallback_host = _env_str("TARVERI_SMTP_FALLBACK_HOST", "SMTP_FALLBACK_HOST")
        smtp_fallback_port = _env_int("TARVERI_SMTP_FALLBACK_PORT", "SMTP_FALLBACK_PORT", default=587)
        smtp_fallback_user = _env_str("TARVERI_SMTP_FALLBACK_USER", "SMTP_FALLBACK_USER")
        smtp_fallback_password = _env_str("TARVERI_SMTP_FALLBACK_PASSWORD", "SMTP_FALLBACK_PASSWORD")
        smtp_fallback_from_email = _env_str("TARVERI_SMTP_FALLBACK_FROM_EMAIL", "SMTP_FALLBACK_FROM_EMAIL")
        smtp_fallback_from_name = _env_str("TARVERI_SMTP_FALLBACK_FROM_NAME", "SMTP_FALLBACK_FROM_NAME").strip('"').strip("'")
        smtp_fallback_use_tls = _env_bool("TARVERI_SMTP_FALLBACK_USE_TLS", "SMTP_FALLBACK_USE_TLS", default=True)

        email_otp_ttl_seconds = _env_int("TARVERI_EMAIL_OTP_TTL_SECONDS", "EMAIL_OTP_TTL_SECONDS", default=600)
        email_otp_max_attempts = _env_int("TARVERI_EMAIL_OTP_MAX_ATTEMPTS", "EMAIL_OTP_MAX_ATTEMPTS", default=3)
        email_otp_resend_cooldown_seconds = _env_int("TARVERI_EMAIL_OTP_RESEND_COOLDOWN_SECONDS", "EMAIL_OTP_RESEND_COOLDOWN_SECONDS", default=60)
        email_restrict_smtp_usage = _env_bool("TARVERI_EMAIL_RESTRICT_SMTP_USAGE", "EMAIL_RESTRICT_SMTP_USAGE", "TARVERI_RESTRICT_SMTP_USAGE", "RESTRICT_SMTP_USAGE", default=True)

        sentry_dsn = _env_str("TARVERI_SENTRY_DSN", "SENTRY_DSN")
        circuit_breaker_fail_max = _env_int("TARVERI_CIRCUIT_BREAKER_FAIL_MAX", "CIRCUIT_BREAKER_FAIL_MAX", default=3)
        circuit_breaker_reset_timeout = _env_int("TARVERI_CIRCUIT_BREAKER_RESET_TIMEOUT", "CIRCUIT_BREAKER_RESET_TIMEOUT", default=300)
        max_storage_mb = _env_int("TARVERI_MAX_STORAGE_MB", "MAX_STORAGE_MB", default=500)
        enable_storage_guard = _env_bool("TARVERI_ENABLE_STORAGE_GUARD", "ENABLE_STORAGE_GUARD", default=True)
        storage_check_interval_hours = _env_int("TARVERI_STORAGE_CHECK_INTERVAL_HOURS", "STORAGE_CHECK_INTERVAL_HOURS", default=6)
        mass_revocation_threshold = _env_int("TARVERI_MASS_REVOCATION_THRESHOLD", "MASS_REVOCATION_THRESHOLD", default=5)

        if validate:
            if not bot_token:
                raise RuntimeError(
                    "TARVERI_BOT_TOKEN is not set. Put it in a .env file or the environment."
                )
            if not id_hash_secret:
                raise RuntimeError(
                    "TARVERI_ID_HASH_SECRET is not set. Generate one with: "
                    '`python -c "import secrets; print(secrets.token_hex(32))"`'
                )
            if enable_email_verification and not email_encryption_key:
                raise RuntimeError(
                    "TARVERI_EMAIL_ENCRYPTION_KEY is required when email verification is enabled. "
                    'Generate one with: python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
                )

        return cls(
            bot_token=bot_token,
            id_hash_secret=id_hash_secret,
            db_path=db_path,
            admin_role_name=admin_role_name,
            logs_dir=logs_dir,
            log_file=log_file,
            log_archive_days=log_archive_days,
            enable_log_rotator=enable_log_rotator,
            hoster_discord_id=hoster_discord_id,
            enable_update_checker=enable_update_checker,
            update_check_interval_hours=update_check_interval_hours,
            update_stream=update_stream,
            help_channel_id=help_channel_id,
            welcome_channel_id=welcome_channel_id,
            timezone_name=timezone_name,
            backup_dir=backup_dir,
            max_backups=max_backups,
            enable_outage_watchdog=enable_outage_watchdog,
            outage_timeout_seconds=outage_timeout_seconds,
            outage_probe_interval_seconds=outage_probe_interval_seconds,
            outage_alert_grace_seconds=outage_alert_grace_seconds,
            enable_graduation_watchdog=enable_graduation_watchdog,
            graduation_check_interval_hours=graduation_check_interval_hours,
            graduation_prompt_cooldown_days=graduation_prompt_cooldown_days,
            enable_email_verification=enable_email_verification,
            enable_email_role_enforcement=enable_email_role_enforcement,
            email_allowed_domains=email_allowed_domains,
            email_encryption_key=email_encryption_key,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            smtp_from_email=smtp_from_email,
            smtp_from_name=smtp_from_name,
            smtp_use_tls=smtp_use_tls,
            smtp_fallback_host=smtp_fallback_host,
            smtp_fallback_port=smtp_fallback_port,
            smtp_fallback_user=smtp_fallback_user,
            smtp_fallback_password=smtp_fallback_password,
            smtp_fallback_from_email=smtp_fallback_from_email,
            smtp_fallback_from_name=smtp_fallback_from_name,
            smtp_fallback_use_tls=smtp_fallback_use_tls,
            email_otp_ttl_seconds=email_otp_ttl_seconds,
            email_otp_max_attempts=email_otp_max_attempts,
            email_otp_resend_cooldown_seconds=email_otp_resend_cooldown_seconds,
            email_restrict_smtp_usage=email_restrict_smtp_usage,
            sentry_dsn=sentry_dsn,
            circuit_breaker_fail_max=circuit_breaker_fail_max,
            circuit_breaker_reset_timeout=circuit_breaker_reset_timeout,
            max_storage_mb=max_storage_mb,
            enable_storage_guard=enable_storage_guard,
            storage_check_interval_hours=storage_check_interval_hours,
            mass_revocation_threshold=mass_revocation_threshold,
        )


class TimezoneFormatter(logging.Formatter):
    """Custom logging formatter that renders timestamps in the local/configured timezone."""

    def __init__(
        self,
        fmt: str = "%(asctime)s | %(levelname)s | %(message)s",
        datefmt: str = "%Y-%m-%d %H:%M:%S",
        tz_name: str | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.tz = get_configured_tz(tz_name)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=self.tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


class DailyRotatingFileHandler(logging.Handler):
    """
    Timezone-aware daily rotating file handler that writes log records to date-separated
    files (e.g. logs/tarveri-YYYY-MM-DD.log) inside `logs_dir`.
    Automatically rolls over to a new daily log file when the date advances in the configured timezone.
    """

    def __init__(
        self,
        logs_dir: str = "logs",
        prefix: str = "tarveri",
        tz_name: str | None = None,
        encoding: str = "utf-8",
    ) -> None:
        super().__init__()
        self.logs_dir = logs_dir
        self.prefix = prefix
        self.tz = get_configured_tz(tz_name)
        self.encoding = encoding
        self.current_date_str: str | None = None
        self._stream: Any = None
        self._current_file_path: str | None = None
        os.makedirs(self.logs_dir, exist_ok=True)

    @property
    def current_file_path(self) -> str | None:
        return self._current_file_path

    def _get_date_str(self, record: logging.LogRecord) -> str:
        dt = datetime.fromtimestamp(record.created, tz=self.tz)
        return dt.strftime("%Y-%m-%d")

    def _open_stream(self, date_str: str) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
                self._stream.close()
            except OSError:
                pass
        self.current_date_str = date_str
        self._current_file_path = os.path.join(self.logs_dir, f"{self.prefix}-{date_str}.log")
        os.makedirs(self.logs_dir, exist_ok=True)
        self._stream = open(self._current_file_path, "a", encoding=self.encoding)

    def emit(self, record: logging.LogRecord) -> None:
        self.acquire()
        try:
            date_str = self._get_date_str(record)
            if self._stream is None or date_str != self.current_date_str:
                self._open_stream(date_str)
            msg = self.format(record)
            self._stream.write(msg + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)
        finally:
            self.release()

    def flush(self) -> None:
        self.acquire()
        try:
            if self._stream is not None and hasattr(self._stream, "flush"):
                self._stream.flush()
        finally:
            self.release()

    def close(self) -> None:
        self.acquire()
        try:
            if self._stream is not None:
                try:
                    self._stream.flush()
                    self._stream.close()
                except OSError:
                    pass
                self._stream = None
            super().close()
        finally:
            self.release()


def setup_logger(
    log_file: str = "tarveri.log",
    max_bytes: int = 2_000_000,
    backup_count: int = 5,
    tz_name: str | None = None,
    logs_dir: str = "logs",
) -> logging.Logger:
    """
    Sets up the application logger with daily file rotation in the logs folder
    and formatted console output.
    """
    logger = logging.getLogger("tarveri")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = TimezoneFormatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        tz_name=tz_name,
    )

    # Determine effective logs directory and log file prefix
    if os.path.dirname(log_file):
        effective_logs_dir = os.path.dirname(log_file)
        base = os.path.basename(log_file)
        prefix = base.rsplit(".", 1)[0] if "." in base else base
    else:
        effective_logs_dir = logs_dir
        base = log_file
        prefix = base.rsplit(".", 1)[0] if "." in base else base
        if not prefix:
            prefix = "tarveri"

    file_handler = DailyRotatingFileHandler(
        logs_dir=effective_logs_dir,
        prefix=prefix,
        tz_name=tz_name,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


def hash_student_id(student_id: str, secret: str) -> str:
    """Deterministic HMAC-SHA256 hash — lets us detect duplicate IDs without
    storing the raw ID at rest."""
    return hmac.new(
        secret.encode("utf-8"), student_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def mask_student_id(student_id: str) -> str:
    """For logs and displays: keep enough to be useful for support, not enough to be sensitive."""
    if len(student_id) >= 6:
        return f"{student_id[:2]}***{student_id[-3:]}"
    return "***"


def encrypt_email(email: str, encryption_key: str) -> str:
    """Encrypts an email address using AES-128-CBC + HMAC-SHA256 authenticated encryption (Fernet)."""
    if not encryption_key:
        raise ValueError("Encryption key is required to encrypt email.")
    f = Fernet(encryption_key.encode("utf-8") if isinstance(encryption_key, str) else encryption_key)
    return f.encrypt(email.strip().lower().encode("utf-8")).decode("utf-8")


def decrypt_email(ciphertext: str, encryption_key: str) -> str:
    """Decrypts an encrypted email address."""
    if not encryption_key:
        raise ValueError("Encryption key is required to decrypt email.")
    f = Fernet(encryption_key.encode("utf-8") if isinstance(encryption_key, str) else encryption_key)
    return f.decrypt(ciphertext.strip().encode("utf-8")).decode("utf-8")


def hash_email(email: str, secret: str) -> str:
    """Deterministic HMAC-SHA256 hash for blind indexing / fast duplicate checks at rest."""
    normalized = email.strip().lower()
    return hmac.new(
        secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def mask_email(email: str) -> str:
    """Masks an email for safe logs/displays (e.g., name-wm24@student.tarc.edu.my -> na***24@student.tarc.edu.my)."""
    if not email or "@" not in email:
        return "***"
    local, domain = email.strip().split("@", 1)
    if len(local) <= 2:
        masked_local = f"{local[:1]}***"
    elif len(local) <= 4:
        masked_local = f"{local[:1]}***{local[-1:]}"
    else:
        masked_local = f"{local[:2]}***{local[-2:]}"
    return f"{masked_local}@{domain}"


def is_valid_student_email(
    email: str,
    allowed_domains: tuple[str, ...] | list[str] = ("student.tarc.edu.my", "tarc.edu.my"),
) -> bool:
    """Validates email format and institutional domain."""
    if not email or "@" not in email:
        return False
    parts = email.strip().lower().rsplit("@", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    local_part, domain_part = parts
    # Check basic local part regex (alphanumeric, dot, underscore, dash, plus)
    if not re.match(r"^[a-zA-Z0-9._%+-]+$", local_part):
        return False
    return any(domain_part == d.lower() or domain_part.endswith(f".{d.lower()}") for d in allowed_domains)



@dataclass(frozen=True, slots=True)
class StudentIdInfo:
    """Detailed parsed components of a TARUMT student ID."""
    is_valid: bool
    student_id: str
    faculty_code: str | None
    faculty_role: str | None
    campus_code: str | None = None
    campus_role: str | None = None
    level_code: str | None = None
    level_role: str | None = None
    programme_code: str | None = None  # 3-letter branch/faculty/level prefix (e.g. 'WMR' for '24WMR12331')
    intake_year: str | None = None      # 2-digit intake year (e.g. '24')
    sequence: str | None = None         # 5-digit sequence (e.g. '12331')


def parse_student_id(raw_id: str) -> StudentIdInfo:
    """
    Parses a student ID into detailed components:
    - Intake year (digits 0..1, e.g. '24')
    - Branch Campus (character 2 -> CAMPUS_ROLES, e.g. 'W')
    - Faculty Code (character 3 -> FACULTY_ROLES, e.g. 'M')
    - Study Level (character 4 -> STUDY_LEVEL_ROLES, e.g. 'R')
    - Programme Code (characters 2..4, e.g. 'WMR')
    - Registration Sequence (digits 5..9, e.g. '12331')
    """
    normalized = raw_id.strip().upper().replace("-", "").replace(" ", "")
    if not STUDENT_ID_PATTERN.match(normalized):
        return StudentIdInfo(
            is_valid=False,
            student_id=normalized,
            faculty_code=None,
            faculty_role=None,
        )

    intake_year = normalized[:2] if len(normalized) >= 2 else None
    campus_code = normalized[2] if len(normalized) > 2 else None
    faculty_code = normalized[3] if len(normalized) > 3 else None
    level_code = normalized[4] if len(normalized) > 4 else None
    programme_code = normalized[2:5] if len(normalized) >= 5 else None
    sequence = normalized[5:] if len(normalized) > 5 else None

    campus_role = CAMPUS_ROLES.get(campus_code) if campus_code else None
    faculty_role = FACULTY_ROLES.get(faculty_code) if faculty_code else None
    level_role = STUDY_LEVEL_ROLES.get(level_code) if level_code else None

    if not faculty_role:
        return StudentIdInfo(
            is_valid=False,
            student_id=normalized,
            faculty_code=faculty_code,
            faculty_role=None,
            campus_code=campus_code,
            campus_role=campus_role,
            level_code=level_code,
            level_role=level_role,
            programme_code=programme_code,
            intake_year=intake_year,
            sequence=sequence,
        )

    return StudentIdInfo(
        is_valid=True,
        student_id=normalized,
        faculty_code=faculty_code,
        faculty_role=faculty_role,
        campus_code=campus_code,
        campus_role=campus_role,
        level_code=level_code,
        level_role=level_role,
        programme_code=programme_code,
        intake_year=intake_year,
        sequence=sequence,
    )


def is_smtp_bounce_error(error_message: str | Exception) -> tuple[bool, str | None, str]:
    """
    Analyzes an SMTP exception or error message to detect if it is a recipient hard or soft email bounce.
    Returns (is_bounce, bounce_code, bounce_reason).
    """
    err_str = str(error_message)
    # Ignore sender/relay rate limits or relay authentication errors
    if re.search(r"(sending\s+limit|rate\s+limit|relay\s+access\s+denied|authentication\s+failed|bad\s+credentials)", err_str, re.IGNORECASE):
        return False, None, err_str

    patterns = [
        (r"\b(5\.1\.1|5\.1\.0|5\.1\.2|5\.2\.1)\b", "550", "Mailbox not found or disabled"),
        (r"\b550\b.*(user|mailbox|no\s+such|unknown|not\s+found|disabled|rejected|invalid|exist)", "550", "Mailbox not found or disabled"),
        (r"\b(551|553|554)\b.*(recipient|user|mailbox|address|destination)", "554", "Recipient address rejected by mail server"),
        (r"\b(552|5\.2\.2)\b", "552", "Recipient mailbox full or quota exceeded"),
        (r"(user\s+unknown|mailbox\s+unavailable|mailbox\s+not\s+found|recipient\s+rejected|no\s+such\s+user|address\s+rejected|invalid\s+recipient|undeliverable\s+address)", "550", "Recipient address undeliverable / user unknown"),
    ]
    for pat, code, reason in patterns:
        if re.search(pat, err_str, re.IGNORECASE):
            return True, code, reason
    return False, None, err_str



def validate_student_id(raw_id: str) -> tuple[bool, str, str | None, str | None]:
    """
    Validates and parses a student ID.
    Returns (is_valid, normalized_id, faculty_code, faculty_role_name) for 100% backwards compatibility.
    """
    info = parse_student_id(raw_id)
    return info.is_valid, info.student_id, info.faculty_code, info.faculty_role


def parse_card_expiry_date(raw_date: str | None) -> str | None:
    """
    Parses and normalizes student card expiry date strings into ISO format (YYYY-MM-DD).
    Supports formats:
    - DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY (e.g. '06/07/2026' -> '2026-07-06')
    - DD/MM/YY, DD-MM-YY (e.g. '06/07/26' -> '2026-07-06')
    - MM/YY (e.g. '10/26' -> '2026-10-31')
    - MM/YYYY (e.g. '10/2026' -> '2026-10-31')
    - MM-YY / MM-YYYY
    - YYYY-MM (e.g. '2026-10' -> '2026-10-31')
    - YYYY-MM-DD (e.g. '2026-10-31')
    - DD Month Year (e.g. '15 OCT 2026' or '15 OCTOBER 2026')
    - Month Year (e.g. 'OCT 2026' or 'October 2026')
    """
    if not raw_date:
        return None

    cleaned = raw_date.strip().upper()
    if not cleaned:
        return None

    month_names = {
        "JAN": 1, "JANUARY": 1,
        "FEB": 2, "FEBRUARY": 2,
        "MAR": 3, "MARCH": 3,
        "APR": 4, "APRIL": 4,
        "MAY": 5,
        "JUN": 6, "JUNE": 6,
        "JUL": 7, "JULY": 7,
        "AUG": 8, "AUGUST": 8,
        "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
        "OCT": 10, "OCTOBER": 10,
        "NOV": 11, "NOVEMBER": 11,
        "DEC": 12, "DECEMBER": 12,
    }

    max_year = datetime.now(get_configured_tz()).year + 100

    # 1. Check ISO full date YYYY-MM-DD or YYYY/MM/DD
    iso_full_match = re.match(r"^(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})$", cleaned)
    if iso_full_match:
        try:
            year = int(iso_full_match.group(1))
            month = int(iso_full_match.group(2))
            day = int(iso_full_match.group(3))
            if 1969 <= year <= max_year and 1 <= month <= 12:
                max_days = calendar.monthrange(year, month)[1]
                if 1 <= day <= max_days:
                    return f"{year:04d}-{month:02d}-{day:02d}"
        except Exception:
            return None

    # 2. Check 3-part date: DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY (or DD/MM/YY)
    three_part_match = re.match(r"^(\d{1,2})[-/\.](\d{1,2})[-/\.](\d{2}|\d{4})$", cleaned)
    if three_part_match:
        try:
            p1 = int(three_part_match.group(1))
            p2 = int(three_part_match.group(2))
            raw_year = int(three_part_match.group(3))
            year = (2000 + raw_year if raw_year < 70 else 1900 + raw_year) if raw_year < 100 else raw_year
            if 1969 <= year <= max_year:
                # Primary check: Standard DD/MM/YYYY
                if 1 <= p2 <= 12 and 1 <= p1 <= 31:
                    max_days = calendar.monthrange(year, p2)[1]
                    if 1 <= p1 <= max_days:
                        return f"{year:04d}-{p2:02d}-{p1:02d}"
                # Secondary check: MM/DD/YYYY fallback
                if 1 <= p1 <= 12 and 1 <= p2 <= 31:
                    max_days = calendar.monthrange(year, p1)[1]
                    if 1 <= p2 <= max_days:
                        return f"{year:04d}-{p1:02d}-{p2:02d}"
        except Exception:
            return None

    # 3. Check YYYY-MM
    iso_year_month = re.match(r"^(\d{4})[-/](\d{1,2})$", cleaned)
    if iso_year_month:
        try:
            year = int(iso_year_month.group(1))
            month = int(iso_year_month.group(2))
            if 1 <= month <= 12 and 1969 <= year <= max_year:
                last_day = calendar.monthrange(year, month)[1]
                return f"{year:04d}-{month:02d}-{last_day:02d}"
        except Exception:
            return None

    # 4. Check MM/YY or MM/YYYY (or with hyphens/dots)
    m_y_match = re.match(r"^(\d{1,2})[-/\.](\d{2}|\d{4})$", cleaned)
    if m_y_match:
        try:
            month = int(m_y_match.group(1))
            raw_year = int(m_y_match.group(2))
            year = (2000 + raw_year if raw_year < 70 else 1900 + raw_year) if raw_year < 100 else raw_year
            if 1 <= month <= 12 and 1969 <= year <= max_year:
                last_day = calendar.monthrange(year, month)[1]
                return f"{year:04d}-{month:02d}-{last_day:02d}"
        except Exception:
            return None

    # 5. Check Day Month Name Year (e.g. '15 OCT 2026', '15 OCTOBER 2026')
    day_month_text_match = re.match(r"^(\d{1,2})\s+([A-Z]{3,9})\s+(\d{2}|\d{4})$", cleaned)
    if day_month_text_match:
        try:
            day = int(day_month_text_match.group(1))
            m_str = day_month_text_match.group(2)
            raw_year = int(day_month_text_match.group(3))
            month = month_names.get(m_str)
            year = (2000 + raw_year if raw_year < 70 else 1900 + raw_year) if raw_year < 100 else raw_year
            if month and 1969 <= year <= max_year:
                max_days = calendar.monthrange(year, month)[1]
                if 1 <= day <= max_days:
                    return f"{year:04d}-{month:02d}-{day:02d}"
        except Exception:
            return None

    # 6. Check Month Name Year (e.g. 'OCT 2026', 'OCTOBER 26')
    month_text_match = re.match(r"^([A-Z]{3,9})\s+(\d{2}|\d{4})$", cleaned)
    if month_text_match:
        try:
            m_str = month_text_match.group(1)
            raw_year = int(month_text_match.group(2))
            month = month_names.get(m_str)
            year = (2000 + raw_year if raw_year < 70 else 1900 + raw_year) if raw_year < 100 else raw_year
            if month and 1969 <= year <= max_year:
                last_day = calendar.monthrange(year, month)[1]
                return f"{year:04d}-{month:02d}-{last_day:02d}"
        except Exception:
            return None

    return None


def format_card_expiry_display(iso_date: str | None) -> str | None:
    """Formats an ISO date (YYYY-MM-DD) as 'MM/YY' for card rendering."""
    if not iso_date:
        return None
    try:
        parts = iso_date.split("-")
        if len(parts) >= 2:
            year_short = parts[0][-2:]
            month = parts[1].zfill(2)
            return f"{month}/{year_short}"
    except (IndexError, ValueError):
        return None
    return None


def is_expiry_date_anomalous(
    iso_date: str | None,
    student_id: str | None = None,
    threshold_years: int = 8,
) -> tuple[bool, str | None]:
    """
    Detects whether a parsed card expiry date is anomalous (e.g. > threshold_years in the past/future
    or prior to the student's intake year, often caused by ambiguous inputs like '06/07').

    Returns:
        (is_anomalous, explanation_reason)
    """
    if not iso_date:
        return False, None

    try:
        parts = iso_date.split("-")
        if len(parts) < 3:
            return False, None
        expiry_year = int(parts[0])
    except Exception:
        return False, None

    current_year = datetime.now(get_configured_tz()).year

    # 1. Check relative to intake year if student_id is provided
    if student_id and len(student_id) >= 2 and student_id[:2].isdigit():
        raw_yy = int(student_id[:2])
        intake_year = 2000 + raw_yy if raw_yy < 70 else 1900 + raw_yy
        if expiry_year < intake_year - 1:
            diff = intake_year - expiry_year
            return (
                True,
                f"The entered expiry year ({expiry_year}) is {diff} year(s) before your intake year ({intake_year}). "
                f"If you entered Day/Month (e.g. '06/07' for 6th July), it was interpreted as Month/Year (June 2007).",
            )
        if expiry_year > intake_year + threshold_years:
            diff = expiry_year - intake_year
            return (
                True,
                f"The entered expiry year ({expiry_year}) is {diff} years after your intake year ({intake_year}), "
                f"which exceeds the standard {threshold_years}-year study threshold.",
            )

    # 2. Check relative to current dynamic year
    if expiry_year < current_year - threshold_years:
        diff = current_year - expiry_year
        return (
            True,
            f"The entered expiry year ({expiry_year}) is {diff} years in the past (current year: {current_year}). "
            f"If you entered Day/Month (e.g. '06/07' for 6th July), it was interpreted as Month/Year (June 2007).",
        )
    if expiry_year > current_year + threshold_years:
        diff = expiry_year - current_year
        return (
            True,
            f"The entered expiry year ({expiry_year}) is {diff} years in the future (current year: {current_year}), "
            f"which exceeds the standard {threshold_years}-year threshold.",
        )

    return False, None


def estimate_student_card_expiry(student_id: str | None, level_code: str | None = None) -> str | None:
    """
    Intelligently estimates student card expiry date from the Student ID intake year and study level.
    Zero-effort automated fallback when the student does not provide an explicit card expiry date.
    Examples:
    - 23WMD09867 (Diploma, 2 yrs): Intake 2023 -> 2025-10-31
    - 24WMR12345 (Degree, 3 yrs): Intake 2024 -> 2027-10-31
    - 25WMF01234 (Foundation, 1 yr): Intake 2025 -> 2026-05-31
    - 23WMP00111 (Postgrad, 2 yrs): Intake 2023 -> 2025-10-31
    """
    if not student_id or len(student_id) < 5:
        return None
    raw_yy = student_id[:2]
    if not raw_yy.isdigit():
        return None
    intake_yy = int(raw_yy)
    intake_year = 2000 + intake_yy if intake_yy < 70 else 1900 + intake_yy

    lvl = (level_code or (student_id[4] if len(student_id) > 4 else "R")).upper()
    if lvl == "F":  # Foundation (1 year)
        return f"{intake_year + 1:04d}-05-31"
    elif lvl == "D":  # Diploma (2 years)
        return f"{intake_year + 2:04d}-10-31"
    elif lvl == "R":  # Degree (3 years typical)
        return f"{intake_year + 3:04d}-10-31"
    elif lvl == "P":  # Postgraduate (2 years typical)
        return f"{intake_year + 2:04d}-10-31"
    else:
        # Default 3 years
        return f"{intake_year + 3:04d}-10-31"





