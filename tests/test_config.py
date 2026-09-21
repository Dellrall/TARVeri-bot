from tarveri.config import (
    hash_student_id,
    mask_student_id,
    parse_student_id,
    validate_student_id,
)


def test_hash_student_id():
    secret = "test_secret_key_12345"
    student_id = "23WMD09867"
    hash1 = hash_student_id(student_id, secret)
    hash2 = hash_student_id(student_id, secret)
    assert hash1 == hash2
    assert len(hash1) == 64

    # Different secret or ID produces different hash
    assert hash_student_id("23WMD09868", secret) != hash1
    assert hash_student_id(student_id, "different_secret") != hash1


def test_mask_student_id():
    assert mask_student_id("23WMD09867") == "23***867"
    assert mask_student_id("123456") == "12***456"
    assert mask_student_id("123") == "***"


def test_validate_student_id_valid():
    # Valid FOCS (M)
    is_valid, norm_id, f_code, role = validate_student_id("  23wmd09867  ")
    assert is_valid is True
    assert norm_id == "23WMD09867"
    assert f_code == "M"
    assert role == "FOCS"

    # Valid FOET (G)
    is_valid, norm_id, f_code, role = validate_student_id("22WGD12345")
    assert is_valid is True
    assert norm_id == "22WGD12345"
    assert f_code == "G"
    assert role == "FOET"

    # Valid CPUS (P)
    is_valid, norm_id, f_code, role = validate_student_id("24WPF00001")
    assert is_valid is True
    assert norm_id == "24WPF00001"
    assert f_code == "P"
    assert role == "CPUS"


def test_parse_student_id_branches_and_levels():
    # Penang Campus (P), FOCS (M), Diploma (D)
    info_penang = parse_student_id("23PMD01234")
    assert info_penang.is_valid is True
    assert info_penang.student_id == "23PMD01234"
    assert info_penang.campus_code == "P"
    assert info_penang.campus_role == "Penang Branch"
    assert info_penang.faculty_code == "M"
    assert info_penang.faculty_role == "FOCS"
    assert info_penang.level_code == "D"
    assert info_penang.level_role == "Diploma"

    # Perak Campus (A), FAFB (B), Degree (R)
    info_perak = parse_student_id("22ABR05678")
    assert info_perak.is_valid is True
    assert info_perak.campus_code == "A"
    assert info_perak.campus_role == "Perak Branch"
    assert info_perak.faculty_code == "B"
    assert info_perak.faculty_role == "FAFB"
    assert info_perak.level_code == "R"
    assert info_perak.level_role == "Degree"

    # Johor Campus (J), FOAS (L), Foundation (F)
    info_johor = parse_student_id("24JLF99999")
    assert info_johor.is_valid is True
    assert info_johor.campus_code == "J"
    assert info_johor.campus_role == "Johor Branch"
    assert info_johor.faculty_code == "L"
    assert info_johor.faculty_role == "FOAS"
    assert info_johor.level_code == "F"
    assert info_johor.level_role == "Foundation"

    # Sabah Campus (S), FSSH (J), Postgraduate (P)
    info_sabah = parse_student_id("21SJP11111")
    assert info_sabah.is_valid is True
    assert info_sabah.campus_code == "S"
    assert info_sabah.campus_role == "Sabah Branch"
    assert info_sabah.faculty_code == "J"
    assert info_sabah.faculty_role == "FSSH"
    assert info_sabah.level_code == "P"
    assert info_sabah.level_role == "Postgraduate"

    # KL Main Campus (W), FOBE (V), Degree (R)
    info_kl = parse_student_id("23WVR22222")
    assert info_kl.is_valid is True
    assert info_kl.campus_code == "W"
    assert info_kl.campus_role == "KL Main Campus"
    assert info_kl.faculty_code == "V"
    assert info_kl.faculty_role == "FOBE"
    assert info_kl.level_code == "R"
    assert info_kl.level_role == "Degree"


def test_validate_student_id_invalid():
    # Invalid length/format
    is_valid, _, _, _ = validate_student_id("invalid_id")
    assert is_valid is False

    # Unknown faculty code
    is_valid, _, f_code, role = validate_student_id("23WZD09867")
    assert is_valid is False
    assert f_code == "Z"
    assert role is None


