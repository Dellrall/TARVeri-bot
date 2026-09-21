# 📋 TARVeri Changelog

All notable changes to the **TARVeri** Discord Student & Guest Verification Bot are documented in this file.

## [v2.6.0] — 2026-09-21 (Current)
### 🚫 Multi-Vector Per-Server Blacklist System
* **Tri-Vector Blind Index Blacklisting**:
  - Implemented per-server blacklist storage (`guild_blacklists`) supporting 3 independent dimensions:
    - `USER`: Discord User Snowflake ID.
    - `STUDENT_ID`: Blind HMAC-SHA256 hashed Student ID (with masked display `23***34`).
    - `EMAIL`: Blind HMAC-SHA256 hashed institutional email (with masked display `s***@student...`).
  - Zero plaintext exposure of student PII in blacklist tables.
* **Instant Auto-Revocation & Verification Gating**:
  - Blacklisting any target immediately strips all faculty, campus, study level, alumni, and guest roles from the affected member in that guild.
  - Verification attempts (`/verify`) are blocked immediately with `BLACKLIST_ATTEMPT_BLOCKED` logging and reason feedback.
  - Prevents blacklisted users from creating referral codes or opening guest review tickets; referral codes from blacklisted referrers are blocked from redemption.
* **Self-Healing & Join Guard**:
  - `on_member_join` and `reconcile_verified_members` automatically ignore blacklisted users and strip stray roles during self-healing cycles.
  - Strict per-guild isolation: blacklist in Guild A never leaks into or impacts verification status in Guild B.
* **Admin Slash Commands (`/admin blacklist`)**:
  - Added `/admin blacklist user`, `/admin blacklist student_id`, `/admin blacklist email`, `/admin blacklist remove`, `/admin blacklist list`, `/admin blacklist clear`.

### 🛡️ Tiered Trust & Cross-Server Synchronization
* **Tier 1 (Fast Verified) vs Tier 2 (Email-Attested)**:
  - Servers can configure email verification requirements independently (`/admin email_verification <True|False>`).
  - Tier 1 students (Student ID verified without email OTP) are auto-assigned roles in Opt-Out servers, while Opt-In servers hold roles in escrow until institutional email OTP is completed.
  - Upgrading to Tier 2 in any Opt-In server automatically unlocks roles across all mutual Opt-In servers.
* **Universal Cross-Server Unverification (`/admin unverify`)**:
  - Unlinks student verification from SQLite, clears rate limiters, and strips faculty/campus/level/alumni roles across **all mutual servers** shared with the bot.

### 🧪 Test Suite Hardening
* Expanded test suite from 215 to **223 passing unit tests** with 0 warnings under `-W error` (including complete blacklist CRUD, verification gating, guest blocking, and slash command integration tests in `tests/test_blacklist.py`).

---

## [v2.5.0] — 2026-09-17
### ☁️ Continuous Cloud Replication & Litestream Integration
* **Litestream SQLite Cloud Streaming**:
  - Configured continuous frame-by-frame SQLite streaming to Cloudflare R2 / AWS S3 via `litestream.yml` (`sync-interval: 10s`, `retention: 720h`).
  - Added multi-mode operational support: wrapped execution (`litestream replicate -log-level warn -exec "..."`), decoupled tmux execution, and dedicated systemd user service (`deploy/litestream.service`).
  - Streamlined environment variable resolution compatible with Go `os.ExpandEnv`.

### 🛡️ Storage Guard & Self-Healing Space Reclamation
* **Automated Storage Monitoring (`StorageGuardService`)**:
  - Added proactive disk and database footprint tracking (`tarveri.db`, `-wal`, `-shm`, `backups/`, `logs/`) against configurable limits (`TARVERI_MAX_STORAGE_MB`).
  - Triggers real-time Sentry warnings at 80% and high-priority `StorageLimitExceededError` alerts at 100%.
  - Executes automatic self-healing space recovery: WAL truncation (`PRAGMA wal_checkpoint(TRUNCATE)`), audit log pruning >30 days (`prune_audit_logs()`), and backup rotation.

### 🧹 Architecture Refactoring & CI Automation
* **Unified Role Management (`RoleManager`)**:
  - Consolidated double-checked locking, cache lookups, Discord API fallbacks, atomic role creation, and audit logging into a single unified manager, eliminating ~500 lines of duplicate role logic across `VerificationService` and `GuestService`.
* **Typed Environment Parsing (`Settings.from_env`)**:
  - Refactored `tarveri/config.py` with typed helper utilities (`_env_str`, `_env_int`, `_env_bool`, `_env_optional_int`), reducing boilerplate by 75%.
