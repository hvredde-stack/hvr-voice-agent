# HVR Voice Agent

Multi-tenant AI phone agent for HVR Media House clients.

The agent does not run with a generic business profile. Every call must resolve
to an active client tenant before the bot starts:

- Outbound callback: `/lead` requires a `tenant` slug from `/?tenant=<voiceSlug>`.
- Inbound call: Twilio's dialed `To` number must match `agent_numbers.phone_number`.
- Bot session: `/ws` requires an active tenant and closes if the tenant is missing.
- Knowledge base: `kb_docs` is always queried by tenant/client id.

## Runtime Flow

```text
Client capture link -> /?tenant=<voiceSlug> -> POST /lead
  -> Client row by voice_slug
  -> voice_leads row
  -> tenant phone number from agent_numbers
  -> Twilio call
  -> /ws?tenant=<voiceSlug>
  -> tenant prompt + tenant tools + tenant KB

Inbound caller -> Twilio number -> POST /twilio/inbound
  -> Client row by agent_numbers.phone_number
  -> /ws?tenant=<voiceSlug>
  -> tenant prompt + tenant tools + tenant KB
```

## Required Client Configuration

Each active voice client needs:

- `Client.voice_slug`
- `Client.voice_active = true`
- `Client.voice_prompt`
- At least one `agent_numbers` row
- Relevant rows in `kb_docs` for that client

The HVR portal owns the client records and knowledge entries. The voice agent
reads those rows and writes `voice_leads` / `voice_calls`.

## Local Setup

```bash
cd hvr-voice-agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

Set these environment variables:

- `DATABASE_URL`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `PUBLIC_HOST` in local/ngrok development
- `XAI_API_KEY` for realtime Grok mode, or the required keys for cascaded mode
- `OPENAI_API_KEY` for embeddings; if unavailable, tenant-scoped text KB search is used

## Test Routes

Open the capture form with a tenant:

```text
https://<PUBLIC_HOST>/?tenant=<voiceSlug>
```

Post a callback lead:

```bash
curl -X POST https://<PUBLIC_HOST>/lead \
  -H "Content-Type: application/json" \
  -d '{"tenant":"<voiceSlug>","name":"Jane","phone":"+14165550199","consent":true}'
```

Missing or inactive tenants are rejected before any call is placed.

## Deploy

This repo deploys as an always-on Render web service via `render.yaml`.

Use the Starter plan or better so the service does not sleep between calls.
Render provides `RENDER_EXTERNAL_HOSTNAME`; the server uses it as `PUBLIC_HOST`
when `PUBLIC_HOST` is not set.

Point each Twilio number's Voice webhook to:

```text
https://<render-host>/twilio/inbound
```

## Knowledge Base

The portal writes `kb_docs` with `tenant_id = Client.id`. Search is isolated by
that id, so one client's KB cannot answer another client's call.

Embeddings use `text-embedding-3-small` when `OPENAI_API_KEY` works. If
embeddings are unavailable, the agent falls back to tenant-scoped text search.
