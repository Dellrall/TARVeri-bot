#!/usr/bin/env python3
"""
CLI Diagnostic Tool for Testing TARVeri SMTP2GO & Fallback Email Relay.
Usage:
    python scripts/test_email_relay.py your_email@student.tarc.edu.my
    python scripts/test_email_relay.py your_email@gmail.com --primary-only
    python scripts/test_email_relay.py your_email@gmail.com --fallback-only
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure repo root is in python path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from dotenv import load_dotenv

# Load .env file
load_dotenv(repo_root / ".env")

from tarveri.config import Settings
from tarveri.services.email_service import EmailService


def print_banner():
    print("=" * 70)
    print("📧 TARVeri Email Relay & SMTP Failover Diagnostic Tester")
    print("=" * 70)


async def main():
    parser = argparse.ArgumentParser(description="Test TARVeri primary (SMTP2GO) and fallback email delivery.")
    parser.add_argument(
        "recipient",
        nargs="?",
        help="Recipient email address to receive the test OTP email.",
    )
    parser.add_argument(
        "--primary-only",
        action="store_true",
        help="Force test using only the Primary SMTP relay (e.g. SMTP2GO).",
    )
    parser.add_argument(
        "--fallback-only",
        action="store_true",
        help="Force test using only the Fallback direct email server SMTP.",
    )
    parser.add_argument(
        "--student-id",
        default="24WMD09999",
        help="Test student ID for template display (default: 24WMD09999).",
    )

    args = parser.parse_args()

    print_banner()

    # Load settings from environment (.env)
    try:
        settings = Settings.from_env(validate=False)
    except Exception as e:
        print(f"❌ Failed to load settings from .env: {e}")
        return 1

    to_email = args.recipient
    if not to_email:
        try:
            to_email = input("Enter destination email address to receive test OTP: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 1

    if not to_email or "@" not in to_email:
        print("❌ Invalid destination email address.")
        return 1

    print(f"\n🎯 Target Recipient : {to_email}")
    print(f"🔑 Primary Relay     : {settings.smtp_host}:{settings.smtp_port} (User: {settings.smtp_user or '<None>'})")
    print(f"   From Email        : {settings.smtp_from_name} <{settings.smtp_from_email}>")
    print(f"   TLS Enabled       : {settings.smtp_use_tls}")

    if settings.smtp_fallback_host:
        print(
            f"🔁 Fallback Relay    : {settings.smtp_fallback_host}:{settings.smtp_fallback_port} (User: {settings.smtp_fallback_user or '<None>'})"
        )
        fb_from = settings.smtp_fallback_from_email or settings.smtp_from_email
        fb_name = settings.smtp_fallback_from_name or settings.smtp_from_name
        print(f"   Fallback From     : {fb_name} <{fb_from}>")
        print(f"   Fallback TLS      : {settings.smtp_fallback_use_tls}")
    else:
        print("🔁 Fallback Relay    : <Not Configured>")

    email_service = EmailService(settings, mock_smtp=False)

    print("\n" + "-" * 70)

    if args.fallback_only:
        if not settings.smtp_fallback_host:
            print("❌ Cannot test fallback: TARVERI_SMTP_FALLBACK_HOST is not set in .env")
            return 1
        print("🚀 Testing FALLBACK direct SMTP server only...")
        fb_from_email = settings.smtp_fallback_from_email.strip() or settings.smtp_from_email
        fb_from_name = settings.smtp_fallback_from_name.strip() or settings.smtp_from_name
        ok, err = email_service._send_to_smtp_endpoint(
            to_email=to_email,
            otp_code="987654",
            server_name="TARVeri Diagnostic Suite",
            ttl_minutes=10,
            host=settings.smtp_fallback_host,
            port=settings.smtp_fallback_port,
            user=settings.smtp_fallback_user,
            password=settings.smtp_fallback_password,
            from_email=fb_from_email,
            from_name=fb_from_name,
            use_tls=settings.smtp_fallback_use_tls,
            relay_label="Fallback Direct SMTP",
        )
        if ok:
            print(f"✅ SUCCESS! Fallback email transmitted to {to_email} with OTP: 987654")
            print("📬 Please check your inbox (and Spam/Junk folder) to verify receipt.")
            return 0
        else:
            print(f"❌ FAILED on Fallback SMTP ({settings.smtp_fallback_host}:{settings.smtp_fallback_port})!")
            print(f"   Error: {err}")
            return 1

    elif args.primary_only:
        print("🚀 Testing PRIMARY SMTP relay (SMTP2GO) only...")
        ok, err = email_service._send_to_smtp_endpoint(
            to_email=to_email,
            otp_code="123456",
            server_name="TARVeri Diagnostic Suite",
            ttl_minutes=10,
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.smtp_user,
            password=settings.smtp_password,
            from_email=settings.smtp_from_email,
            from_name=settings.smtp_from_name,
            use_tls=settings.smtp_use_tls,
            relay_label="Primary SMTP",
        )
        if ok:
            print(f"✅ SUCCESS! Primary email transmitted to {to_email} with OTP: 123456")
            print("📬 Please check your inbox (and Spam/Junk folder) to verify receipt.")
            return 0
        else:
            print(f"❌ FAILED on Primary SMTP ({settings.smtp_host}:{settings.smtp_port})!")
            print(f"   Error: {err}")
            return 1

    else:
        print("🚀 Running full end-to-end OTP dispatch with automatic failover...")
        send_result = await email_service.generate_and_send_otp(
            user_id=999999999,
            student_id=args.student_id,
            email_address=to_email,
            server_name="TARVeri Diagnostic Suite",
        )

        if send_result["success"]:
            pending = email_service.get_pending_otp(999999999)
            otp_code = pending.otp_code if pending else "******"
            print(f"✅ SUCCESS! Verification email transmitted to {to_email}")
            print(f"🔢 Generated Test OTP : {otp_code}")
            print(f"⏱️ Code TTL           : {send_result['ttl_seconds']} seconds")
            print("📬 Please check your inbox (and Spam/Junk folder) for the dark-mode HTML email.")
            return 0
        else:
            print(f"❌ FAILED: {send_result['error']}")
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
