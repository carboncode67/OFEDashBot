"""
OFEDashBot — Shared Utilities v11
===================================
Functions used by whatsapphook.py, processinggui.py, and admingui.py.
Centralizes logic to avoid duplication and inconsistency.

Changes in v9:
- Single slot system (one Twilio number, one experiment per farmer)
- Phone number normalization strips country code prefix for matching
- Experiment identified by farmer phone + OFEDashboard UUID
- MIME type validation using file headers
- Allowed file extension whitelist
"""

import os
import json
import datetime
import httpx
from pathlib import Path
from config import settings

BASE_DIR = settings.base_dir
ADMIN_DIR = f"{BASE_DIR}/admin"
TRASH_DIR = f"{BASE_DIR}/trash"

# ---------------------------------------------------------------------------
# Onboarding message
# To edit: change the text between the triple quotes below.
# ---------------------------------------------------------------------------
ONBOARDING_MESSAGE = """Welcome to the OFE Data Lab! This number is your direct link to our research team. Use it to document your on-farm experiment — send field observations, photos, voice notes, videos, soil reports, location pins, or anything else relevant to your trial.

Important: please do not send sensitive personal or financial information through this channel (government IDs, banking details, medical records, passwords, or anything strictly private).

Technical note: WhatsApp and our messaging provider Twilio are required to deliver your messages to us. As a result, they have incidental technical access to message content as part of how their infrastructure operates. This is a limitation of using WhatsApp as a communication channel, not a choice we made to share your data. We do not share your information with any other party.

Your participation is voluntary. Contact us anytime at farmersdatalab@gmail.com with questions.

Reply with your name to confirm you are ready to start."""

# ---------------------------------------------------------------------------
# Allowed file extensions — whitelist approach
# Files with extensions not in this set are rejected regardless of
# what content-type the sender claims.
# ---------------------------------------------------------------------------
ALLOWED_EXTENSIONS = {
    # Images
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif",
    # Audio
    ".ogg", ".mp3", ".m4a", ".amr", ".wav", ".aac", ".3gp",
    # Video
    ".mp4", ".mov", ".mpeg", ".webm",
    # Documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    # Contacts
    ".vcf",
}

# ---------------------------------------------------------------------------
# MIME type signatures — first bytes of known file types
# Used to validate that the file content matches the claimed type.
# This catches cases where someone renames a .exe to .jpg.
# ---------------------------------------------------------------------------
MIME_SIGNATURES = {
    # Images
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
    b"GIF8": "image/gif",
    b"RIFF": "image/webp",  # WebP starts with RIFF....WEBP
    # Audio
    b"OggS": "audio/ogg",
    b"ID3": "audio/mpeg",
    b"\xff\xfb": "audio/mpeg",
    b"\xff\xf3": "audio/mpeg",
    b"fLaC": "audio/flac",
    # Video / container (MP4, M4A, MOV all use ISO base media format)
    # These are detected by bytes 4-8 being 'ftyp'
    # Documents
    b"%PDF": "application/pdf",
    b"PK\x03\x04": "application/zip",  # DOCX, XLSX, PPTX are ZIP-based
    b"\xd0\xcf\x11\xe0": "application/msoffice",  # Legacy .doc, .xls, .ppt
    # vCard
    b"BEGIN:VCARD": "text/vcard",
}

