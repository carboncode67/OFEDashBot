"""
OFEDashBot — WhatsApp Webhook (v11)
==========================================
Receives WhatsApp messages from registered farmers via Twilio and forwards
them to the FDL Dashboard backend API. Submissions now flow to PostgreSQL via
the FDL backend (fdl_client) instead of the legacy received/ flat files.

NOTE: the downstream tools (processinggui.py, weekly_report.py,
admingui_desktop.py) still read the received/ flat files and are not yet wired
to the FDL backend. See README.md "Migration status" before relying on them.

Unknown phone numbers receive absolute silence: no reply, no error, nothing.

Single-slot system: one Twilio number. The farmer phone number identifies the
experiment, linked to a Contact row in the FDL backend.

To run:
    python3 -m uvicorn whatsapphook:app --port 8001

Webhook endpoint:
    /webhook/1   (primary, set this in Twilio)
    /webhook     (alias, same handler)
"""

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import PlainTextResponse, Response
from contextlib import asynccontextmanager
from typing import Optional
import os
import html
import datetime
import httpx
import re
import io

from config import settings, check_startup_safety, VERSION
import fdl_client

# Shared utilities still used: generate_ticket (for logging/confirmation),
# notify_admin, send_whatsapp, find_farmer, get_experiment_name,
# get_experiment_id, set_onboarding_status, get_onboarding_status.
# Flat-file helpers (log_ticket, log_activity, save_sidecar, etc.) are
# no longer called — storage now goes through fdl_client.
from shared import (
    generate_ticket, send_whatsapp, notify_admin,
    set_onboarding_status, get_onboarding_status,
)


# ---------------------------------------------------------------------------
# Twilio signature validation
# ---------------------------------------------------------------------------

def validate_twilio_signature(request_url: str, params: dict, signature: str) -> bool:
    if not settings.validate_twilio_signature:
        return True
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(settings.twilio_auth_token)
        return validator.validate(request_url, params, signature)
    except ImportError:
        print("[WARN] twilio package not installed — skipping signature validation")
        return True
    except Exception as e:
        print(f"[ERROR] Signature validation error: {e}")
        return False


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

_start_time = datetime.datetime.now()


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_startup_safety()
    print(f"OFEDashBot webhook v{VERSION} starting — environment: {settings.environment}")
    await notify_admin(
        f"Webhook service started. Environment: {settings.environment}."
    )
    yield
    print("OFEDashBot webhook shutting down")
    await notify_admin("Webhook service stopped. Farmers cannot submit until it restarts.")


app = FastAPI(lifespan=lifespan)

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# ---------------------------------------------------------------------------
# Extension map and type labels
# ---------------------------------------------------------------------------

