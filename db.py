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
            os.getenv("DATABASE_URL"), min_size=1, max_size=5, init=_init_conn
        )
        logger.info("DB pool created")
    return _pool


def db_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL")) and asyncpg is not None


# ── Tenant resolution ────────────────────────────────────────────────────────
async def get_tenant_by_number(phone: str) -> Optional[dict]:
    pool = await get_pool()
    if not pool:
        return None
    async with pool.acquire() as c:
        row = await c.fetchrow(
            """select t.* from tenants t
               join tenant_numbers n on n.tenant_id = t.id
               where n.phone_number = $1 and t.active""",
            phone,
        )
        return dict(row) if row else None


async def get_tenant_by_slug(slug: str) -> Optional[dict]:
    pool = await get_pool()
    if not pool:
        return None
    async with pool.acquire() as c:
        row = await c.fetchrow("select * from tenants where slug = $1 and active", slug)
        return dict(row) if row else None


async def get_tenant_number(tenant_id) -> Optional[str]:
    """The phone number to place outbound calls FROM for this tenant."""
    pool = await get_pool()
    if not pool:
        return None
    async with pool.acquire() as c:
        return await c.fetchval(
            "select phone_number from tenant_numbers where tenant_id=$1 order by created_at limit 1",
            tenant_id,
        )


# ── Leads ────────────────────────────────────────────────────────────────────
async def save_lead(
    tenant_id, name: str, email: str, phone: str, source: str = "instagram", consent: bool = False
) -> Optional[str]:
    pool = await get_pool()
    if not pool:
        return None
    async with pool.acquire() as c:
        lead_id = await c.fetchval(
            """insert into leads (tenant_id, name, email, phone, source, consent, status)
               values ($1,$2,$3,$4,$5,$6,'calling') returning id""",
            tenant_id, name, email, phone, source, consent,
        )
        return str(lead_id)


# ── Calls / conversations ────────────────────────────────────────────────────
async def create_call(tenant_id, lead_id, call_sid, direction) -> Optional[str]:
    pool = await get_pool()
    if not pool:
        return None
    async with pool.acquire() as c:
        call_id = await c.fetchval(
            """insert into calls (tenant_id, lead_id, call_sid, direction, status)
               values ($1,$2,$3,$4,'in_progress') returning id""",
            tenant_id, lead_id, call_sid, direction,
        )
        return str(call_id)


async def finish_call(call_id, transcript: list, summary: str = None, outcome: str = None):
    pool = await get_pool()
    if not pool or not call_id:
        return
    async with pool.acquire() as c:
        await c.execute(
            """update calls
                 set status='completed', ended_at=now(),
                     transcript=$2, summary=$3, outcome=$4
               where id=$1""",
            call_id, json.dumps(transcript), summary, outcome,
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