* **GitHub Actions Multi-Version CI Matrix**:
  - Added `.github/workflows/ci.yml` running automated pytest suite across Python 3.11, 3.12, and 3.13.
  - Expanded test coverage to **215 passing unit tests**.

---

## [v2.4.1] — 2026-09-09
### 🐛 Bug Fixes & Reliability Patches
* **Direct Message Callable Prefix Compatibility**:
  - Fixed `TypeError` in `VerificationCog.on_message` when `command_prefix` is configured with a callable (e.g. `commands.when_mentioned_or("!")`) or tuple of prefixes.
  - Resolves prefixes dynamically via `await self.bot.get_prefix(message)` before evaluating message content.
* **Deterministic Database Collision Rollbacks via Guild ID**:
  - Upgraded `RoleSyncResult` items from `(guild_name, role_name)` to `(guild_id, guild_name, role_name)`.
  - Collision rollbacks now lookup guilds directly in $O(1)$ time by immutable snowflake ID (`bot.get_guild(g_id)`), preventing failures or mistaken role removals caused by duplicate or renamed servers.
* **Safe Updater Remote Pruning & Automatic Branch Fallback**:
  - Added `git fetch --prune origin` to `./scripts/update.sh` and `UpdateCheckerService` to automatically purge deleted remote branch tracking refs.
  - Added automatic detection and redirection to `origin/main` whenever an active branch has been merged or deleted on GitHub.
* **RateLimiter Async Documentation Refinement**:
  - Clarified docstrings to reflect single-event-loop asyncio thread concurrency.
* **Test Suite Expansion**:
  - Added test cases covering callable prefix DM processing, command skipping, and remote stream fallback, bringing test coverage to **97 tests passing with 0 warnings**.

---

## [v2.4.0] — 2026-09-09
### 🛡️ Self-Healing & Auto-Recovery Engine
* **Database Integrity & WAL Truncation**:
  - Automatically runs `PRAGMA integrity_check` on connection startup to detect and report corruptions immediately.
  - Automatically checkpoints and truncates SQLite WAL (`PRAGMA wal_checkpoint(TRUNCATE)`) on startup and shutdown to keep disk footprints minimal.
* **Channel Drift & Stale Setting Recovery**:
  - Automatically detects deleted/missing Discord channels (review channels, welcome channels, help channels) in `find_parent_review_channel()`, `get_welcome_or_verify_channel()`, and `is_help_channel()`.
  - Clears stale database IDs from `guild_settings` via `clear_stale_channel_setting()` and falls back smoothly to keyword-matched channels (`review`, `approval`, `ticket`, `help`, `welcome`).
* **Dynamic Role Re-creation with Server Design Colors**:
  - Auto-recreates deleted faculty roles on the fly (`FACULTY_COLORS` mapping: FAFB Dark Red `#992D22`, CPUS Dark Teal `#1F8673`, FOCS Yellow `#F1C40F`, FCCI Dark Purple `#71368A`, FOAS Coral Red `#E74C3C`, FOBE Green `#2ECC71`, FSSH Blue `#3498DB`, FOET Lime Green `#BAE973`, Guest Green `#2ECC71`) without failing user verifications.
* **Downtime Manual Grant Detection**:
  - Detects if an administrator manually granted the `Guest(Approved)` role to an applicant during maintenance or while a review ticket was open.
  - Auto-resolves the ticket to `APPROVED` (*"Applicant was manually granted guest role by admin"*), marks the referral code as `USED`, and archives the thread.
* **Batch Role Auto-Restoration for Returning Students**:
  - Added `reconcile_verified_members()` on bot startup to cross-reference guild members against verified records in SQLite and restore missing faculty roles to students who rejoined during maintenance.
