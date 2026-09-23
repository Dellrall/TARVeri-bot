# 🎓 Student Verification & Academic Lifecycle Guide

TARVeri automates student verification and role provisioning for Tunku Abdul Rahman University of Management and Technology (TARUMT) students across branch campuses and faculties.

---

## 📋 Table of Contents
1. [Student ID Format & Parsing](#1-student-id-format--parsing)
2. [Dynamic Century Windowing](#2-dynamic-century-windowing)
3. [Institutional Email OTP Verification](#3-institutional-email-otp-verification)
4. [Programme Code Storage & Multi-Character Role Recovery](#4-programme-code-storage--multi-character-role-recovery)
5. [8-Year Expiry Anomaly Guard](#5-8-year-expiry-anomaly-guard)
6. [Academic Level Progression](#6-academic-level-progression)
7. [Graduation & Alumni Status](#7-graduation--alumni-status)
8. [Tiered Trust & Cross-Server Role Synchronization](#8-tiered-trust--cross-server-role-synchronization)

---

## 1. Student ID Format & Parsing

TARUMT student IDs follow the pattern `YY[Campus][Faculty][Level]XXXXX` (e.g., `24WMR01234`):

| Component | Code | Meaning | Role Assigned |
| :--- | :---: | :--- | :--- |
| **Intake Year** | `24` | 2024 Intake | — |
| **Campus Branch** | `W` | KL Main Campus | `KL Main Campus` |
| | `P` | Penang Branch | `Penang Branch` |
| | `A` | Perak Branch | `Perak Branch` |
| | `J` | Johor Branch | `Johor Branch` |
| | `C` / `K` | Pahang Branch | `Pahang Branch` |
| | `S` | Sabah Branch | `Sabah Branch` |
| **Faculty** | `M` | Faculty of Computing & Information Technology | `FOCS` |
| | `B` | Faculty of Accountancy, Finance & Business | `FAFB` |
| | `P` | Centre for Pre-University Studies | `CPUS` |
| | `K` | Faculty of Communication & Creative Industries | `FCCI` |
| | `L` | Faculty of Applied Sciences | `FOAS` |
| | `V` | Faculty of Built Environment | `FOBE` |
| | `J` | Faculty of Social Science & Humanities | `FSSH` |
| | `G` | Faculty of Engineering & Technology | `FOET` |
| **Study Level** | `F` | Foundation | `Foundation` |
| | `D` | Diploma | `Diploma` |
| | `R` | Bachelor Degree | `Degree` |
| | `P` | Postgraduate | `Postgraduate` |

> [!NOTE]
> Student ID, faculty, campus, and study-level attributes are self-declared via structured student ID syntax parsing and attested via institutional email OTP challenge rather than a direct university SIS/LDAP database integration. To assist server administrators with auditability and spot-checking, TARVeri logs both the masked student ID and masked email address on every verification event (`/admin audit`).

---

## 2. Dynamic Century Windowing

TARVeri uses a sliding century window algorithm (`< 70 -> 20xx`, `>= 70 -> 19xx`) to parse 2-digit years. This supports historical TAR College alumni records from **1969** up to **2068+** without hardcoded year limits.

---

## 3. Institutional Email OTP Verification

When enabled (`TARVERI_EMAIL_VERIFICATION_ENABLED=true`):
1. User enters Student ID and official institutional email (`@student.tarc.edu.my` or `@tarc.edu.my`).
2. **Pre-Flight Validation & Bounce Gating**: Checks rate limits, duplicate blind hashes, and queries the `bounced_emails` registry to prevent contacting invalid or deactivated university mailboxes.
3. **6-Digit Secure OTP**: Dispatched via `aiosmtplib` with dynamic Discord countdown timers.
4. **Encryption at Rest**: Student email addresses are encrypted at rest with Fernet (AES-128-CBC with HMAC-SHA256 authenticated encryption) in SQLite, with an HMAC-SHA256 blind index preventing duplicate registrations.
5. **SMTP NDR Bounce Detection**: If the institutional mail server rejects the delivery (550 / 554 / mailbox unavailable), the bounce is recorded in SQLite and the user is provided with actionable guidance to check their email spelling or mailbox activation.

---

## 4. Programme Code Storage & Multi-Character Role Recovery

TARVeri parses and stores the 3-letter programme prefix (`programme_code`, e.g. `WMR` from `24WMR12331`):
- `W` = Campus Branch (`KL Main Campus`)
- `M` = Faculty (`FOCS`)
- `R` = Study Level (`Degree`)
- `WMR` = Complete Programme Identity

This ensures that even during full-server reconciliations or member rejoins, all 3 constituent roles (`Faculty`, `Campus`, and `Study Level`) are 100% reconstructed via multi-tier resolvers (`resolve_faculty_role()`, `resolve_campus_role()`, and `resolve_study_level_role()`) without ambiguous fallback.

---

## 5. 8-Year Expiry Anomaly Guard

If a student enters an unusual expiry date exceeding $\pm 8$ years relative to their intake (such as typing `06/07` intending July 6th, which parses as June 2007), TARVeri presents an interactive panel:
- `[Confirm Date]`: Keeps the date if the student graduated in the past.
- `[Re-enter Date]`: Opens a modal to re-input the correct date.
- `[Auto-Calculate]`: Estimates graduation date based on study level (+1y for Foundation, +2y for Diploma, +3y for Degree).

---

## 6. Academic Level Progression

Students transitioning between levels (e.g. Diploma $\to$ Degree):
- Simply run `/verify student_id:<new_id>`.
- Atomically strips previous study level/alumni roles and updates all mutual server roles with audit logging in `verification_transitions`.

---

## 7. Graduation & Alumni Status

- **`/graduate [year] [programme]`**: Verified students claim their `TARUMT Alumni` role and gold card badge.
- **Graduation Watchdog**: Scans daily for expired card dates and sends polite DM resolution prompts.

---

## 8. Tiered Trust & Cross-Server Role Synchronization

TARVeri implements a tiered verification trust model across multiple Discord servers:

| Verification Tier | Description | Opt-Out Servers (`require_email=0`) | Opt-In Servers (`require_email=1`) |
| :--- | :--- | :--- | :--- |
| **Tier 1: Fast Verified** | Student ID syntax verified without email OTP | **Auto-assigned roles** on join and resync | **Roles held in escrow** until email OTP verification is completed |
| **Tier 2: Email-Attested** | Student ID + `@student.tarc.edu.my` OTP verified | **Auto-assigned roles immediately** | **Auto-assigned roles immediately** |

### Member Join & Server Re-entry Flow:
- When a user joins an **Opt-In Server**:
  - If the user already completed Tier 2 (Email OTP) in any mutual server, roles are granted instantly.
  - If the user previously verified only as Tier 1 (no email OTP), roles are **not granted**. The bot welcomes the user in `#welcome` with the 3-button verification gateway, prompting them to complete institutional email OTP verification before gaining faculty roles.

### Self-Healing & Unverification Lifecycle:
1. **Unverification (`/admin unverify`)**: Strips faculty, campus, study level, and alumni roles across **all mutual servers** the user shares with the bot, clears database persistence, and resets rate limiters.
2. **Re-verification & Upgrading**: When a Tier 1 student completes email OTP verification in an Opt-In server, their profile is upgraded to Tier 2 and automatically assigns roles across all remaining mutual Opt-In servers.
3. **Self-Healing Reconciliation**:
   - Strips unauthorized roles from non-email-verified students in Opt-In servers.
   - Cleans up stray verified roles from unverified users who were manually given roles or unlinked.
   - Restores missing roles to verified students across all eligible servers.
