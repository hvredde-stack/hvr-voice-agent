"""Seed voice-agent data for an existing HVR client.

This script intentionally has no sample client, industry, or generic prompt.
Set all client-specific values explicitly before running it.

Required:
  VOICE_SEED_SLUG       Existing Client.voice_slug
  VOICE_SEED_PROMPT     Client-specific voice instructions
  VOICE_SEED_KB         One or more KB facts separated by ||

Optional:
  VOICE_SEED_NUMBER     E.164 phone number to route to this client

Example:
  VOICE_SEED_SLUG=hvr-photography
  VOICE_SEED_PROMPT="You help callers with HVR Photography services..."
  VOICE_SEED_KB="Fact one.||Fact two."
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv(override=True)

import db  # noqa: E402
import rag  # noqa: E402

SLUG = os.getenv("VOICE_SEED_SLUG", "").strip()
PROMPT = os.getenv("VOICE_SEED_PROMPT", "").strip()
NUMBER = os.getenv("VOICE_SEED_NUMBER", "").strip()
KB_DOCS = [
    item.strip()
    for item in os.getenv("VOICE_SEED_KB", "").split("||")
    if item.strip()
]


async def main():
    if not db.db_enabled():
        print("DATABASE_URL is not set or asyncpg is missing.")
        return
    if not SLUG or not PROMPT or not KB_DOCS:
        print("Set VOICE_SEED_SLUG, VOICE_SEED_PROMPT, and VOICE_SEED_KB first.")
        return

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        client_id = await conn.fetchval(
            '''update "Client"
                  set voice_prompt=$2, voice_active=true
                where voice_slug=$1
                returning id''',
            SLUG,
            PROMPT,
        )
        if not client_id:
            print(f"No active Client row found with voice_slug={SLUG!r}.")
            return

        if NUMBER:
            await conn.execute(
                """insert into agent_numbers (id, client_id, phone_number)
                   values ($1,$2,$3)
                   on conflict (phone_number) do update
                     set client_id=excluded.client_id""",
                db._new_id(),  # noqa: SLF001 - local maintenance script
                client_id,
                NUMBER,
            )

        await conn.execute("delete from kb_docs where tenant_id=$1", client_id)

    await rag.ingest(client_id, KB_DOCS)
    print(f"Seeded voice data for client slug {SLUG!r}.")
    print(f"Landing page: https://<PUBLIC_HOST>/?tenant={SLUG}")


if __name__ == "__main__":
    asyncio.run(main())
