"""Function-calling tools the agent can invoke mid-call.

Tenant-aware: when a tenant (+DB) is present, capture_lead writes to Postgres
and a `search_knowledge` RAG tool is added. Without a DB it falls back to the
single-tenant file behaviour, so the demo still works.
"""

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

import db
import rag

# ── Schemas ──────────────────────────────────────────────────────────────────
capture_lead_schema = FunctionSchema(
    name="capture_lead",
    description="Save the caller's contact details and intent. Call this as soon "
    "as you have at least their name and what they want.",
    properties={
        "name": {"type": "string", "description": "Caller's full name"},
        "phone": {"type": "string", "description": "Best callback number"},
        "email": {"type": "string", "description": "Email, if given"},
        "intent": {
            "type": "string",
            "enum": ["buying", "selling", "renting", "question", "other"],
            "description": "What the caller wants",
        },
        "notes": {"type": "string", "description": "Anything else useful"},
    },
    required=["name", "intent"],
)

book_appointment_schema = FunctionSchema(
    name="book_appointment",
    description="Book a viewing or consultation once the caller agrees to a time.",
    properties={
        "name": {"type": "string", "description": "Caller's full name"},
        "datetime": {"type": "string", "description": "Requested time, e.g. 'Saturday 2pm'"},
        "purpose": {"type": "string", "description": "e.g. home valuation, viewing"},
    },
    required=["name", "datetime"],
)

transfer_schema = FunctionSchema(
    name="transfer_to_human",
    description="Transfer to a human when the caller asks for a person, is upset, or it's urgent.",
    properties={"reason": {"type": "string", "description": "Why a human is needed"}},
    required=["reason"],
)

search_knowledge_schema = FunctionSchema(
    name="search_knowledge",
    description="Look up facts about THIS business — listings, prices, areas served, "
    "hours, financing, policies — so you can answer the caller accurately. Use it "
    "whenever you're unsure of a specific detail.",
    properties={"query": {"type": "string", "description": "What to look up"}},
    required=["query"],
)


def build_tools(tenant: dict | None = None) -> ToolsSchema:
    """The tool schemas the LLM sees — adds search_knowledge in tenant/DB mode."""
    tools = [capture_lead_schema, book_appointment_schema, transfer_schema]
    if tenant and db.db_enabled():
        tools.append(search_knowledge_schema)
    return ToolsSchema(standard_tools=tools)


def register_all(llm, tenant: dict | None = None, state: dict | None = None):
    """Register handlers on the LLM service.

    Handlers record what they captured into `state` (a per-call dict). The bot
    turns that into the persisted lead + the call's summary/outcome at hang-up —
    no extra speech-to-text needed.
    """
    tenant_id = tenant.get("id") if tenant else None
    db_on = bool(tenant_id) and db.db_enabled()
    if state is None:
        state = {}

    async def capture_lead(params: FunctionCallParams):
        state["lead"] = dict(params.arguments)
        logger.info(f"📥 Lead captured: {state['lead']}")
        await params.result_callback({"saved": True})

    async def book_appointment(params: FunctionCallParams):
        state["appointment"] = dict(params.arguments)
        logger.info(f"📅 Appointment: {state['appointment']}")
        await params.result_callback(
            {"confirmed": True, "when": params.arguments.get("datetime"),
             "note": "Agent will confirm by text."}
        )

    async def transfer_to_human(params: FunctionCallParams):
        state["transferred"] = params.arguments.get("reason", True)
        logger.info(f"📞 Transfer requested: {state['transferred']}")
        await params.result_callback(
            {"transferring": True, "say": "Connecting you to an agent now, one moment."}
        )

    llm.register_function("capture_lead", capture_lead)
    llm.register_function("book_appointment", book_appointment)
    llm.register_function("transfer_to_human", transfer_to_human)

    if db_on:
        async def search_knowledge(params: FunctionCallParams):
            query = params.arguments.get("query", "")
            chunks = await rag.search(tenant_id, query, k=4)
            logger.info(f"🔎 KB search '{query}' → {len(chunks)} hits")
            await params.result_callback(
                {"results": chunks or ["No specific info on file — offer to have an agent follow up."]}
            )

        llm.register_function("search_knowledge", search_knowledge)
