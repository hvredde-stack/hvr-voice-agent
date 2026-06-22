"""Cascaded voice agent: Twilio -> Deepgram (STT) -> GPT-4o (+tools) -> Cartesia (TTS).

A resolved tenant is mandatory. The tenant dict drives prompt, tools, knowledge
base lookup, and persistence for the call.
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
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
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


def _extract_transcript(context) -> list:
    fn = getattr(context, "get_messages", None)
    if callable(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            pass
    return list(getattr(context, "_messages", []))


async def run_bot(
    websocket, direction: str = "inbound", lead_name: str | None = None,
    tenant: dict | None = None, lead_id: str | None = None,
):
    if not tenant or not tenant.get("id"):
        raise RuntimeError("Tenant is required to run the voice bot.")
    tenant_id = tenant["id"]
    logger.info(f"Cascaded bot - dir={direction} tenant={tenant_id}")

    _transport_type, call_data = await parse_telephony_websocket(websocket)

    tw_sid = tenant.get("twilio_account_sid") or os.getenv("TWILIO_ACCOUNT_SID", "")
    tw_token = crypto.decrypt(tenant.get("twilio_auth_token_enc")) or os.getenv("TWILIO_AUTH_TOKEN", "")
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

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"), model="gpt-4o")
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id="71a7ad14-091c-4e8e-a314-022ece01c121",
    )

    instructions = system_prompt_for(tenant, direction, lead_name)
    call_state: dict = {}
    register_all(llm, tenant, call_state)
    messages = [{"role": "system", "content": instructions}]
    context = LLMContext(messages, build_tools(tenant))
    context_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            context_aggregator.user(),
            llm,
            tts,
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
        logger.info("Caller connected - agent speaking first")
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

    if call_id:
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
        transcript = _extract_transcript(context) if tenant.get("voice_transcribe") else None
        try:
            await db.finish_call(
                call_id, transcript=transcript, summary=summary, outcome=outcome, lead_id=new_lead_id
            )
            logger.info(f"Call {call_id} saved - {outcome}: {summary}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"finish_call failed: {e}")