# Content types that are ZIP-based (Office Open XML formats)
ZIP_BASED_OFFICE = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def validate_file_content(content: bytes, claimed_content_type: str, ext: str) -> bool:
    """
    Validate that uploaded file content matches the claimed type.

    This is a defence-in-depth measure. WhatsApp/Twilio already validate
    files on their end, but we add a check here to catch edge cases.

    Returns True if the file appears legitimate, False if suspicious.
    """
    # Extension must be in whitelist
    if ext.lower() not in ALLOWED_EXTENSIONS:
        return False

    # Need at least a few bytes to check
    if len(content) < 4:
        return False

    header = content[:12]

    # PDF check
    if ext.lower() == ".pdf":
        return content[:4] == b"%PDF"

    # ZIP-based Office formats (docx, xlsx, pptx)
    if ext.lower() in (".docx", ".xlsx", ".pptx"):
        return content[:4] == b"PK\x03\x04"

    # Legacy Office formats
    if ext.lower() in (".doc", ".xls", ".ppt"):
        return content[:4] == b"\xd0\xcf\x11\xe0"

    # JPEG
    if ext.lower() in (".jpg", ".jpeg"):
        return content[:3] == b"\xff\xd8\xff"

    # PNG
    if ext.lower() == ".png":
        return content[:4] == b"\x89PNG"

    # GIF
    if ext.lower() == ".gif":
        return content[:4] in (b"GIF8", b"GIF9")

    # MP4/MOV/M4A — ISO Base Media File Format
    # Bytes 4-7 are 'ftyp' in these formats
    if ext.lower() in (".mp4", ".mov", ".m4a"):
        return len(content) > 8 and content[4:8] == b"ftyp"

    # OGG (audio)
    if ext.lower() == ".ogg":
        return content[:4] == b"OggS"

    # MP3
    if ext.lower() == ".mp3":
        return content[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")

    # vCard
    if ext.lower() == ".vcf":
        try:
            return content[:12].decode("utf-8", errors="ignore").startswith("BEGIN:VCARD")
        except Exception:
            return False

    # For other types we don't have a signature check — allow but log
    return True


# ---------------------------------------------------------------------------
# Safe path validation — prevents path traversal attacks
# ---------------------------------------------------------------------------

def is_safe_path(requested_path: str) -> bool:
    """
    Verify a requested file path stays within BASE_DIR.
    Prevents path traversal attacks like ../../etc/passwd
    where an attacker supplies a crafted path to escape the
    intended directory.
    """
    try:
        resolved = str(Path(requested_path).resolve())
        base_resolved = str(Path(BASE_DIR).resolve())
        return resolved.startswith(base_resolved)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ticket system
# ---------------------------------------------------------------------------

def excel_serial_date() -> int:
    """
    Return today's date as an Excel serial number.
    Excel counts days since January 1, 1900.
    This gives a compact 5-digit date identifier (e.g. 46164 for May 2026).
    To convert back: datetime.date(1899, 12, 30) + timedelta(days=N)
    """
    return (datetime.date.today() - datetime.date(1899, 12, 30)).days


def generate_ticket() -> str:
    """
    Generate the next ticket number in the format EXCELDATESERIAL-NNNN.
    Example: 46164-0005

    The counter resets to 0001 each new day. The current day and counter
    are stored in received/tickets/counter.txt as "SERIAL|COUNTER".
    Thread safety note: this is not atomic — concurrent calls on the same
    millisecond could theoretically generate duplicate tickets. Acceptable
    for pilot scale. Fix in Phase 2: use database auto-increment.
    """
    serial = excel_serial_date()
    counter_file = f"{BASE_DIR}/tickets/counter.txt"
    today_str = str(serial)
    counter = 1
    if os.path.exists(counter_file):
        try:
            with open(counter_file, "r") as f:
                parts = f.read().strip().split("|")
                if len(parts) == 2 and parts[0] == today_str:
                    counter = int(parts[1]) + 1
        except (ValueError, IOError):
            counter = 1
    with open(counter_file, "w") as f:
        f.write(f"{today_str}|{counter}")
    return f"{serial}-{counter:04d}"


def log_ticket(ticket: str, status: str, detail: str):
    """
    Append a status change to the ticket log.
    The ticket log is the single source of truth for ticket lifecycle.
    Current status is determined by reading the most recent entry
    for a given ticket ID.
    Format: TIMESTAMP | TICKET | STATUS | detail
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    entry = f"{timestamp} | {ticket} | {status:<25} | {detail}\n"
    with open(f"{BASE_DIR}/tickets/ticket_log.txt", "a") as f:
        f.write(entry)


def log_activity(timestamp: str, phone: str, data_type: str, detail: str, status: str):
    """
    Append a line to the unified activity log.
    This log records every interaction across all farmers and data types.
    It is separate from the ticket log — the ticket log tracks lifecycle,
    the activity log tracks raw events.
    """
    entry = f"{timestamp} | {phone} | {data_type.upper():12} | {status:7} | {detail}\n"
    with open(f"{BASE_DIR}/activity_log.txt", "a") as f:
        f.write(entry)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def get_farmers() -> list:
    """
    Load the farmer/agronomist registry from admin/farmers.json.
    Each entry contains phone, name, role, and experiment assignments.
    Only registered numbers are allowed to submit data.
    """
    path = f"{ADMIN_DIR}/farmers.json"
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return []


def save_farmers(farmers: list):
    with open(f"{ADMIN_DIR}/farmers.json", "w") as f:
        json.dump(farmers, f, indent=2)


def get_students() -> dict:
    """
    Load the student roster from admin/students.json.
    Only active students can log into the processing GUI.
    """
    path = f"{ADMIN_DIR}/students.json"
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "LAB01": {"name": "Lab Admin", "active": True},
        "ST01":  {"name": "Student 1", "active": True},
        "ST02":  {"name": "Student 2", "active": True},
        "ST03":  {"name": "Student 3", "active": True},
    }


def save_students(students: dict):
    with open(f"{ADMIN_DIR}/students.json", "w") as f:
        json.dump(students, f, indent=2)


def find_farmer(phone: str) -> dict | None:
    """
    Look up a farmer by phone number in the registry.
    Normalizes phone numbers by stripping +, spaces, and leading country
    code (1 for US/Canada) before comparing. This handles mismatches where
    the farmer is stored as "6072299837" but Twilio sends "16072299837".
    Returns the farmer dict if found, None if not registered.
    Unknown numbers are silently rejected by the webhook.
    """
    def normalize(p: str) -> str:
        p = p.replace("+", "").replace(" ", "").strip()
        # Strip leading country code 1 for US/Canada numbers
        # A US number without country code is 10 digits
        # With country code it is 11 digits starting with 1
        if len(p) == 11 and p.startswith("1"):
            p = p[1:]
        return p

    clean = normalize(phone)
    for f in get_farmers():
        fphone = normalize(f.get("phone", ""))
        if fphone == clean:
            return f
    return None


def get_experiment_name(farmer: dict, slot: str = "1") -> str:
    """
    Return the experiment name for a farmer.
    In single-slot mode, returns the first experiment assignment found.
    Falls back to farmer name if no assignment found.
    """
    experiments = farmer.get("experiments", [])
    if experiments:
        # Try slot match first for backwards compatibility
        for exp in experiments:
            if str(exp.get("slot")) == str(slot):
                return exp.get("name", farmer.get("name", "Unknown"))
        # Fall back to first experiment
        return experiments[0].get("name", farmer.get("name", "Unknown"))
    return farmer.get("name", "Unknown")


def get_experiment_id(farmer: dict) -> str:
    """
    Return the OFEDashboard experiment UUID for a farmer.
    Returns empty string if not set.
    """
    experiments = farmer.get("experiments", [])
    if experiments:
        return experiments[0].get("experiment_id", "")
    return ""


def find_file_for_ticket(ticket: str) -> str | None:
    """
    Find the media or document file associated with a ticket number.
    Files are named starting with their ticket number so this is a
    simple prefix search across the media and documents folders.
    Returns the full path or None if not found.
    """
    for folder in ("media", "documents"):
        path = f"{BASE_DIR}/{folder}"
        if os.path.exists(path):
            for fname in os.listdir(path):
                if fname.startswith(ticket) and not fname.endswith(".txt"):
                    full = os.path.join(path, fname)
                    if is_safe_path(full):
                        return full
    return None


def read_sidecar(base_filename: str) -> dict:
    """
    Read the caption/location sidecar file for a media file.
    Sidecar files have the same name as the media file but with .txt extension.
    They contain structured key: value pairs for caption, coordinates, etc.
    Returns an empty dict if no sidecar exists.
    """
    sidecar_path = os.path.splitext(base_filename)[0] + ".txt"
    result = {}
    if not os.path.exists(sidecar_path) or not is_safe_path(sidecar_path):
        return result
    try:
        with open(sidecar_path, "r") as f:
            for line in f:
                if ":" in line:
                    key, _, val = line.partition(":")
                    result[key.strip()] = val.strip()
    except IOError:
        pass
    return result


def read_log_tail(filepath: str, lines: int = 200) -> list:
    """Read the last N lines of a log file. Used by the Admin GUI log viewer."""
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r") as f:
            all_lines = f.readlines()
        return [l.strip() for l in all_lines[-lines:]]
    except IOError:
        return []


def soft_delete_file(path: str) -> bool:
    """
    Move a file to the trash folder instead of permanently deleting it.
    Files in trash/ can be recovered manually if needed.
    Also moves the associated sidecar .txt file if one exists.
    Returns True if successful, False if path is unsafe or operation fails.
    """
    if not is_safe_path(path):
        return False
    if not os.path.exists(path):
        return False
    try:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.basename(path)
        trash_path = f"{TRASH_DIR}/{timestamp}_{filename}"
        os.rename(path, trash_path)
        # Also move the sidecar if it exists
        sidecar = os.path.splitext(path)[0] + ".txt"
        if os.path.exists(sidecar):
            os.rename(sidecar, os.path.splitext(trash_path)[0] + ".txt")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Onboarding status tracking
# ---------------------------------------------------------------------------

def get_onboarding_status(phone: str) -> dict:
    """
    Return the onboarding status for a farmer phone number.
    Stored in received/admin/onboarding.json.

    Status values:
        not_sent    — farmer added but onboarding message not sent yet
        sent        — message sent, waiting for farmer reply
        confirmed   — farmer replied, onboarding complete
        failed      — delivery failed (farmer may not have WhatsApp)
    """
    path = f"{ADMIN_DIR}/onboarding.json"
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return data.get(phone, {"status": "not_sent", "timestamp": None})
        except (json.JSONDecodeError, IOError):
            pass
    return {"status": "not_sent", "timestamp": None}


def set_onboarding_status(phone: str, status: str, detail: str = ""):
    """Update onboarding status for a farmer in onboarding.json."""
    path = f"{ADMIN_DIR}/onboarding.json"
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            data = {}
    data[phone] = {
        "status": status,
        "timestamp": datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
        "detail": detail
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Twilio outbound messaging
# ---------------------------------------------------------------------------

async def send_whatsapp(to_phone: str, message: str) -> bool:
    """
    Send a WhatsApp message via Twilio REST API.
    Returns True if delivered successfully, False on any error.
    Used for confirmations, clarifications, and admin notifications.

    Phone normalization: strips non-digits, then ensures a leading 1 for
    US numbers (10 digits). This makes the system resilient to phone numbers
    stored with or without the country code.
    """
    # Normalize: strip non-digits
    clean = "".join(c for c in to_phone if c.isdigit())
    # Ensure leading 1 for 10-digit US numbers
    if len(clean) == 10:
        clean = "1" + clean
    if not clean:
        print(f"[WARN] Invalid phone number: {to_phone}")
        return False
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        print(f"[WARN] Twilio credentials not configured. Cannot send to {clean}")
        return False
    try:
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Messages.json"
        )
        data = {
            "From": settings.twilio_whatsapp_number,
            "To": f"whatsapp:+{clean}",
            "Body": message,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, data=data,
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            )
            response.raise_for_status()
        print(f"Sent WhatsApp to {to_phone}: {message[:60]}")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to send WhatsApp to {to_phone}: {e}")
        return False


async def send_onboarding(phone: str) -> dict:
    """
    Send the onboarding message to a newly registered farmer.
    Updates the onboarding status in onboarding.json based on result.
    Called from Admin GUI when the Onboard button is clicked.
    """
    clean_phone = phone.replace("+", "").replace(" ", "")
    success = await send_whatsapp(clean_phone, ONBOARDING_MESSAGE)
    if success:
        set_onboarding_status(clean_phone, "sent")
        return {"ok": True, "message": "Onboarding message sent successfully."}
    else:
        set_onboarding_status(clean_phone, "failed",
                             "Twilio could not deliver — farmer may not have WhatsApp")
        return {
            "ok": False,
            "message": (
                "Failed to send. The farmer may not have WhatsApp installed, "
                "or the phone number may be incorrect."
            )
        }


async def notify_admin(message: str):
    """
    Send a status notification to the lab admin phone.
    Used for startup/shutdown alerts and critical errors.
    """
    if settings.admin_phone:
        await send_whatsapp(settings.admin_phone, f"[OFEDashBot] {message}")
