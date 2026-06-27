# OFEDashBot Integration

A FastAPI webhook service that lets farmers submit field observations to the FDL Dashboard by sending WhatsApp or SMS/MMS messages. Photos, videos, voice memos, documents, text notes, and location pins are forwarded to the FDL backend, which stores each submission and attributes it to the correct farm.

## How it works

A farmer sends a message (WhatsApp or SMS) to one of the project's Twilio numbers. Twilio delivers that message to this service's webhook. The service:

1. Resolves the farmer's phone number to an FDL contact token.
2. Downloads any attached media from Twilio.
3. Forwards the submission to the FDL Dashboard's upload API over HTTP.
4. Replies to the farmer with a confirmation and a ticket number.

All persistent data lives in the FDL Dashboard's Postgres database. This service holds no farmer data of its own. The only local state is a small runtime directory (ticket counter and logs) kept outside the repository.

## Channels

The system uses two separate Twilio numbers, one per channel:

- **WhatsApp** uses a WhatsApp-enabled Twilio number. Location pins arrive as native latitude/longitude fields.
- **SMS/MMS** uses a verified toll-free number. Location is shared as a map link or pin; the service parses coordinates from the link where possible.

A single number cannot serve both channels at once, which is why two are used.

## Media handling

Media is saved to the database immediately on arrival. Location is treated as optional enrichment, never a requirement for storage. When a photo or video has no embedded GPS, the service stores it right away and offers an optional five-minute window to attach a location pin afterward. If no location is ever sent, the media stays saved without one.

Supported submission types: text notes, photos, videos, voice memos, PDFs and Office documents, location pins, and contact cards.

## Project structure

| File | Role |
|------|------|
| `whatsapphook.py` | The FastAPI app and all webhook routes. Entry point. |
| `config.py` | Central configuration loaded from `.env`. |
| `fdl_client.py` | HTTP client that forwards submissions to the FDL backend. |
| `shared.py` | Ticket generation and local logging helpers. |
| `requirements.txt` | Python dependencies. |

## Webhook routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/webhook/1` and `/webhook` | POST | Incoming WhatsApp messages |
| `/webhook/sms` | POST | Incoming SMS/MMS messages |
| `/send-message` | POST | Outbound message (called by the FDL Dashboard) |
| `/health` | GET | Health check |

## Setup

### 1. Requirements

- Python 3.11 or newer
- A running FDL Dashboard instance (provides the upload API and Postgres storage)
- A Twilio account with a WhatsApp sender and a verified toll-free SMS number

### 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

Then edit `.env`. The settings are:

| Variable | Description |
|----------|-------------|
| `TWILIO_ACCOUNT_SID` | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_WHATSAPP_NUMBER` | WhatsApp sender, e.g. `whatsapp:+1XXXXXXXXXX` |
| `MMS_PHONE_NUMBER` | Toll-free SMS number, e.g. `+18XXXXXXXXX` |
| `FDL_API_URL` | Base URL of the FDL Dashboard API (default `http://localhost:3000`) |
| `FDL_SERVICE_TOKEN` | Optional service token for reading the FDL contacts list |
| `VALIDATE_TWILIO_SIGNATURE` | `true` in production, `false` for local development |
| `MAX_UPLOAD_MB` | Maximum accepted file size in megabytes |
| `BASE_DIR` | Where local runtime files live. Set this outside the repo, e.g. `/Users/you/ofedashbot-data` |
| `ENVIRONMENT` | `development` or `production` |

`BASE_DIR` must point outside the project folder so farmer-related runtime files are never committed to git.

### 4. Run

```bash
uvicorn whatsapphook:app --reload --port 8000
```

For local testing, expose the port with a tunnel so Twilio can reach it:

```bash
ngrok http 8000
```

Then set each Twilio number's incoming-message webhook to the tunnel URL:

- Toll-free SMS number: `https://YOUR_TUNNEL_URL/webhook/sms`
- WhatsApp number: `https://YOUR_TUNNEL_URL/webhook`

The tunnel URL changes each time ngrok restarts, so update the Twilio webhook fields whenever you restart it.

## Local services for development

Running the full system locally involves three processes:

1. This webhook: `uvicorn whatsapphook:app --reload --port 8000`
2. A tunnel: `ngrok http 8000`
3. The FDL Dashboard (separate project) on port 3000

If the FDL Dashboard is not running, phone-number-to-token resolution fails and the service silently drops messages, so make sure it is up before testing.

## Security notes

- Never commit `.env`. It contains live credentials and is gitignored.
- Set `VALIDATE_TWILIO_SIGNATURE=true` before deploying so only genuine Twilio requests are accepted.
- Change `SESSION_SECRET` and `ADMIN_PASSWORD` from their defaults before running in production. The service refuses to start in production with insecure defaults.
- `BASE_DIR` keeps runtime files out of the repository; keep it that way.

## Compliance

Outbound SMS from a US toll-free number requires Toll-Free Verification through Twilio before messages will deliver reliably. WhatsApp messaging is configured separately through the WhatsApp Business setup in Twilio.

## Notes and known limitations

- Inline video preview in the FDL Dashboard depends on the browser supporting the video codec. Carrier MMS video often arrives as `.3gp`, which many desktop browsers cannot play inline; such files still store and download correctly.
- Some location-share formats (notably newer iOS short map links) do not carry coordinates and cannot be geotagged automatically. The service asks the farmer to drop and share a pin instead.
