"""FastAPI server — landing page, instant-callback webhook, inbound TwiML, media stream.

Multi-tenant + guarded:
  - With a DB: resolve tenant (by dialed number inbound / by slug outbound),
    save leads to Postgres, drive the call with that tenant's config.
  - Without a DB: single-tenant from .env (the working demo) — unchanged.

Run:  uvicorn server:app --host 0.0.0.0 --port 8000
"""

import os
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

import crypto
import db

load_dotenv(override=True)

# Pick the voice engine: cascaded (cheap, default) or realtime (speech-to-speech).
VOICE_MODE = os.getenv("VOICE_MODE", "cascaded").lower()
if VOICE_MODE == "realtime":
    from bot_realtime import run_bot_realtime as run_bot
else:
    from bot import run_bot

# On Render, RENDER_EXTERNAL_HOSTNAME is injected automatically — use it when
# PUBLIC_HOST isn't set (so you don't hand-wire the URL in prod).
PUBLIC_HOST = (
    os.getenv("PUBLIC_HOST") or os.getenv("RENDER_EXTERNAL_HOSTNAME") or ""
).replace("https://", "").replace("http://", "").strip("/")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

app = FastAPI(title="HVR Voice Agent")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def _stream_twiml(direction: str, *, tenant_slug="", lead_name="", lead_id="") -> str:
    if not PUBLIC_HOST:
        raise RuntimeError("PUBLIC_HOST is not set — Twilio can't reach the stream.")
    qs = f"direction={direction}"
    if tenant_slug:
        qs += f"&tenant={quote(tenant_slug)}"
    if lead_name:
        qs += f"&lead_name={quote(lead_name)}"
    if lead_id:
        qs += f"&lead_id={quote(lead_id)}"
    resp = VoiceResponse()
    resp.connect().stream(url=f"wss://{PUBLIC_HOST}/ws?{qs}")
    return str(resp)


async def _twilio_creds(tenant):
    """Resolve which Twilio account + from-number to use for a call.

    - Full BYO creds on the tenant (Model B) → use them.
    - Tenant has only a number (Model A: number under our shared account) → env creds + that number.
    - No tenant → the shared account from env.
    """
    if tenant:
        sid = tenant.get("twilio_account_sid")
        tok = crypto.decrypt(tenant.get("twilio_auth_token_enc"))
        num = await db.get_tenant_number(tenant["id"])
        if sid and tok and num:
            return sid, tok, num
        if num:
            return ACCOUNT_SID, AUTH_TOKEN, num
    return ACCOUNT_SID, AUTH_TOKEN, TWILIO_NUMBER


@app.get("/")
async def landing():
    return FileResponse(os.path.join(os.path.dirname(__file__), "landing.html"))


@app.get("/health")
async def health():
    return {
        "ok": all([PUBLIC_HOST, TWILIO_NUMBER, ACCOUNT_SID, AUTH_TOKEN]),
        "public_host": PUBLIC_HOST or None,
        "multi_tenant": db.db_enabled(),
        "voice_mode": VOICE_MODE,
    }


@app.post("/lead")
async def lead(request: Request):
    """A lead submitted (IG form / Meta webhook) → call them NOW."""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        data = dict(await request.form())

    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip()
    consent = data.get("consent") in (True, "true", "on", "1", 1)
    tenant_slug = (data.get("tenant") or "").strip()

    if not phone:
        return JSONResponse({"error": "phone is required"}, status_code=400)
    if not consent:
        return JSONResponse({"error": "consent is required to place a call"}, status_code=400)

    # Resolve tenant + persist lead (multi-tenant), else single-tenant from env.
    tenant = await db.get_tenant_by_slug(tenant_slug) if tenant_slug else None
    if tenant_slug and not tenant:
        return JSONResponse(
            {"error": "This lead-capture link is not active. Please contact HVR."},
            status_code=404,
        )

    lead_id = ""
    if tenant:
        try:
            lead_id = await db.save_lead(tenant["id"], name, email, phone, "instagram", consent) or ""
        except Exception as e:  # noqa: BLE001
            logger.error(f"save_lead failed: {e}")
            return JSONResponse(
                {"error": "Lead was not saved. Please try again."},
                status_code=500,
            )

    sid, token, from_number = await _twilio_creds(tenant)
    try:
        client = Client(sid, token)
        call = client.calls.create(
            to=phone,
            from_=from_number,
            twiml=_stream_twiml(
                "outbound", tenant_slug=tenant_slug, lead_name=name or "", lead_id=lead_id
            ),
        )
        logger.info(f"📞 Outbound call to {phone} (tenant={tenant_slug or 'single'}) → {call.sid}")
        return {"status": "calling", "call_sid": call.sid}
    except Exception as e:  # noqa: BLE001
        logger.error(f"Outbound call failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/twilio/inbound")
async def twilio_inbound(request: Request):
    """Twilio hits this when someone calls a number → route to its tenant."""
    form = dict(await request.form())
    dialed = (form.get("To") or "").strip()
    tenant = await db.get_tenant_by_number(dialed) if dialed else None
    slug = tenant["slug"] if tenant else ""
    return Response(
        content=_stream_twiml("inbound", tenant_slug=slug), media_type="application/xml"
    )


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    q = websocket.query_params
    direction = q.get("direction", "inbound")
    lead_name = q.get("lead_name")
    lead_id = q.get("lead_id") or None
    tenant_slug = q.get("tenant")
    tenant = await db.get_tenant_by_slug(tenant_slug) if tenant_slug else None
    logger.info(f"WS connected — dir={direction} tenant={tenant_slug or 'single'}")
    try:
        await run_bot(websocket, direction, lead_name, tenant=tenant, lead_id=lead_id)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Bot run error: {e}")
