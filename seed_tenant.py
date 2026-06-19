"""Create/seed one tenant for testing.

  1. Run the schema once:  psql "$DATABASE_URL" -f db/schema.sql
  2. Set DATABASE_URL (+ OPENAI_API_KEY for the knowledge base) in .env
  3. python seed_tenant.py

Edit the TENANT / KB below for your real client.
"""

import asyncio
import os

from dotenv import load_dotenv

load_dotenv(override=True)

import db  # noqa: E402
import rag  # noqa: E402

TENANT = {
    "slug": "reidgroup",
    "name": "The Reid Group",
    "business_name": "The Reid Group",
    "voice": "Ara",  # Ara/Rex/Sal/Eve/Leo
    "system_prompt": (
        "You are Ava, the warm, professional assistant for The Reid Group, a real "
        "estate team serving Durham Region and the east GTA. Find out if the caller is "
        "buying or selling, their area and timeline, answer questions using the "
        "search_knowledge tool, and book a free consultation. Keep replies to 1-2 short "
        "sentences and never invent specific prices or legal advice."
    ),
}
OWNER_EMAIL = "owner@thereidgroup.ca"          # login identity for the dashboard
NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")  # routes calls to this tenant

KB_DOCS = [
    "The Reid Group serves Whitby, Oshawa, Ajax, Pickering, Clarington and the east GTA.",
    "We offer free, no-obligation home valuations.",
    "Jordan Reid has 12 years of experience and has sold over 480 homes.",
    "Office hours are Monday to Saturday, 9am to 7pm.",
    "We help buyers, sellers, first-time buyers, downsizers and investors.",
    "Our listings sell in an average of 18 days at about 99% of asking price.",
]


async def main():
    if not db.db_enabled():
        print("❌ DATABASE_URL not set (or asyncpg missing). Set it in .env first.")
        return
    pool = await db.get_pool()
    async with pool.acquire() as c:
        tid = await c.fetchval(
            """insert into tenants (slug, name, business_name, system_prompt, voice)
               values ($1,$2,$3,$4,$5)
               on conflict (slug) do update
                 set name=excluded.name, business_name=excluded.business_name,
                     system_prompt=excluded.system_prompt, voice=excluded.voice
               returning id""",
            TENANT["slug"], TENANT["name"], TENANT["business_name"],
            TENANT["system_prompt"], TENANT["voice"],
        )
        await c.execute(
            "insert into tenant_users (tenant_id, email, role) values ($1,$2,'owner') "
            "on conflict (email) do nothing",
            tid, OWNER_EMAIL,
        )
        if NUMBER:
            await c.execute(
                "insert into tenant_numbers (tenant_id, phone_number) values ($1,$2) "
                "on conflict (phone_number) do nothing",
                tid, NUMBER,
            )
        await c.execute("delete from kb_docs where tenant_id=$1", tid)

    await rag.ingest(tid, KB_DOCS)  # needs OPENAI_API_KEY; skips with a warning if absent
    print(f"✅ Seeded tenant '{TENANT['slug']}' ({tid})")
    print(f"   • owner login: {OWNER_EMAIL}")
    print(f"   • routes number: {NUMBER or '(none set)'}")
    print(f"   • landing page: https://<PUBLIC_HOST>/?tenant={TENANT['slug']}")


if __name__ == "__main__":
    asyncio.run(main())
