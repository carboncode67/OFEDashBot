"""
OFEDashBot — FDL Backend Client
================================
Thin HTTP client that forwards WhatsApp submissions to the FDL Dashboard's
upload API. This replaces the old flat-file storage layer: instead of writing
to received/, each submission is POSTed to the FDL backend, which stores the
file and creates the database row, attributing it to the right farm.

Authentication
--------------
Every upload carries the farmer's token in an Authorization: Bearer <token>
header. The FDL backend looks the token up in its Contacts table and attributes
the submission to that contact (and their farm). OFEDashBot resolves a WhatsApp
phone number to a token via fdl_resolve_token() before calling these functions.

Endpoint coverage
-----------------
Live in the FDL backend today:
    post_note, post_location, post_photo, post_recording
Pending backend work (Part A1 of the integration plan) — the client functions
are written to the agreed contract so they work the moment the endpoints exist:
    post_video, post_document, post_contact_card

All functions are async and return True on success, False on failure. They never
raise on a normal HTTP error; failures are logged and returned as False so the
webhook can decide how to reply to the farmer.
"""

from __future__ import annotations

from typing import Optional
import json
import httpx

from config import settings


# Timeout for FDL calls. Media uploads can be large, so allow generous headroom.
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)


def _api_url(path: str) -> str:
    """Join the configured FDL base URL with an endpoint path."""
    base = settings.fdl_api_url.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _iso_ts(timestamp: str) -> str:
    """
    Convert OFEDashBot timestamp format (20260603_142030) to ISO 8601
    (2026-06-03T14:20:30) which the FDL backend expects for Date fields.
    Passes through strings that are already ISO format.
    """
    if not timestamp:
        return ""
    # Already ISO format
    if "T" in timestamp or "-" in timestamp:
        return timestamp
    # OFEDashBot format: YYYYMMDD_HHMMSS
    try:
        d = timestamp[:8]
        t = timestamp[9:15] if len(timestamp) > 9 else "000000"
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}"
    except Exception:
        return timestamp


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Token resolution: phone number -> FDL contact token
# ---------------------------------------------------------------------------

async def fdl_resolve_token(phone: str) -> Optional[dict]:
    """
    Resolve a WhatsApp phone number to the farmer's FDL contact.

    Returns a dict with at least {"token": str, "name": str, "farm_id": int|None}
    on success, or None if the phone is not registered as a contact.

    Uses GET /api/contacts/by-phone/{phone} if available (Part A4). Falls back to
    scanning GET /api/contacts (which already exists) so this works before the
    by-phone endpoint is added.
    """
    clean = phone.replace("whatsapp:", "").replace("+", "").replace(" ", "").strip()

    # Preferred: dedicated by-phone endpoint (added in Part A4).
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                _api_url(f"/api/contacts/by-phone/{clean}"),
                headers=_service_headers(),
            )
            if resp.status_code == 200:
                data = resp.json()
                if data and data.get("token"):
                    return {
                        "token": data["token"],
                        "name": data.get("name", ""),
                        "farm_id": data.get("farms_id"),
                        "is_lab_member": data.get("is_lab_member", False),
                        "experiment_name": data.get("experiment_name") or "",
                    }
            # Any non-200 (including 404 when the endpoint does not exist yet, or
            # when no contact matches) falls through to the full-list scan below,
            # which is the reliable source of truth.
    except (httpx.HTTPError, json.JSONDecodeError):
        # Endpoint not present yet or transient error — fall back to full list.
        pass

    # Fallback: scan the full contacts list (endpoint exists today).
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_api_url("/api/contacts"), headers=_service_headers())
            if resp.status_code != 200:
                print(f"[FDL] contacts list returned {resp.status_code}")
                return None
            for c in resp.json():
                stored = (c.get("phone") or "").replace("+", "").replace(" ", "").strip()
                # Match on the last 10 digits to be resilient to leading-1 differences.
                if stored and stored[-10:] == clean[-10:] and c.get("token"):
                    return {
                        "token": c["token"],
                        "name": c.get("name", ""),
                        "farm_id": c.get("farms_id"),
                        "is_lab_member": c.get("is_lab_member", False),
                        "experiment_name": c.get("experiment_name") or "",
                    }
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        print(f"[FDL] token resolution failed: {e}")
    return None


def _service_headers() -> dict:
    """
    Headers for OFEDashBot's own reads of the FDL API (e.g. the contacts list).
    Uses a service token if one is configured; otherwise sends no auth, which is
    fine if the contacts list endpoint is open on the internal network.
    """
    token = getattr(settings, "fdl_service_token", "") or ""
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---------------------------------------------------------------------------
# LIVE endpoints (exist in the FDL backend today)
# ---------------------------------------------------------------------------

async def post_note(
    token: str,
    content: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    timestamp: str = "",
    ticket_ref: str = "",
) -> bool:
    """POST a text observation to /api/upload/note (JSON body)."""
    payload = {
        "content": content,
        "latitude": latitude,
        "longitude": longitude,
        "timestamp": _iso_ts(timestamp),
        "ticket_ref": ticket_ref,
    }
    return await _post_json("/api/upload/note", token, payload, label="note")