def test_settings_from_env(monkeypatch):
    from tarveri.config import Settings
    monkeypatch.setenv("TARVERI_BOT_TOKEN", "mock_token")
    monkeypatch.setenv("TARVERI_ID_HASH_SECRET", "mock_secret")
    monkeypatch.setenv("TARVERI_UPDATE_STREAM", "refactor/modular-optimization")
    monkeypatch.setenv("TARVERI_HELP_CHANNEL_ID", "1122334455")
    monkeypatch.setenv("TARVERI_WELCOME_CHANNEL_ID", "6677889900")
    monkeypatch.setenv("TARVERI_LOGS_DIR", "var_logs")
    monkeypatch.setenv("TARVERI_LOG_ARCHIVE_DAYS", "14")
    monkeypatch.setenv("TARVERI_ENABLE_LOG_ROTATOR", "true")

    settings = Settings.from_env()
    assert settings.bot_token == "mock_token"
    assert settings.id_hash_secret == "mock_secret"
    assert settings.update_stream == "refactor/modular-optimization"
    assert settings.help_channel_id == 1122334455
    assert settings.welcome_channel_id == 6677889900
    assert settings.logs_dir == "var_logs"
    assert settings.log_archive_days == 14
    assert settings.enable_log_rotator is True


def test_settings_legacy_env_fallbacks(monkeypatch):
    from tarveri.config import Settings
    # Clear any TARVERI_* vars
    monkeypatch.delenv("TARVERI_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TARVERI_ID_HASH_SECRET", raising=False)
    monkeypatch.delenv("TARVERI_DB_PATH", raising=False)
    monkeypatch.delenv("TARVERI_ADMIN_ROLE_NAME", raising=False)

    # Set legacy variable names
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "legacy_bot_token")
    monkeypatch.setenv("HASH_SECRET", "legacy_hash_secret")
    monkeypatch.setenv("DB_PATH", "legacy_tarveri.db")
    monkeypatch.setenv("ADMIN_ROLE", "Legacy Admin")
    monkeypatch.setenv("TIMEZONE", "Asia/Kuala_Lumpur")

    settings = Settings.from_env()
    assert settings.bot_token == "legacy_bot_token"
    assert settings.id_hash_secret == "legacy_hash_secret"
    assert settings.db_path == "legacy_tarveri.db"
    assert settings.admin_role_name == "Legacy Admin"
    assert settings.timezone_name == "Asia/Kuala_Lumpur"



