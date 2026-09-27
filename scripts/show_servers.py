#!/usr/bin/env python3
"""
Inspect per-server channel configuration in the SQLite database backend.
Usage: python scripts/show_servers.py [path_to_db]
"""

from __future__ import annotations

import os
import sqlite3
import sys


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TARVERI_DB_PATH", "tarveri.db")

    if not os.path.exists(db_path):
        print(f"Database not found at: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get total verified
    try:
        cursor.execute("SELECT COUNT(*) FROM verifications")
        total_verified = cursor.fetchone()[0]
    except sqlite3.OperationalError:
        total_verified = 0

    try:
        cursor.execute("SELECT COUNT(*) FROM referral_codes WHERE status = 'ACTIVE'")
        active_referrals = cursor.fetchone()[0]
    except sqlite3.OperationalError:
        active_referrals = 0

    print("=" * 85)
    print(" TARVeri Backend Database Inspector")
    print(f" Database File: {db_path}")
    print(f" Total Verified Students: {total_verified} | Active Referral Codes: {active_referrals}")
    print("=" * 85)

    try:
        cursor.execute(
            """SELECT guild_id, welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, updated_at
               FROM guild_settings ORDER BY updated_at DESC"""
        )
        rows = cursor.fetchall()
    except sqlite3.OperationalError:
        rows = []

    if not rows:
        print("\nNo servers configured yet in 'guild_settings' table.")
        print("Servers will use auto-detection until an admin configures channels using:")
        print("  /admin dashboard (interactive UI)")
        print("  /admin set_channel welcome|help|review #channel")
        print("  /admin set_role guest|admin [role]\n")
    else:
        print(f"\nConfigured Servers ({len(rows)} server(s) in SQLite 'guild_settings'):")
        print("-" * 105)
        print(
            f"{'Guild ID':<20} | {'Welcome Channel':<18} | {'Help Channel':<18} | {'Guest Role':<15} | {'Review Channel':<18}"
        )
        print("-" * 105)
        for g_id, w_id, h_id, g_role, r_id, updated in rows:
            w_str = str(w_id) if w_id else "Auto-detect"
            h_str = str(h_id) if h_id else "Auto-detect"
            r_str = str(r_id) if r_id else "Auto-detect"
            role_str = str(g_role) if g_role else "Guest"
            print(f"{g_id!s:<20} | {w_str:<18} | {h_str:<18} | {role_str:<15} | {r_str:<18}")
        print("-" * 105)

    conn.close()


if __name__ == "__main__":
    main()
