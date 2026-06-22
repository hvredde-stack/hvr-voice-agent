"""RAG helpers: embed text and search a tenant's knowledge base.

Embeddings use OpenAI text-embedding-3-small (1536 dims, cheap). Set
OPENAI_API_KEY to enable; without it, ingest/search no-op gracefully.
"""

import os
from typing import Optional

from loguru import logger

import db

EMBED_MODEL = "text-embedding-3-small"
_client = None


def _openai():
    global _client
    if _client is None:
        if not os.getenv("OPENAI_API_KEY"):
            return None
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


async def embed(text: str) -> Optional[list[float]]:
    client = _openai()
    if not client:
        return None
    resp = await client.embeddings.create(model=EMBED_MODEL, input=text)
    return resp.data[0].embedding


async def ingest(tenant_id, chunks: list[str]):
    """Embed and store a list of text chunks for a tenant's knowledge base."""
    for chunk in chunks:
        vec = await embed(chunk)
        if vec is None:
            logger.warning("No OPENAI_API_KEY — skipping KB ingest")
            return
        await db.insert_kb_doc(tenant_id, chunk, vec)
    logger.info(f"Ingested {len(chunks)} KB chunks for tenant {tenant_id}")


async def search(tenant_id, query: str, k: int = 4) -> list[str]:
    """Return the top-k knowledge chunks relevant to the query."""
    try:
        vec = await embed(query)
    except Exception as e:  # noqa: BLE001
        logger.error(
            f"Embedding search failed; falling back to text KB search: {type(e).__name__}"
        )
        vec = None
    if vec is None:
        return await db.search_kb_text(tenant_id, query, k)
    try:
        return await db.search_kb(tenant_id, vec, k)
    except Exception as e:  # noqa: BLE001
        logger.error(
            f"Vector KB search failed; falling back to text KB search: {type(e).__name__}"
        )
        return await db.search_kb_text(tenant_id, query, k)