async def post_location(
    token: str,
    name: Optional[str],
    track_data: dict,
    start_time: str = "",
    end_time: str = "",
    ticket_ref: str = "",
) -> bool:
    """
    POST a location/track to /api/upload/location (JSON body).

    For a single WhatsApp location pin, pass a GeoJSON-style track_data with one
    coordinate, e.g.:
        {"type": "Feature",
         "geometry": {"type": "LineString", "coordinates": [[lon, lat]]}}
    The backend extracts the first point for farm attribution.
    """
    payload = {
        "name": name,
        "track_data": track_data,
        "start_time": _iso_ts(start_time),
        "end_time": _iso_ts(end_time),
        "ticket_ref": ticket_ref,
    }
    return await _post_json("/api/upload/location", token, payload, label="location")


async def post_photo(
    token: str,
    file_bytes: bytes,
    filename: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    note: str = "",
    timestamp: str = "",
    ticket_ref: str = "",
) -> bool:
    """POST a photo to /api/upload/photo (multipart). geoJSON carries the coords."""
    geo = {}
    if latitude is not None and longitude is not None:
        geo = {"latitude": latitude, "longitude": longitude}
    data = {
        "geoJSON": json.dumps(geo),
        "note": note,
        "timestamp": _iso_ts(timestamp),
    }
    return await _post_multipart("/api/upload/photo", token, file_bytes, filename, data, label="photo")


async def post_recording(
    token: str,
    file_bytes: bytes,
    filename: str,
    start_time: str = "",
    end_time: str = "",
    gps_track: Optional[dict] = None,
    ticket_ref: str = "",
) -> bool:
    """POST an audio recording to /api/upload/recording (multipart)."""
    data = {
        "startTime": _iso_ts(start_time),
        "endTime": _iso_ts(end_time),
        "gpsTrack": json.dumps(gps_track) if gps_track else "",
        "ticket_ref": ticket_ref,
    }
    return await _post_multipart("/api/upload/recording", token, file_bytes, filename, data, label="recording")


# ---------------------------------------------------------------------------
# PENDING endpoints (Part A1 — written to the agreed contract, ready to go live)
# ---------------------------------------------------------------------------

async def post_video(
    token: str,
    file_bytes: bytes,
    filename: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    note: str = "",
    timestamp: str = "",
    ticket_ref: str = "",
) -> bool:
    """
    POST a video to /api/upload/video (multipart). Mirrors the photo contract.
    Requires the /api/upload/video endpoint from Part A1.
    """
    geo = {}
    if latitude is not None and longitude is not None:
        geo = {"latitude": latitude, "longitude": longitude}
    data = {
        "geoJSON": json.dumps(geo),
        "note": note,
        "timestamp": _iso_ts(timestamp),
    }
    return await _post_multipart("/api/upload/video", token, file_bytes, filename, data, label="video")


async def post_document(
    token: str,
    file_bytes: bytes,
    filename: str,
    note: str = "",
    timestamp: str = "",
    ticket_ref: str = "",
) -> bool:
    """
    POST a document (PDF, Office) to /api/upload/document (multipart).
    Requires the /api/upload/document endpoint from Part A1.
    """
    data = {
        "note": note,
        "timestamp": _iso_ts(timestamp),
    }
    return await _post_multipart("/api/upload/document", token, file_bytes, filename, data, label="document")


async def post_contact_card(
    token: str,
    name: str,
    phone: str = "",
    email: str = "",
    org: str = "",
    note: str = "",
    ticket_ref: str = "",
) -> bool:
    """
    POST a shared contact card (vCard) to /api/upload/contact-card (JSON body).
    The note field carries the farmer's answer to "who is this / should they
    have data access". Requires the dedicated contact-card model and endpoint
    from Part A1 (Q4: dedicated model).
    """
    payload = {
        "name": name,
        "phone": phone,
        "email": email,
        "org": org,
        "note": note,
        "ticket_ref": ticket_ref,
    }
    return await _post_json("/api/upload/contact-card", token, payload, label="contact-card")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _post_json(path: str, token: str, payload: dict, label: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _api_url(path),
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                json=payload,
            )
        if resp.status_code in (200, 201):
            return True
        print(f"[FDL] {label} upload returned {resp.status_code}: {resp.text[:200]}")
        return False
    except httpx.HTTPError as e:
        print(f"[FDL] {label} upload failed: {e}")
        return False


async def _post_multipart(
    path: str, token: str, file_bytes: bytes, filename: str, data: dict, label: str
) -> bool:
    try:
        files = {"file": (filename, file_bytes)}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _api_url(path),
                headers=_auth_headers(token),
                data=data,
                files=files,
            )
        if resp.status_code in (200, 201):
            return True
        print(f"[FDL] {label} upload returned {resp.status_code}: {resp.text[:200]}")
        return False
    except httpx.HTTPError as e:
        print(f"[FDL] {label} upload failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Convenience: build a single-point GeoJSON track for a WhatsApp location pin
# ---------------------------------------------------------------------------

def single_point_track(lat: float, lon: float) -> dict:
    """Wrap one coordinate as a GeoJSON Feature the FDL backend can parse."""
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[lon, lat]]},
    }
