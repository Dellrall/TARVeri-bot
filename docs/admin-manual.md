# 🛡️ Administrator Operations & Control Center Manual

TARVeri includes an interactive administration suite with diagnostics, telemetry, role synchronization, and server operations.

---

## 📋 Table of Contents
1. [Interactive Admin Dashboard](#interactive-admin-dashboard)
2. [Diagnostic & Self-Healing Commands](#diagnostic--self-healing-commands)
3. [Moderation & Verification Actions](#moderation--verification-actions)
4. [Channel & Role Configuration](#channel--role-configuration)
5. [Database Backups & Audit Logs](#database-backups--audit-logs)
6. [Complete Slash Command Matrix](#complete-slash-command-matrix)

---

## 1. Interactive Admin Dashboard (`/admin dashboard`)

Opens the interactive Administrator Control Center featuring:
- **Navigation Menu**: Dropdown categories (`Overview`, `Configuration`, `Diagnostics`, `Moderation`, `Tickets`, `Backups`, `Logs`, `Gateway Panel`).
- **One-Click Actions**: Trigger self-healing diagnostics, database backups, email requirement toggles, and member unverification via native Discord modals.

---

## 2. Diagnostic & Self-Healing Commands

- **`/admin diagnose`**: Checks role hierarchies, scans for duplicate faculty roles, restores missing council/SRC roles, and cleans orphaned ticket states.
- **`/admin backfill_roles [default_campus] [default_level] [all_servers]`**: Batch-syncs campus branch and study level roles for all previously verified members.
- **`/admin resync`**: Reconciles and synchronizes member roles across all mutual guilds.
- **`/admin sync_commands`**: Forces command tree synchronization with the Discord API.

---

## 3. Moderation & Verification Actions

- **`/admin unverify @user [reason]`**: Unlinks a student ID, records audit history, and revokes faculty roles.
- **`/admin alumni_revoke @user [reason]`**: Revokes alumni status and removes the `TARUMT Alumni` role across mutual servers.
- **`/admin close_ticket [ticket] [reason]`**: Manually closes a guest review ticket without kicking the applicant from the server.

---

## 4. Channel & Role Configuration

- **`/admin set_channel [type] [channel]`**: Configures `welcome`, `help`, or guest `review` channels.
- **`/admin set_role [type] [role]`**: Configures `guest` or `admin` reviewer roles.
- **`/admin panel [channel]`**: Posts the persistent 3-button verification gateway panel.
- **`/admin email_verification [enabled]`**: Enables or disables mandatory email OTP verification on the current server.

---

## 5. Database Backups & Audit Logs

- **`/admin backup [action]`**: Creates an immediate SQLite snapshot or lists previous backup archives.
- **Dual Backup Isolation**:
  - **Daily Backups (`backups/daily/`)**: Retains the 5 most recent uncompressed daily snapshots. Older snapshots are automatically compressed into `.tar.gz` archives inside `backups/daily/archives/`.
  - **Update Backups (`backups/updates/`)**: Retains the 5 most recent pre-update snapshots created by `scripts/update.sh` (older update backups > 5 are pruned).
- **`/admin logs [action]`**: Tails live logs or inspects 10-day `.tar.gz` compressed archives.
- **`/admin audit [limit] [event_type]`**: Queries structured database audit records with filter support.

---

## 6. Multi-Vector Per-Server Blacklist System

TARVeri includes a privacy-preserving, per-server blacklist system allowing server administrators to block and automatically strip roles from bad actors across 3 independent dimensions:

- **Discord User ID (`USER`)**: Blocks specific Discord accounts from verifying, requesting guest access, or creating referral codes in the server.
- **Student ID Hash (`STUDENT_ID`)**: Blocks specific TARUMT student IDs via HMAC-SHA256 blind indexing (`display_mask: 23***34`), preventing banned students from verifying through alternate Discord accounts.
- **Institutional Email Hash (`EMAIL`)**: Blocks specific institutional emails (`display_mask: s***@student...`), preventing compromised or banned student emails from verifying in the server.

### Automatic Enforcement & Self-Healing:
1. **Instant Role Revocation**: Adding a target to a server's blacklist immediately strips all faculty, campus, study level, alumni, and guest roles from the member in that server.
2. **Verification Gating (`/verify`)**: Blocked immediately with `BLACKLIST_ATTEMPT_BLOCKED` if the user, student ID, or email is blacklisted in that server context.
3. **Guest & Referral Blocking**: Blacklisted users cannot generate referral codes or open review tickets; referral codes created by blacklisted users cannot be redeemed.
4. **Join & Self-Healing Reconciliation**: `on_member_join` and `/admin resync` ignore blacklisted users and strip any stray roles automatically.
5. **Per-Guild Isolation**: Blacklists are strictly isolated per server. Being blacklisted in Guild A has zero impact on verification status in Guild B.

### Blacklist Slash Commands:
- `/admin blacklist user <user> [reason]`: Blacklists a Discord user in the current server.
- `/admin blacklist student_id <student_id> [reason]`: Blacklists a Student ID in the current server.
- `/admin blacklist email <email> [reason]`: Blacklists an institutional email in the current server.
- `/admin blacklist remove <target_type> <target>`: Unblacklists a target in the current server.
- `/admin blacklist list [target_type] [page]`: Displays paginated blacklist entries.
- `/admin blacklist clear`: Clears all blacklist entries for the current server.

---

## 7. Complete Slash Command Matrix

| Slash Command | Permissions | Description |
| :--- | :---: | :--- |
| `/admin dashboard` | Administrator | Open the interactive control center dashboard. |
| `/admin stats` | Administrator | View student metrics and faculty distribution. |
| `/admin email_stats` | Administrator | View email verification and opt-in rates. |
| `/admin email_verification` | Administrator | Toggle mandatory student email OTP verification. |
| `/admin blacklist user` | Administrator | Blacklist a Discord user from verifying in this server. |
| `/admin blacklist student_id` | Administrator | Blacklist a student ID hash from verifying in this server. |
| `/admin blacklist email` | Administrator | Blacklist an institutional email hash from verifying in this server. |
| `/admin blacklist remove` | Administrator | Remove a target from this server's blacklist. |
| `/admin blacklist list` | Administrator | View paginated blacklist records for this server. |
| `/admin blacklist clear` | Administrator | Clear all blacklist records for this server. |
| `/admin diagnose` | Administrator | Run server health check, role recovery & hierarchy diagnostics. |
| `/admin backfill_roles` | Administrator | Backfill branch campus and study level roles. |
| `/admin unverify` | Administrator | Unlink student ID and revoke roles across all mutual servers. |
| `/admin alumni_revoke` | Administrator | Revoke alumni status and card badges. |
| `/admin panel` | Administrator | Post the 3-button verification gateway panel. |
| `/admin tickets` | Administrator | List and inspect guest review tickets. |
| `/admin close_ticket` | Administrator | Close a review ticket without kicking applicant. |
| `/admin backup` | Administrator | Manage on-demand and automated SQLite snapshots. |
| `/admin logs` | Administrator | View live log files or compressed archives. |
| `/admin audit` | Administrator | Query structured security audit records. |
| `/admin set_channel` | Administrator | Bind welcome, help, or review channels. |
| `/admin set_role` | Administrator | Set guest role or reviewer role names. |
| `/admin resync` | Administrator | Force verification role reconciliation. |
| `/admin updates` | Administrator | Check for upstream Git releases. |
| `/admin sync_commands` | Administrator | Force slash command registration with Discord. |
