# HVR Voice Agent

An English AI phone agent for real-estate businesses. It does two things:

1. **24/7 receptionist** — answers inbound calls, qualifies, books, transfers.
2. **Instant lead caller** — when someone submits the landing form (from an
   Instagram reel/ad link), it calls them back in **seconds**.

**Stack (Route 2 — self-hosted):** Twilio (phone) → Pipecat (orchestration) →
Deepgram (STT) → OpenAI GPT-4o + tools (brain) → Cartesia (TTS).

```
Reel/ad → link → landing.html → POST /lead → Twilio dials lead → AI talks
Inbound call → Twilio → POST /twilio/inbound → /ws media stream → AI talks
```

---

## 1. Prerequisites (accounts — all have free trials)

| Service | What for | Get a key |
|---|---|---|
| **Twilio** | Phone number + calls | console.twilio.com → buy a number |
| **OpenAI** | The LLM brain | platform.openai.com |
| **Deepgram** | Speech-to-text | console.deepgram.com |
| **Cartesia** | Text-to-speech | play.cartesia.ai |
| **ngrok** | Public URL in dev | ngrok.com |

## 2. Setup

```bash
cd hvr-voice-agent
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your keys
```

## 3. Run it (local)

```bash
# terminal 1
uvicorn server:app --host 0.0.0.0 --port 8000

# terminal 2 — expose it publicly so Twilio can reach it
ngrok http 8000
```

Copy the ngrok domain (e.g. `abcd-1234.ngrok-free.app`) into `.env` as
`PUBLIC_HOST` (no `https://`), then **restart uvicorn**.

## 4. Point your Twilio number at the agent (for inbound)

Twilio Console → your number → **Voice → A call comes in** →
**Webhook**, `POST`, URL: `https://<PUBLIC_HOST>/twilio/inbound`

Now **call your Twilio number** → the AI receptionist answers.

## 5. Test the instant-callback (the Instagram loop)

Open `https://<PUBLIC_HOST>/` → fill the form with **your own** mobile →
submit → your phone rings within seconds and the AI talks to you. 🎉

Or hit the webhook directly:
```bash
curl -X POST https://<PUBLIC_HOST>/lead \
  -H "Content-Type: application/json" \
  -d '{"name":"Jane","phone":"+14165550199","consent":true}'
```

---

## 6. Wire it to Instagram

**Organic reel/post:** put `https://<PUBLIC_HOST>/` (or your real domain) as the
**link in bio** / Story link sticker. Reel CTA: "Link in bio — get a call back in
seconds."

**Paid ad (Meta Ads Manager):** either
- set the ad's link to the landing page (simplest, truly instant), **or**
- use a **Meta Instant Form (Lead Ad)** and forward leads to `POST /lead` in
  real time via the Meta Leadgen webhook or a Zapier/LeadsBridge "instant" Facebook
  Lead Ads trigger (JSON body with `name`, `phone`, `consent`).

---

## 7. Customise

| Want to change… | Edit |
|---|---|
| What the agent says / its goal | `prompts.py` |
| Booking / CRM / transfer logic | `tools.py` |
| Voice, LLM model, STT | `bot.py` (services section) |
| The landing page | `landing.html` |
| Business name / transfer number | `.env` |
| **Cascaded ↔ speech-to-speech** | `.env` → `VOICE_MODE` |

### Speech-to-speech (realtime) mode
Both inbound and outbound work in either engine. Flip in `.env`:
```
VOICE_MODE=realtime        # one model hears+thinks+speaks (lower latency)
REALTIME_PROVIDER=grok     # grok (~$0.05/min, cheapest S2S) or openai (gpt-realtime-2)
XAI_API_KEY=...            # for grok    (GROK_VOICE: Ara/Rex/Sal/Eve/Leo)
# or OPENAI_API_KEY=...    # for openai
```
`VOICE_MODE=cascaded` (default) uses Deepgram+GPT-4o+Cartesia and is the cheapest.
The realtime engine lives in `bot_realtime.py`; the cascaded one in `bot.py`.
Tools, telephony, the `/lead` webhook and landing page are shared by both.

## 7b. Multi-tenant mode (many clients, one system)
Single-tenant (from `.env`) is the default. To serve many clients with isolated
data + per-tenant knowledge bases, set `DATABASE_URL` (your Neon Postgres):

```bash
# 1. create the tables
psql "$DATABASE_URL" -f db/schema.sql
# 2. set DATABASE_URL (+ OPENAI_API_KEY for the knowledge base) in .env
# 3. seed a tenant (edit seed_tenant.py for your client)
python seed_tenant.py
```

How it works:
- **Account/login = email** (`tenant_users`); **call routing = phone number** (`tenant_numbers`).
- **Inbound:** the dialed number → tenant. **Outbound:** the landing page `/?tenant=<slug>` tags the lead → tenant.
- **Leads** → `leads` table; **conversations** (transcript + outcome) → `calls` table — both tagged `tenant_id`.
- **Knowledge base (RAG):** per-tenant docs in `kb_docs` (pgvector); the agent calls the `search_knowledge` tool mid-call. Embeddings via OpenAI.
- Each tenant's landing page is `https://<PUBLIC_HOST>/?tenant=<slug>` (or a Meta Lead Form posting `{tenant, name, phone, consent}` to `/lead`).

Tables: `tenants · tenant_users · tenant_numbers · leads · calls · kb_docs` (see `db/schema.sql`).
Reuse your HVR Neon DB + portal so clients view their own leads/calls.

## 8. Deploy to Render (always-on, replaces ngrok)

A `render.yaml` Blueprint is included. The server auto-detects its public URL on
Render (via `RENDER_EXTERNAL_HOSTNAME`), so you don't set `PUBLIC_HOST` there.

1. Push this folder to a GitHub repo.
2. Render → **New + → Blueprint** → connect the repo → it reads `render.yaml`.
3. When prompted, paste the secret env vars: `TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `XAI_API_KEY`, and (optional)
   `OPENAI_API_KEY`, `DATABASE_URL`.
4. Deploy → you get `https://hvr-voice-agent.onrender.com`.
5. Point your Twilio number's Voice webhook at
   `https://hvr-voice-agent.onrender.com/twilio/inbound` (for inbound).
6. Test outbound: open `https://hvr-voice-agent.onrender.com/` → submit the form.

Use the **Starter plan** (always-on). The free plan sleeps on idle and would miss
the first call. Leads append to `leads.jsonl` in single-tenant mode — set
`DATABASE_URL` for multi-tenant (leads/calls/KB in Postgres) instead.

## Notes & guardrails
- **Consent:** the form's checkbox is the caller's consent to be called — keep it
  (Canada CASL/CRTC, US TCPA). Don't auto-call numbers that didn't opt in.
- **Costs:** ~$0.07–0.15 per talk-minute across Twilio + the AI services.
- **Latency:** ~700ms–1s per turn. To push to ~500ms, swap the cascaded
  STT→LLM→TTS for the OpenAI Realtime service in `bot.py`.
- **Version:** imports target the current Pipecat runner API. If `pip` resolves a
  newer Pipecat and an import moves, check the
  [quickstart phone bot](https://github.com/pipecat-ai/pipecat-quickstart-phone-bot).
