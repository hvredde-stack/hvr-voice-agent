-- ============================================================
--  Multi-tenant voice-agent schema  (Postgres + pgvector)
--  Run once on your Neon DB:  psql "$DATABASE_URL" -f db/schema.sql
-- ============================================================

create extension if not exists vector;

-- A tenant = one client (real-estate business/agent).
create table if not exists tenants (
  id             uuid primary key default gen_random_uuid(),
  slug           text unique not null,          -- used in landing-page URL /{slug}
  name           text not null,
  business_name  text,
  system_prompt  text,                          -- the agent persona/script for this client
  greeting       text,                          -- optional custom opening line
  voice          text default 'Ara',            -- Grok voice: Ara/Rex/Sal/Eve/Leo
  transfer_phone text,                           -- escalate-to-human number
  active         boolean default true,
  created_at     timestamptz default now()
);

-- LOGIN identity: who can sign into this tenant's dashboard (by email).
create table if not exists tenant_users (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  email       text unique not null,
  role        text default 'owner',             -- owner | member
  created_at  timestamptz default now()
);

-- ROUTING identity: which phone number maps to which tenant (a tenant may have many).
create table if not exists tenant_numbers (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  phone_number  text unique not null,            -- E.164, e.g. +16475849438
  created_at    timestamptz default now()
);

-- Leads captured from Instagram/ads (the form submission).
create table if not exists leads (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  name        text,
  email       text,
  phone       text not null,
  source      text default 'instagram',
  consent     boolean default false,
  status      text default 'new',                -- new | calling | called | booked | lost
  created_at  timestamptz default now()
);
create index if not exists leads_tenant_idx on leads (tenant_id, created_at desc);

-- One row per AI call, with the conversation transcript + outcome.
create table if not exists calls (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  lead_id        uuid references leads(id) on delete set null,
  call_sid       text,
  direction      text,                            -- inbound | outbound
  status         text default 'in_progress',      -- in_progress | completed | failed
  outcome        text,                             -- booked | qualified | not_interested | voicemail
  transcript     jsonb,                            -- [{ "role": "...", "content": "..." }, ...]
  summary        text,
  recording_url  text,
  started_at     timestamptz default now(),
  ended_at       timestamptz
);
create index if not exists calls_tenant_idx on calls (tenant_id, started_at desc);

-- Per-tenant knowledge base (RAG). Isolated by tenant_id.
-- NOTE: when integrated with the HVR Next.js site, this `kb_docs` table is
-- created/owned by that app's Prisma schema (model KnowledgeDoc, @@map),
-- with tenant_id = the HVR Client.id (cuid text). The client manages entries
-- from their portal; the agent only READS this table. In that mode, don't run
-- this CREATE against the HVR DB — Prisma's `db push` owns it.
create table if not exists kb_docs (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  content     text not null,
  embedding   vector(1536),                        -- OpenAI text-embedding-3-small
  created_at  timestamptz default now()
);
create index if not exists kb_docs_tenant_idx on kb_docs (tenant_id);
-- Cosine similarity index for fast retrieval:
create index if not exists kb_docs_embedding_idx
  on kb_docs using ivfflat (embedding vector_cosine_ops) with (lists = 100);
