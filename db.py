"""Async Postgres data layer for the multi-tenant voice agent.

GUARDED: if DATABASE_URL is unset (or asyncpg isn't installed), `get_pool()`
returns None and every helper no-ops / returns None. That keeps the
single-tenant .env demo working without any database.

Run the schema first:  psql "$DATABASE_URL" -f db/schema.sql
"""

import json
import os
from typing import Any, Optional

from loguru import logger

try:
    import asyncpg
except ImportError:  # demo runs fine without it
    asyncpg = None

_pool: Optional["asyncpg.Pool"] = None


async def _init_conn(conn):
    # Register pgvector so we can pass/receive Python lists as vectors.
    try:
        from pgvector.asyncpg import register_vector

        await register_vector(conn)
    except Exception:  # noqa: BLE001 — vector ops still work via text fallback
        pass


async def get_pool():
    """Return a connection pool, or None when no DB is configured."""
    global _pool
    if not os.getenv("DATABASE_URL") or asyncpg is None:
        return None
    if _pool is None:
        _pool = await asyncpg.create_pool(
            os.getenv("DATABASE_URL"),
            min_size=1,
            max_size=5,
            init=_init_conn,
            # Neon's pooled endpoint runs pgbouncer (transaction mode), which
            # doesn't support prepared statements — disable asyncpg's cache.
            statement_cache_size=0,
        )
        logger.info("DB pool created")
    return _pool


def db_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL")) and asyncpg is not None


# ── Tenant resolution (tenant = the HVR `Client` row) ─────────────────────────
# We read the HVR site's Prisma tables. Aliases map Client columns to the dict
# keys the bots/tools expect (id, slug, system_prompt, voice, business_name, …).
_TENANT_SELECT = """
    c.id,
    c.voice_slug         as slug,
    c.voice_prompt       as system_prompt,
    c.voice_name         as voice,
    c.name               as business_name,
    c.twilio_account_sid as twilio_account_sid,
    c.twilio_auth_token  as twilio_auth_token_enc,
    c.voice_transcribe   as voice_transcribe
"""


async def get_tenant_by_number(phone: str) -> Optional[dict]:
    pool = await get_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f'''select {_TENANT_SELECT} from "Client" c
                join agent_numbers n on n.client_id = c.id
                where n.phone_number = $1 and c.voice_active''',
            phone,
        )
        return dict(row) if row else None


async def get_tenant_by_slug(slug: str) -> Optional[dict]:
    pool = await get_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f'''select {_TENANT_SELECT} from "Client" c
                where c.voice_slug = $1 and c.voice_active''',
            slug,
        )
        return dict(row) if row else None


async def get_tenant_number(client_id) -> Optional[str]:
    """The phone number to place outbound calls FROM for this tenant."""
    pool = await get_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "select phone_number from agent_numbers where client_id=$1 order by created_at limit 1",
            client_id,
        )


# ── Leads ────────────────────────────────────────────────────────────────────
async def save_lead(
    tenant_id, name: str, email: str, phone: str, source: str = "instagram", consent: bool = False
) -> Optional[str]:
    pool = await get_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """insert into voice_leads (id, client_id, name, email, phone, source, consent, status)
               values (gen_random_uuid()::text,$1,$2,$3,$4,$5,$6,'calling') returning id""",
            tenant_id, name, email, phone, source, consent,
        )


# ── Calls / conversations ────────────────────────────────────────────────────
async def create_call(tenant_id, lead_id, call_sid, direction) -> Optional[str]:
    pool = await get_pool()
    if not pool:
        return None
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """insert into voice_calls (id, client_id, lead_id, call_sid, direction, status)
               values (gen_random_uuid()::text,$1,$2,$3,$4,'in_progress') returning id""",
            tenant_id, lead_id, call_sid, direction,
        )


async def finish_call(call_id, transcript=None, summary=None, outcome=None, lead_id=None):
    pool = await get_pool()
    if not pool or not call_id:
        return
    tjson = json.dumps(transcript) if transcript else None
    async with pool.acquire() as conn:
        await conn.execute(
            """update voice_calls
                 set status='completed', ended_at=now(),
                     transcript=$2::jsonb, summary=$3, outcome=$4,
                     lead_id=coalesce(lead_id,$5)
               where id=$1""",
            call_id, tjson, summary, outcome, lead_id,
        )


# ── Knowledge base (vector search) ───────────────────────────────────────────
async def insert_kb_doc(tenant_id, content: str, embedding: list[float]):
    pool = await get_pool()
    if not pool:
        return
    async with pool.acquire() as c:
        try:
            await c.execute(
                "insert into kb_docs (tenant_id, content, embedding) values ($1,$2,$3)",
                tenant_id, content, embedding,
            )
        except Exception:  # noqa: BLE001 — fallback if vector type isn't registered
            await c.execute(
                "insert into kb_docs (tenant_id, content, embedding) values ($1,$2,$3::vector)",
                tenant_id, content, str(embedding),
            )


async def search_kb(tenant_id, embedding: list[float], k: int = 4) -> list[str]:
    pool = await get_pool()
    if not pool:
        return []
    async with pool.acquire() as c:
        try:
            rows = await c.fetch(
                """select content from kb_docs where tenant_id=$1
                   order by embedding <=> $2 limit $3""",
                tenant_id, embedding, k,
            )
        except Exception:  # noqa: BLE001
            rows = await c.fetch(
                """select content from kb_docs where tenant_id=$1
                   order by embedding <=> $2::vector limit $3""",
                tenant_id, str(embedding), k,
            )
        return [r["content"] for r in rows]
