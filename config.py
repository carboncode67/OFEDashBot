"""
OFEDashBot — Central Configuration v11
========================================
All settings loaded from .env file.
Never hardcode credentials in any other file.

Usage:
    from config import settings
    print(settings.twilio_account_sid)
"""

from pydantic_settings import BaseSettings
from pathlib import Path
import os
import sys

# Single source of truth for the project version.
# Referenced in service docstrings and the /health endpoint.
VERSION = "11.0.0"


class Settings(BaseSettings):
    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_number: str = "whatsapp:+14155238886"

    # Admin
    admin_phone: str = ""
    admin_password: str = "admin"

    # Session — used to sign admin session cookies server-side
    # Generate a strong random value: python3 -c "import secrets; print(secrets.token_hex(32))"
    session_secret: str = "change-this-secret-before-deployment"

    # Security
    # Set to true once deployed. False only during local development.
    validate_twilio_signature: bool = False
    max_upload_mb: int = 64

    # Storage
    # Runtime flat-file state (ticket counter, logs, onboarding status).
    # Defaults to a directory OUTSIDE the repo so these files are never
    # committed to git. Override with BASE_DIR in .env if needed.
    base_dir: str = str(Path.home() / "ofedashbot-data")

    # MMS phone number (Twilio, for native iPhone/Android messaging)
    mms_phone_number: str = "+16078826071"

    # FDL backend integration
    # Base URL of the FDL Dashboard API that receives forwarded submissions.
    fdl_api_url: str = "http://localhost:3000"
    # Optional service token for OFEDashBot's own reads (e.g. the contacts list).
    fdl_service_token: str = ""

    # Environment
    environment: str = "development"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()

# ---------------------------------------------------------------------------
# Startup safety checks
# ---------------------------------------------------------------------------

def check_startup_safety():
    """
    Refuse to start in production with insecure defaults.
    This prevents accidentally deploying with weak credentials.
    """
    warnings = []
    errors = []

    if settings.is_production:
        if settings.admin_password == "admin":
            errors.append(
                "ADMIN_PASSWORD is still set to 'admin'. "
                "Change it in .env before running in production."
            )
        if settings.session_secret == "change-this-secret-before-deployment":
            errors.append(
                "SESSION_SECRET has not been changed. "
                "Generate one with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
            )
        if not settings.validate_twilio_signature:
            errors.append(
                "VALIDATE_TWILIO_SIGNATURE is false in production. "
                "Anyone can forge webhook requests. Set it to true in .env."
            )
        if not settings.twilio_account_sid:
            errors.append("TWILIO_ACCOUNT_SID is not set.")
        if not settings.twilio_auth_token:
            errors.append("TWILIO_AUTH_TOKEN is not set.")

    if warnings:
        for w in warnings:
            print(f"[WARN] {w}")

    if errors:
        print("\n[ERROR] Cannot start — fix these issues in .env:\n")
        for e in errors:
            print(f"  - {e}")
        print()
        sys.exit(1)


# Create all required directories on startup
for folder in ("media", "documents", "spatial", "contacts",
               "tickets", "admin", "trash"):
    Path(f"{settings.base_dir}/{folder}").mkdir(parents=True, exist_ok=True)