EXTENSION_MAP = {
    "image/jpeg":   (".jpg",  "photo"),
    "image/jpg":    (".jpg",  "photo"),
    "image/png":    (".png",  "photo"),
    "image/webp":   (".webp", "photo"),
    "image/heic":   (".heic", "photo"),
    "image/heif":   (".heif", "photo"),
    "image/gif":    (".gif",  "photo"),
    "audio/ogg":    (".ogg",  "recording"),
    "audio/mpeg":   (".mp3",  "recording"),
    "audio/mp4":    (".m4a",  "recording"),
    "audio/amr":    (".amr",  "recording"),
    "audio/wav":    (".wav",  "recording"),
    "audio/x-wav":  (".wav",  "recording"),
    "audio/aac":    (".aac",  "recording"),
    "audio/3gpp":   (".3gp",  "recording"),
    "video/mp4":    (".mp4",  "video"),
    "video/3gpp":   (".3gp",  "video"),
    "video/quicktime": (".mov","video"),
    "video/mpeg":   (".mpeg", "video"),
    "video/webm":   (".webm", "video"),
    "application/pdf": (".pdf", "document"),
    "application/msword": (".doc", "document"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx", "document"),
    "application/vnd.ms-excel": (".xls", "document"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (".xlsx", "document"),
    "application/vnd.ms-powerpoint": (".ppt", "document"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (".pptx", "document"),
    "text/vcard":   (".vcf",  "contact"),
    "text/x-vcard": (".vcf",  "contact"),
}

# Types that go to the geotag window (hold until location or window expires)
MEDIA_TYPES = ("photo", "recording", "video")

GEOREFERENCE_WINDOW_MINUTES = 5
LOCATION_CAPTION_WINDOW_MINUTES = 5
CONTACT_NOTE_WINDOW_MINUTES = 5

# ---------------------------------------------------------------------------
# Session state (in-memory; Phase 2 will persist to JSON)
# ---------------------------------------------------------------------------

pending_georeference:     dict = {}
pending_location_caption: dict = {}
pending_clarification:    dict = {}
pending_contact_note:     dict = {}

# Geotag window bookkeeping: phone -> {files, tickets, since, located, lat, lon}.
# Media is NEVER buffered here; it is posted to FDL on arrival. This window only
# tracks which recently-saved tickets an optional follow-up location can geotag.
# Each dict: {content_type, file_bytes, filename, caption, ticket, timestamp}
pending_media_hold:       dict = {}


# ---------------------------------------------------------------------------
# Session helpers (unchanged from v11)
# ---------------------------------------------------------------------------

def is_within_window(since: datetime.datetime, minutes: int) -> bool:
    return (datetime.datetime.now() - since).total_seconds() < minutes * 60


def add_to_georeference_window(phone: str, filename: str, ticket: str, slot: str):
    now = datetime.datetime.now()
    if phone in pending_georeference and is_within_window(
        pending_georeference[phone]["since"], GEOREFERENCE_WINDOW_MINUTES
    ):
        pending_georeference[phone]["files"].append(filename)
        pending_georeference[phone]["tickets"].append(ticket)
    else:
        pending_georeference[phone] = {
            "files": [filename], "tickets": [ticket],
            "slot": slot, "since": now
        }


def get_georeference_window(phone: str) -> dict | None:
    if phone in pending_georeference and is_within_window(
        pending_georeference[phone]["since"], GEOREFERENCE_WINDOW_MINUTES
    ):
        return pending_georeference[phone]
    return None


def clear_georeference_window(phone: str):
    pending_georeference.pop(phone, None)
    pending_media_hold.pop(phone, None)


def set_location_caption_window(phone: str, ticket: str):
    pending_location_caption[phone] = {
        "ticket": ticket,
        "since": datetime.datetime.now()
    }


def get_location_caption_window(phone: str) -> dict | None:
    if phone in pending_location_caption and is_within_window(
        pending_location_caption[phone]["since"], LOCATION_CAPTION_WINDOW_MINUTES
    ):
        return pending_location_caption[phone]
    return None


def clear_location_caption_window(phone: str):
    pending_location_caption.pop(phone, None)


def set_contact_note_window(phone: str, ticket: str):
    pending_contact_note[phone] = {
        "ticket": ticket,
        "since": datetime.datetime.now()
    }


def get_contact_note_window(phone: str) -> dict | None:
    if phone in pending_contact_note and is_within_window(
        pending_contact_note[phone]["since"], CONTACT_NOTE_WINDOW_MINUTES
    ):
        return pending_contact_note[phone]
    return None


def clear_contact_note_window(phone: str):
    pending_contact_note.pop(phone, None)


# ---------------------------------------------------------------------------
# vCard parser (unchanged)
# ---------------------------------------------------------------------------

def parse_vcard(raw: str) -> dict:
    contact = {"name": "n/a", "phone": "n/a", "org": "n/a", "email": "n/a"}
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("FN:"):
            contact["name"] = line[3:][:200]
        elif line.startswith("TEL") and ":" in line:
            contact["phone"] = line.split(":")[-1][:20]
        elif line.startswith("ORG:"):
            contact["org"] = line[4:][:200]
        elif line.startswith("EMAIL") and ":" in line:
            contact["email"] = line.split(":")[-1][:200]
    return contact


# ---------------------------------------------------------------------------
# Silent response
# ---------------------------------------------------------------------------

SILENT_RESPONSE = PlainTextResponse(
    content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
    media_type="text/xml"
)


# ---------------------------------------------------------------------------
# Core message handler
# ---------------------------------------------------------------------------

async def handle_message(
    slot: str,
    From: str,
    Body: str,
    NumMedia: int,
    MediaUrl0: Optional[str],
    MediaContentType0: Optional[str],
    MessageSid: str,
    SentAt: Optional[str],
    Latitude: Optional[str],
    Longitude: Optional[str],
    Address: Optional[str],
    Label: Optional[str],
) -> PlainTextResponse:

    received_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    farmer_timestamp   = SentAt or received_timestamp
    phone   = From.replace("whatsapp:", "").replace("+", "").strip()[:15]
    caption = Body.strip()[:2000]

    # --- Resolve phone to FDL contact (single source of truth) ---
    fdl_contact = await fdl_client.fdl_resolve_token(phone)
    if not fdl_contact:
        print(f"[SILENT] Unknown phone ignored: {phone}")
        return SILENT_RESPONSE
    fdl_token = fdl_contact["token"]

    # --- Onboarding confirmation ---
    onboarding = get_onboarding_status(phone)
    if onboarding.get("status") == "sent":
        set_onboarding_status(phone, "confirmed",
                             f"First message received: {caption[:60] or '[media]'}")
        print(f"[INFO] Onboarding confirmed for {phone}")

    experiment_name = fdl_contact.get("experiment_name") or "Unknown"
    ticket          = generate_ticket()

    print(f"Message from {phone} | slot={slot} | exp={experiment_name} | ticket={ticket}")

    # --- Clarification reply ---
    clarif = pending_clarification.get(phone)
    if clarif and not Latitude and not Longitude and NumMedia == 0 and caption:
        if caption.upper() == "YES" and "candidate" in clarif:
            pending_clarification.pop(phone, None)
            return _twiml(f"✅ Got it! Answer recorded for ticket {clarif['ticket']}. Thank you!")
        elif caption.upper() == "NO":
            pending_clarification.pop(phone, None)
            await send_whatsapp(phone,
                f"OK, we will keep waiting for your answer to: {clarif['question']}")
        else:
            pending_clarification[phone]["candidate"] = caption
            return _twiml(
                f"❓ Is this your answer to our question about your recent submission?\n\n"
                f'"{clarif["question"]}"\n\n'
                f"Reply YES to confirm or NO if this is a new observation."
            )

    # --- 1. Location pin ---
    if Latitude and Longitude:
        try:
            lat = float(Latitude)
            lon = float(Longitude)
        except ValueError:
            return _twiml("⚠️ Could not read your location. Please try sharing it again.")

        geo_window = get_georeference_window(phone)

        if geo_window:
            # Media in this window was ALREADY saved to the database when it
            # arrived. We only record the location and associate it with the
            # same ticket(s); we do not re-post the files.
            linked_tickets = geo_window.get("tickets", []) or [ticket]
            file_count = len(geo_window.get("files", [])) or len(linked_tickets)
            primary_ticket = linked_tickets[0] if linked_tickets else ticket

            clear_georeference_window(phone)

            await fdl_client.post_location(
                fdl_token,
                name=f"Location for {file_count} file(s)",
                track_data=fdl_client.single_point_track(lat, lon),
                start_time=received_timestamp,
                ticket_ref=primary_ticket,
            )
            return _twiml(
                f"✅ 📍 Location linked to your last {file_count} file(s).\n"
                f"Ticket: {primary_ticket}"
            )

        else:
            # Standalone location pin
            set_location_caption_window(phone, ticket)
            await fdl_client.post_location(
                fdl_token,
                name=Address or f"Location pin {ticket}",
                track_data=fdl_client.single_point_track(lat, lon),
                start_time=received_timestamp,
            )
            return _twiml(
                f"📍 Location recorded. Ticket: {ticket}\n"
                f"Experiment: {experiment_name}\n\n"
                f"📝 Want to add a note? Just reply with your comment."
            )

    # --- 2. Plain text ---
    elif caption and not NumMedia:
        contact_window = get_contact_note_window(phone)
        if contact_window:
            # Note about a previously shared contact
            await fdl_client.post_note(
                fdl_token,
                content=f"[Contact note] {caption}",
                timestamp=received_timestamp,
            )
            clear_contact_note_window(phone)
            return _twiml(
                f"✅ Got it! Note saved for this contact.\n"
                f"Ticket: {contact_window['ticket']}"
            )

        loc_window = get_location_caption_window(phone)
        if loc_window:
            # Caption for a previous location pin
            await fdl_client.post_note(
                fdl_token,
                content=f"[Location note] {caption}",
                timestamp=received_timestamp,
            )
            clear_location_caption_window(phone)
            return _twiml(f"✅ Note added to your location.\nTicket: {loc_window['ticket']}")

        # Plain text observation
        ok = await fdl_client.post_note(
            fdl_token,
            content=caption,
            timestamp=received_timestamp,
        )
        if not ok:
            return _twiml("⚠️ Your message could not be recorded — please contact the program administrator.")
        return _twiml(
            f"✅ Got it! Your message has been recorded.\n"
            f"Experiment: {experiment_name}\n"
            f"Ticket: {ticket}"
        )

    # --- 3. Media, document, or contact ---
    elif NumMedia > 0 and MediaUrl0:
        content_type = (MediaContentType0 or "application/octet-stream").split(";")[0].strip().lower()

        if content_type not in EXTENSION_MAP:
            return _twiml(
                f"⚠️ Your data was not recorded — file type not supported ({content_type}). "
                f"Supported: photos, voice memos, videos, PDF, MS Office, contacts."
            )

        ext, fdl_type = EXTENSION_MAP[content_type]
        fname = f"{ticket}_{received_timestamp}_{phone}{ext}"

        # Download from Twilio
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(
                    MediaUrl0,
                    auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                    follow_redirects=True,
                )
                response.raise_for_status()
                file_bytes = response.content

            if len(file_bytes) > settings.max_upload_bytes:
                return _twiml(
                    f"⚠️ Your data was not recorded — file is too large "
                    f"({len(file_bytes)/1024/1024:.1f} MB). "
                    f"Maximum size is {settings.max_upload_mb} MB."
                )

        except httpx.HTTPStatusError as e:
            print(f"[ERROR] Failed to download media: {e}")
            return _twiml("⚠️ Your data was not recorded — could not download your file — please contact the program administrator.")
        except Exception as e:
            print(f"[ERROR] Media download failed: {e}")
            return _twiml("⚠️ Your data was not recorded — please contact the program administrator.")

        # --- Contact card: post immediately (no geotag needed) ---
        if fdl_type == "contact":
            raw_vcard = file_bytes.decode("utf-8", errors="ignore")
            contact = parse_vcard(raw_vcard)
            ok = await fdl_client.post_contact_card(
                fdl_token,
                name=contact["name"],
                phone=contact["phone"],
                email=contact["email"],
                org=contact["org"],
            )
            if not ok:
                return _twiml("⚠️ Contact could not be saved — please contact the program administrator.")
            set_contact_note_window(phone, ticket)
            return _twiml(
                f"👤 Contact saved.\n"
                f"Name: {contact['name']}\n"
                f"Ticket: {ticket}\n\n"
                f"📝 Who is this person and should they have access to your "
                f"data repository? Reply with a description."
            )

        # --- Document: post immediately (no geotag needed) ---
        if fdl_type == "document":
            ok = await fdl_client.post_document(
                fdl_token, file_bytes, fname,
                note=caption, timestamp=received_timestamp
            )
            if not ok:
                return _twiml("⚠️ Document could not be saved — please contact the program administrator.")
            return _twiml(
                f"✅ Your document has been saved.\n"
                f"Experiment: {experiment_name}\n"
                f"Ticket: {ticket}"
            )

        # --- Photo, recording, video: SAVE IMMEDIATELY ---
        # Media always goes to the database the moment it arrives. Location is
        # optional enrichment, never a precondition for storage. If a location
        # pin was already captured in this window, geotag at upload; otherwise
        # post without coordinates and open a short window so an OPTIONAL pin can
        # be associated afterward.
        geo_window = get_georeference_window(phone)
        lat_val = geo_window.get("lat") if (geo_window and geo_window.get("located")) else None
        lon_val = geo_window.get("lon") if (geo_window and geo_window.get("located")) else None

        if fdl_type == "photo":
            ok = await fdl_client.post_photo(fdl_token, file_bytes, fname,
                latitude=lat_val, longitude=lon_val, note=caption,
                timestamp=received_timestamp, ticket_ref=ticket)
        elif fdl_type == "video":
            ok = await fdl_client.post_video(fdl_token, file_bytes, fname,
                latitude=lat_val, longitude=lon_val, note=caption,
                timestamp=received_timestamp, ticket_ref=ticket)
        elif fdl_type == "recording":
            gps_track = fdl_client.single_point_track(lat_val, lon_val) if lat_val is not None else None
            ok = await fdl_client.post_recording(fdl_token, file_bytes, fname,
                start_time=received_timestamp, gps_track=gps_track,
                ticket_ref=ticket)
        else:
            ok = False

        if not ok:
            return _twiml(
                f"⚠️ Your {fdl_type} could not be saved — please contact the program administrator.\nTicket: {ticket}"
            )

        if lat_val is not None and lon_val is not None:
            return _twiml(
                f"✅ Your {fdl_type} has been saved and geotagged.\n"
                f"Experiment: {experiment_name}\nTicket: {ticket}"
            )

        # Saved without coordinates. Register the ticket so an optional follow-up
        # pin can be linked, but the file is already safely stored.
        add_to_georeference_window(phone, fname, ticket, slot)
        return _twiml(
            f"✅ Your {fdl_type} has been saved.\n"
            f"Experiment: {experiment_name}\nTicket: {ticket}\n\n"
            f"📍 Optional: share a location pin within 5 minutes to geotag it. "
            f"If you don't, your {fdl_type} stays saved without a location."
        )

    else:
        return _twiml(
            "💬 Send a message, 📸 photo, 🎤 voice memo, 🎥 video, 📄 PDF, "
            "Office document, 📍 location, or 👥 contact to record an observation."
        )


# ---------------------------------------------------------------------------
# MMS utilities
# ---------------------------------------------------------------------------

def extract_exif_gps(file_bytes: bytes) -> tuple[float | None, float | None]:
    """
    Extract GPS coordinates from image EXIF data.
    MMS carriers do not strip EXIF, so photos sent via native messaging
    often carry GPS coordinates embedded by the farmer's camera app.
    Returns (latitude, longitude) or (None, None) if not found.
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS
        img = Image.open(io.BytesIO(file_bytes))
        exif_data = img._getexif()
        if not exif_data:
            return None, None
        gps_info = {}
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == "GPSInfo":
                for gps_tag_id, gps_value in value.items():
                    gps_tag = GPSTAGS.get(gps_tag_id, gps_tag_id)
                    gps_info[gps_tag] = gps_value
        if not gps_info:
            return None, None

        def dms_to_decimal(dms, ref):
            d, m, s = float(dms[0]), float(dms[1]), float(dms[2])
            decimal = d + m / 60 + s / 3600
            if ref in ("S", "W"):
                decimal = -decimal
            return decimal

        lat = dms_to_decimal(gps_info["GPSLatitude"],  gps_info.get("GPSLatitudeRef",  "N"))
        lon = dms_to_decimal(gps_info["GPSLongitude"], gps_info.get("GPSLongitudeRef", "E"))
        return lat, lon
    except Exception:
        return None, None


def _valid_latlon(lat: float, lon: float) -> bool:
    """Sanity-check that a parsed pair is a plausible WGS84 coordinate."""
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def extract_coords_from_text(text: str) -> tuple[float | None, float | None]:
    """
    Parse GPS coordinates from a native SMS/MMS message body.

    Handles the common share formats that carry inline coordinates:
      - Apple Maps:   ?ll=lat,lon   and   &coordinate=lat,lon  (iOS sim / older)
      - Apple "place": maps.apple.com/place?coordinate=lat,lon
      - Google Maps:  ?q=lat,lon  /  &query=lat,lon  /  @lat,lon  /  !3dLAT!4dLON
      - geo: URI:     geo:lat,lon
      - Bare pair typed by hand: "42.4440, -76.5019"

    NOTE: iOS 26 short links (https://maps.apple/p/XXXX) contain NO coordinates
    and cannot be parsed here. Those must be resolved by following redirects
    (see resolve_short_map_link) before calling this function, and even then
    Apple often redirects to /unsupported. When this returns (None, None) on a
    map link, the caller should fall back to asking the farmer to type
    coordinates or drop a pin a different way.
    """
    if not text:
        return None, None

    patterns = [
        r'[?&]ll=(-?\d+\.\d+),\s*(-?\d+\.\d+)',          # Apple ?ll=lat,lon
        r'coordinate=(-?\d+\.\d+),\s*(-?\d+\.\d+)',      # Apple place coordinate=
        r'[?&]q=(-?\d+\.\d+),\s*(-?\d+\.\d+)',           # Google ?q=lat,lon
        r'[?&]query=(-?\d+\.\d+),\s*(-?\d+\.\d+)',       # Google ?query=lat,lon
        r'[?&]daddr=(-?\d+\.\d+),\s*(-?\d+\.\d+)',       # Apple directions daddr=
        r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)',               # Google embed !3dLAT!4dLON (precise)
        r'@(-?\d+\.\d+),\s*(-?\d+\.\d+)',                # Google @lat,lon (map center)
        r'geo:(-?\d+\.\d+),\s*(-?\d+\.\d+)',             # geo: URI
        r'(?<![\d.])(-?\d{1,2}\.\d{3,}),\s*(-?\d{1,3}\.\d{3,})(?![\d.])',  # bare pair
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            try:
                lat, lon = float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
            if _valid_latlon(lat, lon):
                return lat, lon
    return None, None


async def resolve_short_map_link(text: str) -> tuple[float | None, float | None]:
    """
    Some map shares arrive as short links that redirect to a URL containing
    coordinates (older goo.gl/maps, maps.app.goo.gl, some maps.apple links).
    Follow redirects once and re-run the text parser on the final URL and on
    any intermediate redirect URLs.

    iOS 26 https://maps.apple/p/... links typically redirect to
    maps.apple.com/unsupported and yield nothing; this function returns
    (None, None) in that case and the caller must handle it gracefully.
    """
    m = re.search(r'https?://[^\s]+', text)
    if not m:
        return None, None
    url = m.group(0)

    # Only bother for known short-link hosts
    if not re.search(r'(goo\.gl|maps\.app\.goo\.gl|maps\.apple/p/|g\.co/)', url):
        return None, None

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            # Try the final URL
            lat, lon = extract_coords_from_text(str(resp.url))
            if lat is not None:
                return lat, lon
            # Try the redirect chain
            for r in resp.history:
                loc = r.headers.get("location", "")
                lat, lon = extract_coords_from_text(loc)
                if lat is not None:
                    return lat, lon
            # Last resort: scan the response body for a coordinate pattern
            lat, lon = extract_coords_from_text(resp.text[:5000])
            if lat is not None:
                return lat, lon
    except Exception as e:
        print(f"[MMS] short link resolve failed: {e}")

    return None, None


async def download_from_url(url: str, max_bytes: int = 200 * 1024 * 1024) -> bytes | None:
    """
    Download a file from a URL (used for S3 links sent by carriers when
    the MMS attachment exceeds the 1.5 MB inline limit).
    Returns the file bytes or None on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            if len(resp.content) > max_bytes:
                print(f"[MMS] File too large from S3 link: {len(resp.content)} bytes")
                return None
            return resp.content
    except Exception as e:
        print(f"[MMS] Failed to download from URL {url}: {e}")
        return None


def is_s3_or_media_link(text: str) -> str | None:
    """
    Detect if a plain-text MMS body contains only a media URL
    (S3 link sent by carrier for large attachments).
    Returns the URL if found, None otherwise.
    """
    text = text.strip()
    patterns = [
        r'https://[\w\-.]+\.s3\.amazonaws\.com/[^\s]+',
        r'https://mms\.twilio\.com/[^\s]+',
        r'https://[\w\-.]+\.twilio\.com/[^\s]+',
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(0)
    return None


# ---------------------------------------------------------------------------
# MMS send helper (uses regular SMS, not WhatsApp)
# ---------------------------------------------------------------------------

async def send_sms(to_phone: str, message: str) -> bool:
    """Send a plain SMS reply to an MMS farmer via Twilio REST API."""
    clean = "".join(c for c in to_phone if c.isdigit())
    if len(clean) == 10:
        clean = "1" + clean
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        print(f"[WARN] Twilio credentials not configured. Cannot send SMS to {clean}")
        return False
    try:
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Messages.json"
        )
        data = {
            "From": settings.mms_phone_number,
            "To": f"+{clean}",
            "Body": message,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                data=data,
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            )
        if resp.status_code in (200, 201):
            print(f"[SMS] Sent to {clean}: {message[:60]}")
            return True
        print(f"[SMS] Failed {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"[SMS] Error: {e}")
        return False


# ---------------------------------------------------------------------------
# MMS message handler
# ---------------------------------------------------------------------------

async def handle_sms_message(
    From: str,
    Body: str,
    NumMedia: int,
    MediaUrl0: Optional[str],
    MediaContentType0: Optional[str],
    MessageSid: str,
) -> PlainTextResponse:
    """
    Handle incoming MMS from farmers using native iPhone/Android messaging.
    Reuses all existing fdl_client post functions and session window logic.
    Key differences from WhatsApp:
      - EXIF GPS extraction from photos (carriers don't strip EXIF)
      - Apple Maps / Google Maps URL parsing for location sharing
      - S3 link auto-download for large attachments
      - Replies via plain SMS (not WhatsApp)
    """
    received_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    phone   = From.replace("+", "").replace(" ", "").strip()[:15]
    caption = Body.strip()[:2000]

    # Resolve phone to FDL contact (single source of truth)
    fdl_contact = await fdl_client.fdl_resolve_token(phone)
    if not fdl_contact:
        print(f"[MMS SILENT] Unknown phone: {phone}")
        return SILENT_RESPONSE
    fdl_token = fdl_contact["token"]

    experiment_name = fdl_contact.get("experiment_name") or "Unknown"
    ticket = generate_ticket()

    print(f"[MMS] Message from {phone} | exp={experiment_name} | ticket={ticket}")

    async def reply(msg: str) -> PlainTextResponse:
        await send_sms(phone, msg)
        return SILENT_RESPONSE  # Twilio MMS uses REST API reply, not TwiML

    # --- Check if body is a GPS location URL ---
    lat, lon = extract_coords_from_text(caption)
    # If a map link was sent but no inline coords were found, try following
    # the short-link redirect chain before giving up.
    looks_like_map_link = bool(
        re.search(r'(maps\.apple|maps\.google|google\.[^/]+/maps|goo\.gl|geo:)', caption)
    )
    if lat is None and looks_like_map_link:
        lat, lon = await resolve_short_map_link(caption)

    if lat is not None and lon is not None:
        geo_window = get_georeference_window(phone)
        if geo_window:
            # Media in this window was ALREADY saved to the database when it
            # arrived. We only need to record the location and associate it with
            # the same ticket(s), not re-post the files.
            linked_tickets = geo_window.get("tickets", []) or [ticket]
            file_count = len(geo_window.get("files", [])) or len(linked_tickets)
            primary_ticket = linked_tickets[0] if linked_tickets else ticket

            prior = pending_georeference.get(phone, {})
            pending_georeference[phone] = {
                "files": geo_window.get("files", []),
                "tickets": linked_tickets,
                "slot": "1",
                "since": prior.get("since", datetime.datetime.now()),
                "located": True, "lat": lat, "lon": lon,
            }
            pending_media_hold.pop(phone, None)

            await fdl_client.post_location(fdl_token,
                name=f"Location for {file_count} file(s)",
                track_data=fdl_client.single_point_track(lat, lon),
                start_time=received_timestamp, ticket_ref=primary_ticket)
            return await reply(
                f"📍 Location linked to your last {file_count} file(s).\n"
                f"Ticket: {primary_ticket}"
            )
        else:
            set_location_caption_window(phone, ticket)
            await fdl_client.post_location(fdl_token,
                name=f"Location pin {ticket}",
                track_data=fdl_client.single_point_track(lat, lon),
                start_time=received_timestamp, ticket_ref=ticket)
            return await reply(
                f"📍 Location recorded. Ticket: {ticket}\n"
                f"Experiment: {experiment_name}\n\n"
                f"📝 Reply with a note to add context."
            )

    # Map link detected but coordinates could not be extracted (e.g. iOS 26
    # short links that carry no coordinates). Don't silently save it as a note —
    # tell the farmer how to send a usable location. Any media they already sent
    # is ALREADY saved; this only affects whether it gets geotagged.
    if looks_like_map_link and lat is None:
        window = get_georeference_window(phone)
        pending_count = len(window.get("tickets", [])) if window else 0
        hint = (
            "📍 I couldn't read the location from that link. "
            "To geotag, open Maps, press and hold to drop a pin, tap Share, "
            "and send the pin here."
        )
        if pending_count:
            hint += (
                f"\n\nYour {pending_count} recent file(s) are already saved — "
                f"a location now would just geotag them."
            )
        return await reply(hint)

    # --- Check if body is an S3 link for a large attachment ---
    s3_url = is_s3_or_media_link(caption)
    if s3_url and NumMedia == 0:
        file_bytes = await download_from_url(s3_url)
        if not file_bytes:
            return await reply("⚠️ Could not download your file — please contact the program administrator.")
        # Guess type from URL extension
        ext = s3_url.split("?")[0].split(".")[-1].lower()
        content_type_map = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "mp4": "video/mp4", "mov": "video/quicktime", "avi": "video/avi",
            "pdf": "application/pdf", "ogg": "audio/ogg", "wav": "audio/wav",
        }
        content_type = content_type_map.get(ext, "application/octet-stream")
        fname = f"{ticket}_{received_timestamp}_{phone}.{ext}"
        _, fdl_type = EXTENSION_MAP.get(content_type, (".bin", "document"))
        if fdl_type == "photo":
            lat, lon = extract_exif_gps(file_bytes)
            await fdl_client.post_photo(fdl_token, file_bytes, fname,
                latitude=lat, longitude=lon, note=caption,
                timestamp=received_timestamp, ticket_ref=ticket)
        elif fdl_type == "video":
            await fdl_client.post_video(fdl_token, file_bytes, fname,
                note=caption, timestamp=received_timestamp, ticket_ref=ticket)
        elif fdl_type == "recording":
            await fdl_client.post_recording(fdl_token, file_bytes, fname,
                start_time=received_timestamp, ticket_ref=ticket)
        else:
            await fdl_client.post_document(fdl_token, file_bytes, fname,
                note=caption, timestamp=received_timestamp, ticket_ref=ticket)
        return await reply(
            f"✅ Your file has been saved.\n"
            f"Experiment: {experiment_name}\nTicket: {ticket}"
        )

    # --- Plain text ---
    if caption and NumMedia == 0:
        loc_window = get_location_caption_window(phone)
        if loc_window:
            await fdl_client.post_note(fdl_token,
                content=f"[Location note] {caption}",
                timestamp=received_timestamp, ticket_ref=ticket)
            clear_location_caption_window(phone)
            return await reply(f"✅ Note added.\nTicket: {loc_window['ticket']}")

        ok = await fdl_client.post_note(fdl_token,
            content=caption, timestamp=received_timestamp, ticket_ref=ticket)
        if not ok:
            return await reply("⚠️ Your message could not be recorded — please contact the program administrator.")
        return await reply(
            f"✅ Got it! Your message has been recorded.\n"
            f"Experiment: {experiment_name}\nTicket: {ticket}"
        )

    # --- Media attachment ---
    if NumMedia > 0 and MediaUrl0:
        content_type = (MediaContentType0 or "application/octet-stream").split(";")[0].strip().lower()
        if content_type not in EXTENSION_MAP:
            return await reply(
                f"⚠️ File type not supported ({content_type}). "
                f"Supported: photos, audio, video, PDF, Office documents."
            )
        ext, fdl_type = EXTENSION_MAP[content_type]
        fname = f"{ticket}_{received_timestamp}_{phone}{ext}"

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(
                    MediaUrl0,
                    auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                    follow_redirects=True,
                )
                response.raise_for_status()
                file_bytes = response.content
        except Exception as e:
            print(f"[MMS] Media download failed: {e}")
            return await reply("⚠️ Could not download your file — please contact the program administrator.")

        if fdl_type == "document":
            await fdl_client.post_document(fdl_token, file_bytes, fname,
                note=caption, timestamp=received_timestamp, ticket_ref=ticket)
            return await reply(
                f"✅ Document saved.\nExperiment: {experiment_name}\nTicket: {ticket}"
            )

        if fdl_type == "recording":
            geo_window = get_georeference_window(phone)
            gps_track = None
            if geo_window and geo_window.get("located"):
                gps_track = fdl_client.single_point_track(
                    geo_window["lat"], geo_window["lon"])
            await fdl_client.post_recording(fdl_token, file_bytes, fname,
                start_time=received_timestamp, gps_track=gps_track,
                ticket_ref=ticket)
            return await reply(
                f"✅ Voice memo saved.\nExperiment: {experiment_name}\nTicket: {ticket}"
            )

        # Photo or video: ALWAYS post to the database immediately.
        # Location is optional enrichment, never a gate. Use EXIF coords if the
        # photo carries them, otherwise any coords already captured in the
        # current window, otherwise post with no coordinates.
        lat, lon = None, None
        if fdl_type == "photo":
            lat, lon = extract_exif_gps(file_bytes)

        coord_source = None
        if lat is not None and lon is not None:
            coord_source = "photo"
        else:
            geo_window = get_georeference_window(phone)
            if geo_window and geo_window.get("located"):
                lat, lon = geo_window.get("lat"), geo_window.get("lon")
                coord_source = "window"

        if fdl_type == "photo":
            ok = await fdl_client.post_photo(fdl_token, file_bytes, fname,
                latitude=lat, longitude=lon, note=caption,
                timestamp=received_timestamp, ticket_ref=ticket)
        else:
            ok = await fdl_client.post_video(fdl_token, file_bytes, fname,
                latitude=lat, longitude=lon, note=caption,
                timestamp=received_timestamp, ticket_ref=ticket)

        if not ok:
            return await reply(
                f"⚠️ Your {fdl_type} could not be saved — please contact the program administrator.\nTicket: {ticket}"
            )

        # Already geotagged at upload time — nothing further needed.
        if coord_source == "photo":
            return await reply(
                f"✅ Your {fdl_type} has been saved with GPS coordinates from the photo.\n"
                f"Experiment: {experiment_name}\nTicket: {ticket}"
            )
        if coord_source == "window":
            return await reply(
                f"✅ Your {fdl_type} has been saved and geotagged.\n"
                f"Experiment: {experiment_name}\nTicket: {ticket}"
            )

        # Saved without coordinates. Register the ticket in the location window
        # so an OPTIONAL follow-up pin can be attached, but the file is already
        # safely in the database regardless of whether a location ever arrives.
        add_to_georeference_window(phone, fname, ticket, "1")
        return await reply(
            f"✅ Your {fdl_type} has been saved.\n"
            f"Experiment: {experiment_name}\nTicket: {ticket}\n\n"
            f"📍 Optional: to geotag it, drop a pin in Maps and Share it here "
            f"within 5 minutes. Otherwise your {fdl_type} stays saved without a location."
        )

    return await reply(
        "💬 Send a message, 📸 photo, 🎤 voice memo, 🎥 video, 📄 PDF, "
        "or 📍 location link to record an observation."
    )


def _twiml(message: str) -> PlainTextResponse:
    safe = html.escape(message, quote=True)
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message>{safe}</Message></Response>"""
    return PlainTextResponse(content=twiml, media_type="text/xml")


# ---------------------------------------------------------------------------
# Send-message endpoint (called by FDL Dashboard WhatsApp admin page)
# ---------------------------------------------------------------------------

@app.post("/send-message")
async def send_message_endpoint(request: Request):
    """
    POST /send-message  { phone: str, message: str, channel?: str }
    Called by the FDL Dashboard messaging page to send a custom or onboarding
    message to a farmer. Twilio credentials stay in OFEDashBot.

    channel selects the delivery path:
      - "sms"      -> native SMS/MMS via the toll-free number
      - "whatsapp" -> WhatsApp (default when channel is missing/unknown)
    """
    body = await request.json()
    phone   = body.get("phone", "").strip()
    message = body.get("message", "").strip()
    channel = (body.get("channel") or "whatsapp").strip().lower()
    if not phone or not message:
        return PlainTextResponse("phone and message required", status_code=400)

    if channel == "sms":
        ok = await send_sms(phone, message)
    else:
        ok = await send_whatsapp(phone, message)

    if ok:
        return {"ok": True, "channel": channel}
    return PlainTextResponse(
        f"Failed to send message via Twilio ({channel})", status_code=502
    )


# ---------------------------------------------------------------------------
# Slot endpoints
# ---------------------------------------------------------------------------

def make_webhook(slot: str):
    async def webhook(
        request: Request,
        From: str = Form(...),
        Body: str = Form(default=""),
        NumMedia: int = Form(default=0),
        MediaUrl0: Optional[str] = Form(default=None),
        MediaContentType0: Optional[str] = Form(default=None),
        MessageSid: str = Form(...),
        SentAt: Optional[str] = Form(default=None),
        Latitude: Optional[str] = Form(default=None),
        Longitude: Optional[str] = Form(default=None),
        Address: Optional[str] = Form(default=None),
        Label: Optional[str] = Form(default=None),
    ):
        if settings.validate_twilio_signature:
            signature = request.headers.get("X-Twilio-Signature", "")
            form_data = await request.form()
            if not validate_twilio_signature(str(request.url), dict(form_data), signature):
                print(f"[WARN] Invalid Twilio signature on slot {slot}")
                raise HTTPException(status_code=403, detail="Invalid signature")

        return await handle_message(
            slot=slot, From=From, Body=Body, NumMedia=NumMedia,
            MediaUrl0=MediaUrl0, MediaContentType0=MediaContentType0,
            MessageSid=MessageSid, SentAt=SentAt,
            Latitude=Latitude, Longitude=Longitude,
            Address=Address, Label=Label,
        )
    return webhook


app.add_api_route("/webhook/1", make_webhook("1"), methods=["POST"])
app.add_api_route("/webhook",   make_webhook("1"), methods=["POST"])


@app.post("/webhook/sms")
async def sms_webhook(
    request: Request,
    From: str = Form(...),
    Body: str = Form(default=""),
    NumMedia: int = Form(default=0),
    MediaUrl0: Optional[str] = Form(default=None),
    MediaContentType0: Optional[str] = Form(default=None),
    MessageSid: str = Form(...),
):
    """
    Receives incoming MMS from farmers using native iPhone/Android messaging.
    The Twilio MMS number points to this endpoint.
    """
    if settings.validate_twilio_signature:
        signature = request.headers.get("X-Twilio-Signature", "")
        form_data = await request.form()
        if not validate_twilio_signature(str(request.url), dict(form_data), signature):
            print("[WARN] Invalid Twilio signature on MMS webhook")
            raise HTTPException(status_code=403, detail="Invalid signature")

    return await handle_sms_message(
        From=From, Body=Body, NumMedia=NumMedia,
        MediaUrl0=MediaUrl0, MediaContentType0=MediaContentType0,
        MessageSid=MessageSid,
    )


@app.get("/health")
def health():
    uptime = str(datetime.datetime.now() - _start_time).split(".")[0]
    return {
        "status": "ok",
        "service": "OFEDashBot-webhook",
        "version": VERSION,
        "environment": settings.environment,
        "uptime": uptime,
        "signature_validation": settings.validate_twilio_signature,
        "fdl_backend": settings.fdl_api_url,
    }