* **Role Hierarchy & Permission Diagnostics (`/diagnose`)**:
  - Added `diagnose_guild_permissions()` to audit bot permissions (`Manage Roles`, `Manage Channels`, etc.) and detect role hierarchy conflicts (when managed roles are above bot's top role).
  - Added `/diagnose` slash command for administrators to run instant server health checks, permission audits, and self-healing reconciliation on demand.

### 🔢 Alphanumeric Ticket Sequencing & Smart Escalation
* **Alphanumeric Ticket Tracking**:
  - Upgraded review ticket sequences from 4-digit numbers to scalable alphanumeric identifiers (`#A0001` – `#Z9999` $\to$ `#AA0001`) via `format_ticket_seq()`.
  - Displayed across private thread names (`guest-a0001-username`), review embeds, logs, and `/guest_tickets`.
* **Batch-of-2 Staff Mentions & 1-Hour Escalation**:
  - `get_target_admin_mentions_batch()` selects 2 admins per notification, prioritizing active/online moderators first, then highest-authority staff (Owner $\to$ Senior Admins).
  - Added `check_and_escalate_tickets()` and background task `_escalation_loop` that automatically tags the next 2 admins in hierarchy if a ticket is pending for $\ge$ 1 hour without staff reply.

### 🧪 Test Suite & Warning Hardening
* Hardened mock and role iterables across all test fixtures against unawaited `_aget` coroutines.
* Reached 95 passing unit tests with 0 warnings under `-W error`.

---

## [v2.3.0] — 2026-09-08
### 🤝 Double Verification Workflow & Audit Tracking
* **Two-Step Guest Verification Process**:
  - Requires explicit confirmation from both the referring student (Step 1: vouch statement / context) and server administration (Step 2: final approval or veto).
  - Automatically tags the server admin role (`@Admin` / `@Staff`) inside the review thread once the voucher submits their statement.
* **Reason Giver & Comments Auditing**:
  - Tracks and stores explicit author IDs, comments, notes, and timestamps in `guest_tickets`:
    - `applicant_id` & submission reason.
    - `vouched_by_id`, `vouch_note`, `vouched_at`.
    - `closed_by_admin_id`, `close_reason`, `closed_at`.
* **Automatic Role & Ticket Revocation on Leave / Kick / Ban**:
  - Implemented `handle_member_leave_or_ban()` to automatically revoke guest status, close open/approved tickets (`LEFT_SERVER` / `BANNED`), and invalidate active referral codes whenever a member leaves or is removed.

### ⏱️ Timezone & Backup Lifecycle Management
* **Local Machine / NTP Timezone Synchronization**:
  - Added `TimezoneFormatter` and `get_configured_tz()` (defaulting to `Asia/Kuala_Lumpur` / UTC+8 or `TARVERI_TIMEZONE` / local system time).
  - Synchronized terminal logs, file logs, database audit timestamps, backup filenames, and embed footers with the host machine's NTP clock.
* **Automatic 10-Backup Rotation Policy**:
  - Added `rotate_backups()` in `tarveri/database.py` and `scripts/update.sh` to automatically prune older database snapshots, retaining only the 10 most recent backups (`TARVERI_MAX_BACKUPS`).

### 🚀 UX & Performance Polish
* **Permanent Public Messages (No TTL)**:
  - Removed auto-delete timers (`delete_after`) from public channel messages (`#welcome`, `#help`, `!verify`, `!sync`), keeping guidance permanently visible for future students.
  - Ephemeral user responses retain a clean 60-second self-deleting TTL.
* **Instant Non-Blocking Startup**:
  - Made slash command sync asynchronous in `setup_hook()` to eliminate bot boot latency.
* **Multilingual Semantic Help Engine**:
  - Broadened regex matching in `#help` channels to support Malaysian colloquial phrases (`nak verify`, `camne nak masuk`, `bantuan`, `matrik`, `id number`, etc.) and removed redundant verification blocks.

### 🔄 Zero-Downtime Backwards Compatibility
* **Dynamic Table Migrations**:
  - Dynamic `PRAGMA table_info` checks automatically add missing columns to existing SQLite databases without data loss.
* **Legacy Environment Variable Fallbacks**:
  - Supports older `.env` keys (`DISCORD_BOT_TOKEN`, `HASH_SECRET`, `DB_PATH`, `ADMIN_ROLE`, `TIMEZONE`, etc.).
* **Module Execution & Top-Level Exports**:
  - Added `tarveri/__main__.py` for `python -m tarveri` execution and exposed core symbols in `tarveri/__init__.py`.

---

## [v2.2.0] — 2026-09-03
### 🎟️ Guest Access, Referral Codes & Private Thread Reviews
* **Verified Student Referral Codes (`/referral generate`, `/referral list`)**:
  - Allowed verified TARUMT students to generate single-use, expiring referral codes (e.g. `TAR-8X2K9P`) for friends and collaborators.
  - Added rate limiting (max 3 active codes per student) and configurable TTL (default 48 hours).
  - Code lifecycle tracking (`ACTIVE` → `PENDING_APPROVAL` → `USED` / `EXPIRED`).
* **Persistent Verification Gateway Panel (`/send_gateway_panel`)**:
  - Added persistent 3-button welcome panel with `[Verify TARUMT Student]`, `[Enter Referral Code]`, and `[Apply as Guest]`.
* **Private Discord Thread Review System**:
  - Created private review threads under the configured review/help channel.
  - Automatically invites the applicant (`@Friend`), referring student (`@Student`), and pings the admin role.
  - Added interactive review actions: **`[Approve Guest]`**, **`[Reject & Kick]`** (with custom reason modal), and **`[Confirm Vouch]`**.
* **Smart `Guest(Approved)` Role Detection & Reuse**:
  - Prioritizes existing server roles named `Guest(Approved)`, `Guest (Approved)`, or `Guest` before attempting to create duplicate roles.
  - Automatically isolates guest access to general channels while excluding faculty-restricted channels.
  - Updated `is_unverified_member()` so approved guests are not prompted with student verification tips.

### 🛡️ Security & Anti-Oracle Protections
* **Uniform Error Oracle Mitigation**:
  - Sanitized referral validation error outputs to prevent attackers from enumerating valid/expired/used codes.
* **Rate-Limited Guest & Referral Submissions**:
  - Extended sliding-window rate limiting to modal submission endpoints to block automated brute-force attempts.
* **Sensitive Data Masking**:
  - Student IDs remain strictly masked (`23***867`) in all database logs and admin audits.

### ⚡ Performance & Engine Optimizations
* **SQLite Engine Performance Tuning**:
  - Enabled 64MB memory-mapped I/O (`PRAGMA mmap_size = 67108864;`).
  - Allocated 4MB dedicated RAM page cache (`PRAGMA cache_size = -4000;`).
  - Forced temporary tables, sort buffers, and indexes to memory (`PRAGMA temp_store = MEMORY;`).
* **Drift-Immune Monotonic Rate Limiting**:
  - Converted `RateLimiter` to `time.monotonic()` to eliminate vulnerabilities from NTP system clock drift and leap seconds.
* **Bulk Referral Cleanup**:
  - Added `cleanup_expired_referrals()` for high-speed background batch expiration.

### 🪵 Logging Cleanliness & Multi-Server Tagging
* **Clean Startup Output**:
  - Replaced verbose multi-line channel dump banner on startup with a single, clear summary line.
* **Enforced Server Tagging**:
  - Added explicit `[Server: 'Server Name']` context to all backend mutation logs, admin actions, and warnings.

---

## [v2.1.0] — 2026-09-03
### 🌐 Multi-Server Configuration & Admin Suite
* **Per-Server Channel Mapping (`/setwelcomec`, `/sethelpc`, `/setguestrole`, `/setreviewchannel`)**:
  - Added SQLite `guild_settings` table to persist independent welcome channels, help channels, guest roles, and review channels per Discord server.
* **Zero-Delay Command Synchronization (`!sync`, `/sync_commands`)**:
  - Added `!sync` (and `!sync guild`) prefix command to instantly copy global commands to the current server without waiting up to 1 hour for Discord's global cache.
* **Backend Database Inspector (`scripts/show_servers.py`)**:
  - Created a CLI tool for bot hosters to inspect connected servers, channel IDs, guest roles, and verification metrics.
* **Automated Member Assistance**:
  - **New Member Onboarding**: Automatically tags unverified new joiners in the server's welcome channel.
  - **Smart Role Help Tips**: Proactively replies to unverified members asking how to get roles in support channels.

---

## [v2.0.0] — 2026-09-02
### 🏗️ Modular Architecture & Safe Automated Updates
* **Modular Codebase Refactoring**:
  - Reorganized the monolithic script into clean, decoupled modules: `tarveri/bot.py`, `tarveri/config.py`, `tarveri/database.py`, `tarveri/rate_limiter.py`, `tarveri/services/`, and `tarveri/cogs/`.
* **Zero-Downtime Safe Updater (`scripts/update.sh`)**:
  - Automated updater featuring atomic pre-update SQLite database snapshots (`VACUUM INTO`), virtualenv dependency sync, pre-flight test verification, and automated rollback upon failure.
  - Multi-stream branch support (`--stream main`, `--stream refactor/modular-optimization`, or `auto`).
* **Background Update Checker (`UpdateCheckerService`)**:
  - Non-blocking background service that checks git upstream and sends DM alerts to the hoster when updates are available.
* **Administrative Tooling**:
  - Added `/stats`, `/unverify`, `/audit`, `/backup`, `/resync`, and `/check_updates` slash commands.
* **Automated Async Test Suite**:
  - Built comprehensive test suite covering all services, rate limiters, databases, and cogs (39 unit tests).

---

## [v1.0.0] — 2026-09-01
### 🚀 Initial Release
* Basic student verification pipeline for TARUMT.
* HMAC-SHA256 privacy hashing to protect raw student IDs at rest.
* Faculty code mapping (`FAFB`, `FCCI`, `FOAS`, `FSSH`, `FOBE`, `CPUS`, `FOCS`, `FOET`).
* Basic `/verify` slash command and prefix commands.
* Graceful shutdown handlers.