def test_role_help_keywords_pattern():
    from tarveri.config import ROLE_HELP_KEYWORDS_PATTERN

    # Matching cases
    assert ROLE_HELP_KEYWORDS_PATTERN.search("How to get role?") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("how do i get a role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("Where to verify") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("how to verify?") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("I need role please") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("can you give role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("i have no role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("claim role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("faculty role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("what is my role") is not None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("roles") is not None

    # Non-matching cases
    assert ROLE_HELP_KEYWORDS_PATTERN.search("hello everyone") is None
    assert ROLE_HELP_KEYWORDS_PATTERN.search("good morning") is None


def test_timezone_configuration_and_formatter():
    import logging
    import zoneinfo

    from tarveri.config import TimezoneFormatter, get_configured_tz, now_formatted

    # Default timezone is Asia/Kuala_Lumpur
    tz = get_configured_tz("Asia/Kuala_Lumpur")
    assert isinstance(tz, zoneinfo.ZoneInfo)
    assert tz.key == "Asia/Kuala_Lumpur"

    # now_formatted returns a string formatted properly
    formatted = now_formatted(tz_name="Asia/Kuala_Lumpur")
    assert len(formatted) == 19
    assert formatted[4] == "-" and formatted[7] == "-" and formatted[10] == " "

    # TimezoneFormatter converts log record timestamp accurately
    formatter = TimezoneFormatter(tz_name="Asia/Kuala_Lumpur")
    record = logging.LogRecord(
        name="tarveri_test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="Test message",
        args=(),
        exc_info=None,
    )
    # Fixed epoch timestamp: 1700000000 -> 2023-11-14 22:13:20 UTC -> 2023-11-15 06:13:20 in UTC+8
    record.created = 1700000000.0
    log_time = formatter.formatTime(record)
    assert log_time == "2023-11-15 06:13:20"


def test_settings_validation_missing_tokens(monkeypatch):
    import pytest

    from tarveri.config import Settings

    monkeypatch.delenv("TARVERI_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    monkeypatch.setenv("TARVERI_ID_HASH_SECRET", "some_secret")

    with pytest.raises(RuntimeError, match="TARVERI_BOT_TOKEN is not set"):
        Settings.from_env(validate=True)

    monkeypatch.setenv("TARVERI_BOT_TOKEN", "some_token")
    monkeypatch.delenv("TARVERI_ID_HASH_SECRET", raising=False)
    monkeypatch.delenv("ID_HASH_SECRET", raising=False)
    monkeypatch.delenv("HASH_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="TARVERI_ID_HASH_SECRET is not set"):
        Settings.from_env(validate=True)


def test_get_configured_tz_fallback():
    from tarveri.config import get_configured_tz

    # Invalid timezone string falls back safely
    tz_invalid = get_configured_tz("NonExistent/Timezone_123")
    assert tz_invalid is not None

    # Auto / local / system strings resolve cleanly
    tz_auto = get_configured_tz("auto")
    assert tz_auto is not None
    tz_system = get_configured_tz("system")
    assert tz_system is not None


def test_settings_sentry_and_circuit_breaker(monkeypatch):
    from tarveri.config import Settings

    monkeypatch.setenv("TARVERI_BOT_TOKEN", "mock_bot_token")
    monkeypatch.setenv("TARVERI_ID_HASH_SECRET", "mock_secret")
    monkeypatch.setenv("TARVERI_SENTRY_DSN", "https://public@sentry.example.com/1")
    monkeypatch.setenv("TARVERI_CIRCUIT_BREAKER_FAIL_MAX", "5")
    monkeypatch.setenv("TARVERI_CIRCUIT_BREAKER_RESET_TIMEOUT", "600")

    settings = Settings.from_env(validate=True)
    assert settings.sentry_dsn == "https://public@sentry.example.com/1"
    assert settings.circuit_breaker_fail_max == 5
    assert settings.circuit_breaker_reset_timeout == 600


def test_role_resolvers_and_bounce_patterns():
    from tarveri.config import (
        is_smtp_bounce_error,
        resolve_campus_role,
        resolve_faculty_role,
        resolve_study_level_role,
    )

    # Faculty resolution
    assert resolve_faculty_role("M") == "FOCS"
    assert resolve_faculty_role("FOCS") == "FOCS"
    assert resolve_faculty_role("WM") == "FOCS"
    assert resolve_faculty_role("WMR") == "FOCS"
    assert resolve_faculty_role("PK") == "FCCI"
    assert resolve_faculty_role("PB") == "FAFB"
    assert resolve_faculty_role("WP") == "CPUS"
    assert resolve_faculty_role("UNKNOWN") is None

    # Campus resolution
    assert resolve_campus_role("W") == "KL Main Campus"
    assert resolve_campus_role("P") == "Penang Branch"
    assert resolve_campus_role("WM") == "KL Main Campus"
    assert resolve_campus_role("WMR") == "KL Main Campus"
    assert resolve_campus_role("KL Main Campus") == "KL Main Campus"

    # Study Level resolution
    assert resolve_study_level_role("R") == "Degree"
    assert resolve_study_level_role("D") == "Diploma"
    assert resolve_study_level_role("WMR") == "Degree"
    assert resolve_study_level_role("WMD") == "Diploma"
    assert resolve_study_level_role("Degree") == "Degree"

    # SMTP bounce matching
    is_bounce, code, _ = is_smtp_bounce_error("550 5.1.1 User unknown")
    assert is_bounce is True
    assert code == "550"

    is_bounce2, code2, _ = is_smtp_bounce_error("554 5.7.1 Recipient address rejected")
    assert is_bounce2 is True

    # Ignored sending quota errors
    is_bounce3, _, _ = is_smtp_bounce_error("550 Daily sending limit exceeded")
    assert is_bounce3 is False





