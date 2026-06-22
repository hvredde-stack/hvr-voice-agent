-- HVR Voice Agent database helper
--
-- The HVR Media House Next.js app owns the production schema through Prisma:
--   Client
--   agent_numbers
--   voice_leads
--   voice_calls
--   kb_docs
--
-- The voice agent reads/writes those tables directly. Do not create a separate
-- tenant schema for production; that would bypass the customer portal.

create extension if not exists vector;

-- Optional index for tenant-scoped KB vector search. Run after the portal has
-- created kb_docs with tenant_id, content, embedding vector(1536), and created_at.
create index if not exists kb_docs_tenant_idx on kb_docs (tenant_id);
create index if not exists kb_docs_embedding_idx
  on kb_docs using ivfflat (embedding vector_cosine_ops) with (lists = 100);
