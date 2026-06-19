"""Speech-to-speech (realtime) agent — one model hears+thinks+speaks.

Used for BOTH inbound and outbound calls. Tenant-aware: when a `tenant` dict is
passed (loaded from the DB), the prompt/voice/tools/knowledge come from that
tenant and the call+transcript are persisted. Without a tenant it runs in
single-tenant mode from .env (the working demo).

Provider switch (REALTIME_PROVIDER): grok (default) | openai.
Imports follow the current Pipecat 1.x API.
"""

import os

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

import crypto
import db
import summarize
from prompts import first_turn_for, system_prompt_for
from tools import build_tools, register_all

load_dotenv(override=True)

PROVIDER = os.getenv("REALTIME_PROVIDER", "grok").lower()


def _build_realtime_llm(instructions: str, voice: str):
    if PROVIDER == "openai":
        from pipecat.services.openai.realtime import OpenAIRealtimeLLMService
        from pipecat.services.openai.realtime.events import SessionProperties, TurnDetection

        return OpenAIRealtimeLLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model="gpt-realtime-2",
            settings=OpenAIRealtimeLLMService.Settings(
                session_properties=SessionProperties(
                    instructions=instructions,
                    turn_detection=TurnDetection(type="server_vad"),
                ),
            ),
        )

    from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService
    from pipecat.services.xai.realtime.events import SessionProperties, TurnDetection

    return GrokRealtimeLLMService(
        api_key=os.getenv("XAI_API_KEY"),
        settings=GrokRealtimeLLMService.Settings(
            session_properties=SessionProperties(
                instructions=instructions,
                voice=voice,
                turn_detection=TurnDetection(type="server_vad"),
            ),
        ),
    )


def _extract_transcript(context) -> list:
    """Best-effort read of the full conversation from the context after a call."""
    fn = getattr(context, "get_messages", None)
    if callable(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            pass
    return list(getattr(context, "_messages", []))


async def run_bot_realtime(
    websocket, direction: str = "inbound", lead_name: str | None = None,
    tenant: dict | None = None, lead_id: str | None = None,
):
    tenant_id = (tenant or {}).get("id")
    logger.info(f"REALTIME bot — provider={PROVIDER} dir={direction} tenant={tenant_id}")

    _transport_type, call_data = await parse_telephony_websocket(websocket)

    # BYO Twilio (Model B): use the tenant's own creds if set, else the shared account.
    tw_sid = (tenant or {}).get("twilio_account_sid") or os.getenv("TWILIO_ACCOUNT_SID", "")
    tw_token = crypto.decrypt((tenant or {}).get("twilio_auth_token_enc")) or os.getenv("TWILIO_AUTH_TOKEN", "")
    serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data["call_id"],
        account_sid=tw_sid,
        auth_token=tw_token,
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(),
            serializer=serializer,
        ),
    )

    instructions = system_prompt_for(tenant, direction, lead_name)
    voice = (tenant or {}).get("voice") or os.getenv("GROK_VOICE", "Ara")
    llm = _build_realtime_llm(instructions, voice)
    call_state: dict = {}
    register_all(llm, tenant, call_state)

    messages = [{"role": "system", "content": instructions}]
    context = LLMContext(messages, build_tools(tenant))
    context_aggregator = LLMContextAggregatorPair(context, realtime_service_mode=True)

    pipeline = Pipeline(
        [
            transport.input(),
            context_aggregator.user(),
            llm,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    call_id = None

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport, _client):
        nonlocal call_id
        logger.info("Caller connected — agent speaking first")
        if tenant_id:
            try:
                call_id = await db.create_call(
                    tenant_id, lead_id, call_data.get("call_id"), direction
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"create_call failed: {e}")
        messages.append({"role": "system", "content": first_turn_for(tenant, direction, lead_name)})
        await task.queue_frame(LLMRunFrame())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        logger.info("Caller disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)

    # Persist the call once it ends: summary + outcome always; transcript only
    # if this tenant opted in (full transcription is the only piece that costs extra).
    if tenant_id and call_id:
        summary, outcome = summarize.build(call_state)
        new_lead_id = None
        if not lead_id and call_state.get("lead"):
            l = call_state["lead"]
            try:
                new_lead_id = await db.save_lead(
                    tenant_id, l.get("name"), l.get("email"), l.get("phone") or "", "inbound", True
                )
            except Exception as e:  # noqa: BLE001
                logger.error(f"save_lead failed: {e}")
        transcript = _extract_transcript(context) if (tenant or {}).get("voice_transcribe") else None
        try:
            await db.finish_call(
                call_id, transcript=transcript, summary=summary, outcome=outcome, lead_id=new_lead_id
            )
            logger.info(f"Call {call_id} saved — {outcome}: {summary}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"finish_call failed: {e}")
