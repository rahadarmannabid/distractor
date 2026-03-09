"""OTP generation, hashing, verification, and email delivery.

Uses Firebase for storage (consistent with the rest of the project).
Includes a pluggable provider abstraction and a safe dev-mode fallback.
"""

import hashlib
import logging
import os
import random
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# ─── Firebase key sanitiser (same helper used elsewhere) ────────────────────

def sanitize_key(key: str) -> str:
    return re.sub(r'[.#$\[\]/:]', '_', key)


# ─── Hashing ────────────────────────────────────────────────────────────────

def _hash_otp(otp: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{otp}".encode()).hexdigest()


# ─── Firebase OTP storage ───────────────────────────────────────────────────

def store_otp(firebase_db, email: str, otp_hash: str, salt: str, expiry: datetime):
    """Store hashed OTP in Firebase with expiry and zero attempts."""
    if not firebase_db:
        logger.warning("Firebase unavailable — OTP not persisted")
        return
    from firebase_admin import db
    sanitized = sanitize_key(email.lower().strip())
    db.reference(f"otps/{sanitized}").set({
        "otp_hash": otp_hash,
        "salt": salt,
        "expires": expiry.isoformat(),
        "attempts": 0,
        "created_at": datetime.utcnow().isoformat(),
    })


def get_otp(firebase_db, email: str) -> Optional[Dict[str, Any]]:
    if not firebase_db:
        return None
    from firebase_admin import db
    sanitized = sanitize_key(email.lower().strip())
    data = db.reference(f"otps/{sanitized}").get()
    if not data:
        return None
    return {
        "otp_hash": data.get("otp_hash", ""),
        "salt": data.get("salt", ""),
        "expires": datetime.fromisoformat(data["expires"]),
        "attempts": data.get("attempts", 0),
    }


def increment_otp_attempts(firebase_db, email: str, new_count: int):
    if not firebase_db:
        return
    from firebase_admin import db
    sanitized = sanitize_key(email.lower().strip())
    db.reference(f"otps/{sanitized}/attempts").set(new_count)


def delete_otp(firebase_db, email: str):
    if not firebase_db:
        return
    from firebase_admin import db
    sanitized = sanitize_key(email.lower().strip())
    db.reference(f"otps/{sanitized}").delete()


# ─── OTP lifecycle ──────────────────────────────────────────────────────────

OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 5
OTP_MAX_ATTEMPTS = 5


def generate_otp() -> tuple[str, str, str]:
    """Return (plaintext_otp, otp_hash, salt)."""
    otp = "".join(str(random.randint(0, 9)) for _ in range(OTP_LENGTH))
    salt = hashlib.sha256(os.urandom(16)).hexdigest()[:16]
    otp_hash = _hash_otp(otp, salt)
    return otp, otp_hash, salt


def verify_otp_value(provided_otp: str, stored_hash: str, salt: str) -> bool:
    return _hash_otp(provided_otp.strip(), salt) == stored_hash


# ─── Email provider abstraction ─────────────────────────────────────────────

class OTPEmailProvider:
    """Base class for OTP delivery. Subclass to plug in SendGrid, SES, etc."""

    def send(self, recipient: str, otp: str) -> bool:
        raise NotImplementedError


class SMTPEmailProvider(OTPEmailProvider):
    """Send OTP via SMTP (Gmail-compatible)."""

    def __init__(self):
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.email_address = os.getenv("EMAIL_ADDRESS", "")
        self.email_password = os.getenv("EMAIL_PASSWORD", "")

    def send(self, recipient: str, otp: str) -> bool:
        if not self.email_address or not self.email_password:
            logger.error("SMTP credentials missing (EMAIL_ADDRESS / EMAIL_PASSWORD)")
            return False
        try:
            msg = MIMEMultipart()
            msg["From"] = self.email_address
            msg["To"] = recipient
            msg["Subject"] = "Your OTP Code - Instructor Login"

            body = f"""
            <html>
            <body style="font-family: Arial, sans-serif; padding: 20px; background-color: #f4f4f4;">
                <div style="max-width: 600px; margin: 0 auto; background-color: white; padding: 30px; border-radius: 10px;">
                    <h2 style="color: #58a6ff;">Instructor Login OTP</h2>
                    <p>Your One-Time Password (OTP) for instructor login is:</p>
                    <div style="background-color: #0d1117; color: #58a6ff; font-size: 32px; font-weight: bold;
                                padding: 20px; text-align: center; border-radius: 8px; letter-spacing: 8px;">
                        {otp}
                    </div>
                    <p style="margin-top: 20px; color: #666;">This code is valid for <strong>{OTP_EXPIRY_MINUTES} minutes</strong>.</p>
                    <p style="color: #999; font-size: 12px; margin-top: 30px;">
                        If you didn't request this code, please ignore this email.
                    </p>
                </div>
            </body>
            </html>
            """
            msg.attach(MIMEText(body, "html"))

            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.email_address, self.email_password)
            server.send_message(msg)
            server.quit()

            logger.info(f"OTP email sent to {recipient}")
            return True
        except Exception as e:
            logger.error(f"Failed to send OTP email to {recipient}: {e}")
            return False


class DevFallbackProvider(OTPEmailProvider):
    """Development-only provider that logs OTP to console.

    Activated when OTP_DEV_MODE=true.  NEVER use in production.
    """

    def send(self, recipient: str, otp: str) -> bool:
        logger.warning("=" * 60)
        logger.warning(f"[DEV MODE] OTP for {recipient}: {otp}")
        logger.warning("=" * 60)
        return True


def get_otp_provider() -> OTPEmailProvider:
    """Return the appropriate OTP email provider based on configuration."""
    if os.getenv("OTP_DEV_MODE", "false").lower() in ("true", "1", "yes"):
        logger.warning("OTP dev-mode fallback active — OTPs printed to console only")
        return DevFallbackProvider()
    return SMTPEmailProvider()


# ─── Domain validation ──────────────────────────────────────────────────────

def is_email_domain_allowed(email: str) -> bool:
    """Check if the email domain is in the allowlist or is a .edu domain."""
    domain = email.lower().strip().split("@")[-1]
    allowed_raw = os.getenv("ALLOWED_EMAIL_DOMAINS", "")
    allowed_domains = [d.strip().lower() for d in allowed_raw.split(",") if d.strip()]
    allow_any_edu = os.getenv("ALLOW_ANY_EDU", "false").strip().lower() in ("true", "1", "yes")

    if allow_any_edu and domain.endswith(".edu"):
        return True
    if domain in allowed_domains:
        return True
    # If no restrictions configured, allow all
    if not allowed_domains and not allow_any_edu:
        return True
    return False
