"""
TARVeri — Discord student verification bot for TARUMT.
"""

from __future__ import annotations

from tarveri.bot import TARVeriBot, run_bot
from tarveri.config import (
    CAMPUS_ALIASES,
    CAMPUS_COLORS,
    CAMPUS_ROLE_NAMES,
    CAMPUS_ROLES,
    FACULTY_ALIASES,
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    GUEST_ROLE_PATTERN,
    ROLE_QUALIFIER_PATTERN,
    SRC_ROLE_NAMES,
    SRC_ROLES,
    STUDY_LEVEL_ALIASES,
    STUDY_LEVEL_COLORS,
    STUDY_LEVEL_ROLE_NAMES,
    STUDY_LEVEL_ROLES,
    DailyRotatingFileHandler,
    Settings,
    StudentIdInfo,
    hash_student_id,
    mask_student_id,
    parse_student_id,
    setup_logger,
    validate_student_id,
)
from tarveri.database import Database, list_backups, rotate_backups
from tarveri.rate_limiter import RateLimiter
from tarveri.services.guest_service import GuestService
from tarveri.services.log_service import (
    LogRotationService,
    archive_old_logs,
    get_10day_period,
    list_daily_logs,
    list_log_archives,
)
from tarveri.services.outage_service import OutageService
from tarveri.services.verification_service import VerificationService

__version__ = "2.5.0"

__all__ = [
    "CAMPUS_ALIASES",
    "CAMPUS_COLORS",
    "CAMPUS_ROLES",
    "CAMPUS_ROLE_NAMES",
    "FACULTY_ALIASES",
    "FACULTY_ROLES",
    "FACULTY_ROLE_NAMES",
    "GUEST_ROLE_PATTERN",
    "ROLE_QUALIFIER_PATTERN",
    "SRC_ROLES",
    "SRC_ROLE_NAMES",
    "STUDY_LEVEL_ALIASES",
    "STUDY_LEVEL_COLORS",
    "STUDY_LEVEL_ROLES",
    "STUDY_LEVEL_ROLE_NAMES",
    "DailyRotatingFileHandler",
    "Database",
    "GuestService",
    "LogRotationService",
    "OutageService",
    "RateLimiter",
    "Settings",
    "StudentIdInfo",
    "TARVeriBot",
    "VerificationService",
    "__version__",
    "archive_old_logs",
    "get_10day_period",
    "hash_student_id",
    "list_backups",
    "list_daily_logs",
    "list_log_archives",
    "mask_student_id",
    "parse_student_id",
    "rotate_backups",
    "run_bot",
    "setup_logger",
    "validate_student_id",
]
