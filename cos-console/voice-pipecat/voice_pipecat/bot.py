"""The live voice loop, assembled with Pipecat.

    mic → VAD → STT → Claude (tool-use) → TTS → speaker,  barge-in on by default

This module imports Pipecat at module load; it is only imported by the `run`
(live) path, never by `--dry-run`, so the keyless dry-run works before
`pip install`.

Targets **Pipecat 1.5.x** (the current line — verified against 1.5.0). Pipecat's
API moved substantially at 1.0: the universal `LLMContext` /
`LLMContextAggregatorPair` replaced the per-provider `OpenAILLMContext` +
`create_context_aggregator`, and VAD is now a pipeline `VADProcessor` rather than
a transport param. If imports break after an upgrade, that's the first place to
look.
"""

from __future__ import annotations

from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.transports.local.audio import (
    LocalAudioTransport,
    LocalAudioTransportParams,
)

from .brain import SYSTEM_PROMPT
from .config import Providers
from .status_tool import (
    TOOL_DESCRIPTION,
    TOOL_INPUT_SCHEMA,
    TOOL_NAME,
    get_status_report,
)

SAMPLE_RATE = 24000


def _build_stt(p: Providers):
    if p.stt_choice == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        logger.info("STT: Deepgram streaming")
        return DeepgramSTTService(api_key=p.deepgram_key)

    from pipecat.services.whisper.stt import WhisperSTTService

    logger.info("STT: local Whisper (faster-whisper) — no key")
    return WhisperSTTService()


def _build_tts(p: Providers):
    import os

    if p.tts_choice == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService

        logger.info("TTS: Cartesia")
        return CartesiaTTSService(
            api_key=p.cartesia_key,
            voice_id=os.environ.get(
                "CARTESIA_VOICE_ID", "71a7ad14-091c-4e8e-a314-022ece01c121"
            ),
            sample_rate=SAMPLE_RATE,
        )
    if p.tts_choice == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        logger.info("TTS: ElevenLabs")
        return ElevenLabsTTSService(
            api_key=p.elevenlabs_key,
            voice_id=os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL"),
            sample_rate=SAMPLE_RATE,
        )

    from .tts_say import SayTTSService

    logger.info("TTS: macOS `say` — no key")
    return SayTTSService(
        sample_rate=SAMPLE_RATE, voice=os.environ.get("SAY_VOICE", "Samantha")
    )


def _status_tools_schema() -> ToolsSchema:
    fn = FunctionSchema(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        properties=TOOL_INPUT_SCHEMA["properties"],
        required=TOOL_INPUT_SCHEMA["required"],
    )
    return ToolsSchema(standard_tools=[fn])


async def run_live(p: Providers) -> None:
    """Run the live mic→speaker loop until Ctrl-C."""
    if not p.has_brain:
        raise RuntimeError(
            "Live mode needs ANTHROPIC_API_KEY (Claude is the brain). "
            "Use `--dry-run` for the keyless canned demo."
        )

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_out_sample_rate=SAMPLE_RATE,
        )
    )

    stt = _build_stt(p)
    tts = _build_tts(p)
    llm = AnthropicLLMService(api_key=p.anthropic_key, model=p.claude_model)

    # Register the single tool. Pipecat invokes this, we call the stub, and the
    # result goes back to Claude to summarize aloud.
    async def handle_get_project_status(params):
        project = (params.arguments or {}).get("project", "")
        logger.info(f"tool get_project_status(project={project!r})")
        await params.result_callback(get_status_report(project))

    llm.register_function(TOOL_NAME, handle_get_project_status)

    context = LLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=_status_tools_schema(),
    )
    aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            VADProcessor(vad_analyzer=SileroVADAnalyzer()),  # barge-in signal
            stt,
            aggregator.user(),
            llm,
            tts,
            transport.output(),
            aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_out_sample_rate=SAMPLE_RATE,
        ),
    )

    logger.info(f"Providers: {p.summary()}")
    logger.info(
        'Speak into your mic, e.g. "give me a status on dstrader". '
        "Interrupt any time (barge-in). Ctrl-C to quit."
    )

    runner = PipelineRunner(handle_sigint=True)
    await runner.run(task)
