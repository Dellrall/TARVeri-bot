# 🚨 Operational Incidents & Post-Mortems

This document chronicles operational incidents, root cause analyses (RCA), and preventative engineering mitigations implemented in TARVeri.

---

## 📋 Incident Register

| Incident ID | Date | Severity | Category | Title | Status |
| :--- | :---: | :---: | :--- | :--- | :---: |
| **[INC-2026-09A](#inc-2026-09a-unintended-role-revocation-during-email-policy-enforcement)** | 2026-09-21 | **High (P2)** | Access Control & Policy | Unintended Role Revocation During Email Policy Enforcement | **Resolved** |
| **[INC-2026-09B](#inc-2026-09b-role-recovery-degradation-for-past-verified-members--programme-codes)** | 2026-09-21 | **Medium (P3)** | Identity & Role Reconciliation | Role Recovery Degradation for Past-Verified Members & Programme Codes (`WMR`) | **Resolved** |
| **[INC-2026-09C](#inc-2026-09c-smtp-ndr-bounce-delivery-loops--non-existent-mailbox-rejection)** | 2026-09-21 | **Low (P4)** | Email Delivery & SMTP | SMTP NDR Bounce Delivery Loops & Non-Existent Mailbox Rejection | **Resolved** |

---

## INC-2026-09A: Unintended Role Revocation During Email Policy Enforcement

### 1. Executive Summary
When server administrators enabled or refreshed email verification enforcement on existing servers, previously verified active students who had verified prior to the introduction of email verification (or on servers without email verification) faced automated role revocation during reconciliation routines. In a production incident on `tarumt banana hub`, **89 verified student members** had 2 to 3 roles stripped during a background `[ROLE_POLICY_ENFORCED]` self-healing cycle.

### 2. Root Cause Analysis (RCA)
- The cross-guild role synchronizer (`assign_role_across_guilds`) enforced email attestation (`requires_email_in`) globally if any server requested email verification, without checking if the member was already verified prior (`is_past_verified`).
- Unverified/rogue role cleanup routines stripped roles during background self-healing reconciliation without a safety threshold or human-in-the-loop authorization when affecting multiple members simultaneously.

### 3. Impact & Blast Radius
- **89 verified student members** in `tarumt banana hub` lost their faculty, branch campus, and study-level roles during automated background reconciliation.
- Server moderators received alerts and unverified members were temporarily locked out of faculty discussion channels.

### 4. Corrective & Preventative Actions (CAPA)
1. **Interactive 2-Step Quorum for Mass Actions (`PendingMassAction`)**:
   - Any automated or administrative action affecting **more than 5 users** (configurable via `TARVERI_MASS_REVOCATION_THRESHOLD`) is automatically intercepted and queued in SQLite `pending_mass_actions`.
   - The bot renders an interactive confirmation embed with **`Approve`** and **`Reject`** buttons sent to server administrators in private channels or the command context.
   - Prevents accidental bulk revocations without explicit human approval.
2. **Past-Verified Exemption (`is_past_verified`)**:
   - Updated `assign_role_across_guilds()` so that members who already hold a verified record in SQLite (`is_past_verified`) are exempt from blocking when email verification is not strictly enforced on that specific guild (`enable_email_role_enforcement=False`).
3. **Reconciliation Role Recovery Fallback**:
   - Integrated `resolve_faculty_role()`, `resolve_campus_role()`, and `resolve_study_level_role()` into reconciliation tasks to gracefully reconstruct member roles even if email hashes are missing on legacy records.

### 5. Resolution & Operational Verification
- **Automated Self-Healing Verified**: Following deployment of the past-verified exemption and resolver engine, TARVeri's startup self-healing reconciliation (`reconcile_verified_members()`) automatically re-matched the 89 stripped members against SQLite verification profiles and restored all Faculty, Campus, Study Level, and Alumni roles with 100% success and zero human intervention required.
- **Mass Action Guard Active**: All background loops and admin commands affecting $\ge 5$ members now route through interactive quorum approval.

---

## INC-2026-09B: Role Recovery Degradation for Past-Verified Members & Programme Codes

### 1. Executive Summary
When attempting to restore or reconcile roles for students with legacy records or 3-letter programme codes (such as `24WMR12331`, where `WMR` represents KL Main Campus $\to$ Faculty of Computing $\to$ Degree), the role recovery mechanism failed to resolve the faculty or campus roles.

### 2. Root Cause Analysis (RCA)
- In TARUMT student IDs (`YY[Campus][Faculty][Level]XXXXX`):
  - `W` = Campus Branch (`KL Main Campus`)
  - `M` = Faculty (`FOCS`)
  - `R` = Study Level (`Degree`)
  - `WMR` = Complete Programme Prefix
- The historical verification table stored single-character or multi-character faculty codes ambiguously. When parsing 2-letter or 3-letter strings, direct dictionary lookups (`FACULTY_ROLES.get(code)`) returned `None`, leaving the user with missing faculty or campus roles during `/verify` resync or `/admin resync`.

### 3. Impact & Blast Radius
- Returning students and rejoining members were verified in the database but could not receive their faculty roles upon rejoining or running `/verify`.

### 4. Corrective & Preventative Actions (CAPA)
1. **Multi-Tier Robust Role Resolvers**:
   - Implemented `resolve_faculty_role(code)`:
     - Tier 1: Single character code (`M` $\to$ `FOCS`)
     - Tier 2: Exact full role name (`FOCS` $\to$ `FOCS`)
     - Tier 3: 2-Letter branch-faculty code (`WM` $\to$ `FOCS`, `PK` $\to$ `FCCI`, `PB` $\to$ `FAFB`)
     - Tier 4: 3-Letter programme code (`WMR` $\to$ `FOCS`, `PKD` $\to$ `FCCI`)
     - Tier 5: Dynamic alias match (`Faculty of Computing` $\to$ `FOCS`)
   - Implemented `resolve_campus_role(code)` and `resolve_study_level_role(code)` with matching multi-tier resolution.
2. **Database Schema Field & Migration (`programme_code`)**:
   - Added `programme_code TEXT DEFAULT NULL` to the `verifications` table.
   - Added automatic migration and backfill on startup:
     ```sql
     UPDATE verifications
     SET programme_code = campus_code || faculty_code || COALESCE(level_code, 'R')
     WHERE programme_code IS NULL AND campus_code IS NOT NULL AND faculty_code IS NOT NULL;
     ```
3. **Dedicated Admin Recovery Command (`/admin restore_roles [user]`)**:
   - Provides server administrators with immediate, granular recovery of all faculty, branch campus, study level, and alumni roles for a specific user or the entire server with embed reporting.

---

## INC-2026-09C: SMTP NDR Bounce Delivery Loops & Non-Existent Mailbox Rejection

### 1. Executive Summary
Students inputting invalid, misspelled, or deactivated institutional student email addresses (e.g. typing `@student.tarc.edu` or a discontinued student ID mailbox) caused institutional mail servers to return hard bounce status codes (e.g. `550 5.1.1 User unknown / Mailbox unavailable`). The bot repeatedly attempted retransmissions on subsequent retries, generating relay error logs and unhelpful generic timeout errors.

### 2. Root Cause Analysis (RCA)
- Non-delivery reports (NDRs) and hard bounce responses (SMTP 550, 551, 552, 553, 554) were treated as transient SMTP transport errors rather than permanent recipient delivery failures.
- No local database table existed to record and cache bounced email addresses, causing the bot to re-contact the SMTP relay every time the user re-clicked the verification button.

### 3. Impact & Blast Radius
- Wasted SMTP API relay quota on guaranteed-to-fail email addresses.
- Poor user experience: users received generic "SMTP transmission failed" messages rather than clear notifications that their university mailbox was deactivated or mistyped.

### 4. Corrective & Preventative Actions (CAPA)
1. **RFC-Compliant SMTP Bounce Classifier (`is_smtp_bounce_error`)**:
   - Inspects SMTP status codes and NDR diagnostics for recipient bounce patterns (`550`, `551`, `552`, `553`, `554`, `5.1.1`, `User unknown`, `Mailbox unavailable`, `Recipient rejected`).
   - Specifically ignores sender-side relay rate limits (e.g. daily quota exceeded) to prevent false-positive recipient bounce classifications.
2. **Persistent `bounced_emails` SQLite Registry**:
   - Creates `bounced_emails` table storing blind hashes (`email_hash UNIQUE`), encrypted email strings (`email_encrypted`), bounce code, and detection timestamps.
   - Methods: `record_bounced_email()`, `is_email_bounced()`, `get_bounced_email()`, `list_bounced_emails()`, `remove_bounced_email()`.
3. **Zero-Network Pre-Flight Circuit**:
   - `generate_and_send_otp()` checks `is_email_bounced()` before making any network calls.
   - If previously bounced, OTP dispatch is aborted instantly with clear user-facing guidance:
     > ⚠️ *The email `s***@student.tarc.edu.my` was previously rejected/bounced by the institutional mail server. Please verify your student mailbox is active and receiving mail, or contact server staff.*
