"""Pure builder functions that translate frontend config into SDK objects."""

import logging
import math
import os
from typing import Optional

from azure.ai.voicelive.models import (
    AvatarConfig,
    AzureCustomVoice,
    AzurePersonalVoice,
    AzureSemanticDetection,
    AzureSemanticDetectionEn,
    AzureSemanticDetectionMultilingual,
    AzureSemanticVad,
    AzureStandardVoice,
    Background,
    InterimResponseTrigger,
    OpenAIVoice,
    ServerVad,
    StaticInterimResponseConfig,
    VideoCrop,
    VideoParams,
)

from ..config import (
    MODEL_BINDING,
    REALTIME_INTERIM_TEXTS,
    REALTIME_INTERIM_THRESHOLD_MS,
)

logger = logging.getLogger(__name__)


def build_voice_config(config: dict):
    """Build voice configuration from client settings."""
    voice_type = config.get("voiceType", "standard")
    voice_name = config.get("voiceName", os.getenv("VOICELIVE_VOICE", "en-US-AvaMultilingualNeural"))
    voice_temperature = config.get("voiceTemperature", 0.9)
    voice_speed = config.get("voiceSpeed", 1.0)

    if voice_type == "custom":
        custom_voice_name = config.get("customVoiceName", "")
        deployment_id = config.get("voiceDeploymentId", "")
        return AzureCustomVoice(
            name=custom_voice_name,
            endpoint_id=deployment_id,
            rate=str(voice_speed),
        )
    elif voice_type == "personal":
        personal_voice_name = config.get("personalVoiceName", "")
        personal_model = config.get("personalVoiceModel", "DragonLatestNeural")
        return AzurePersonalVoice(
            name=personal_voice_name,
            model=personal_model,
            temperature=voice_temperature,
        )
    else:
        # Standard voice - check if Azure or OpenAI
        if "-" in voice_name:
            # Azure voice
            is_dragon = "Dragon" in voice_name
            return AzureStandardVoice(
                name=voice_name,
                temperature=voice_temperature if is_dragon else None,
                rate=str(voice_speed),
            )
        else:
            # OpenAI voice
            return OpenAIVoice(name=voice_name)

def build_avatar_config(config: dict) -> Optional[AvatarConfig]:
    """Build avatar configuration from client settings."""
    if not config.get("avatarEnabled", False):
        return None

    avatar_name = config.get("avatarName", "Lisa-casual-sitting")
    is_photo = config.get("isPhotoAvatar", False)
    is_custom = config.get("isCustomAvatar", False)
    background_url = config.get("avatarBackgroundImageUrl", "")

    # Parse character and style from avatar name
    if is_photo and is_custom:
        # Custom photo avatar trained in the customer resource: preserve case,
        # no style, customized=True is set further down.
        character = avatar_name
        style = None
    elif is_custom:
        character = avatar_name
        style = None
    elif is_photo:
        photo_name = config.get("photoAvatarName") or config.get("avatarName") or "Anika"
        parts = photo_name.split("-", 1)
        character = parts[0].lower() if parts else photo_name.lower()
        style = parts[1] if len(parts) > 1 else None
    else:
        parts = avatar_name.split("-", 1)
        character = parts[0].lower() if parts else avatar_name.lower()
        style = parts[1] if len(parts) > 1 else None

    # Build video params
    video_crop = None
    if not is_photo:
        # Centered crop matching JS sample: 800px wide centered in 1920
        video_crop = VideoCrop(top_left=[560, 0], bottom_right=[1360, 1080])

    background = None
    if background_url:
        background = Background(image_url=background_url)

    video = VideoParams(
        codec="h264",
        crop=video_crop,
        background=background,
    )

    # Build avatar config kwargs
    avatar_kwargs = {
        "character": character,
        "style": style,
        "video": video,
    }

    # Only set customized=True when actually custom (omit when False).
    # Applies to both custom video avatars and custom photo avatars.
    if is_custom:
        avatar_kwargs["customized"] = True

    avatar_cfg = AvatarConfig(**avatar_kwargs)

    # Photo avatar: add type, model, and scene via bracket notation (not in SDK model)
    if is_photo:
        avatar_cfg["type"] = "photo-avatar"
        avatar_cfg["model"] = "vasa-1"
        photo_scene = config.get("photoScene", {})
        if photo_scene:
            import math
            avatar_cfg["scene"] = {
                "zoom": photo_scene.get("zoom", 100) / 100,
                "position_x": photo_scene.get("positionX", 0) / 100,
                "position_y": photo_scene.get("positionY", 0) / 100,
                "rotation_x": photo_scene.get("rotationX", 0) * math.pi / 180,
                "rotation_y": photo_scene.get("rotationY", 0) * math.pi / 180,
                "rotation_z": photo_scene.get("rotationZ", 0) * math.pi / 180,
                "amplitude": photo_scene.get("amplitude", 100) / 100,
            }

    # Add output_protocol (not in SDK model, inject as additional property)
    avatar_output_mode = config.get("avatarOutputMode", "webrtc")
    try:
        avatar_cfg["output_protocol"] = avatar_output_mode
    except Exception:
        try:
            avatar_cfg.output_protocol = avatar_output_mode
        except Exception:
            logger.warning("Could not set output_protocol on AvatarConfig")

    return avatar_cfg

def build_turn_detection(config: dict, cascaded: bool = True):
    """Build turn detection configuration."""
    td_type = config.get("turnDetectionType", "server_vad")
    eou_type = config.get("eouDetectionType", "none")
    remove_filler = config.get("removeFillerWords", True)
    silence_duration_ms = config.get("turnDetectionSilenceMs", 500)
    # Derive the filler-word-detection language hint from the configured
    # recognition language. azure_semantic_vad's `languages` field takes
    # ISO-639-1 codes (e.g. "en"), while recognitionLanguage may be a full
    # BCP-47 tag like "en-ZA" — strip to the primary subtag. Defaults to
    # English because this deployment is locked to English output.
    recognition_lang = (config.get("recognitionLanguage") or "en").strip()
    if recognition_lang and recognition_lang.lower() != "auto":
        vad_language = recognition_lang.split("-", 1)[0].lower() or "en"
    else:
        vad_language = "en"
    vad_languages = [vad_language]
    # interrupt_response MUST mirror the client-side barge-in behaviour. If the
    # server is allowed to interrupt on speech_started while the client keeps
    # playing the avatar audio (barge-in off), the avatar's own voice echoing
    # into the always-on mic re-triggers the VAD, cancelling/reopening turns and
    # leaving an orphaned "You: ..." segment that never commits. Keeping them in
    # lock-step prevents that runaway feedback loop.
    interrupt_response = config.get("enableBargeIn", True)

    # Tuned for lower turn-taking latency. EOU timeout dropped from 500ms to
    # 300ms to shave ~200ms off every turn. Raise back to 500 if you start
    # seeing premature cutoffs from users who pause mid-sentence.
    #
    # `cascaded=False` is model mode (Voice Live bound to a realtime model).
    # Semantic end-of-utterance detection is text-based, so it needs the local
    # speech recognizer that only exists in the cascaded ASR -> model -> TTS
    # pipeline. Sending it to a realtime model is rejected outright:
    #
    #   "Text-based end-of-utterance detection requires a local speech
    #    recognizer and is only supported on cascaded pipelines."
    #
    # It fails the whole session.update, not just the field, so this cannot be
    # left to the service to ignore. Model mode therefore falls back to plain
    # silence-duration turn-taking and loses this tuning -- a real trade-off
    # against removing the ASR stage, and one the benchmark has to account for.
    if td_type == "azure_semantic_vad":
        eou_detection = None
        if not cascaded:
            if eou_type in ("semantic_detection_v1", "semantic_detection_v1_multilingual"):
                logger.info(
                    f"Dropping end-of-utterance detection {eou_type!r}: not supported "
                    "when Voice Live is bound to a realtime model (no local recognizer)."
                )
        elif eou_type == "semantic_detection_v1_multilingual":
            eou_detection = AzureSemanticDetectionMultilingual(
                threshold_level="default",
                timeout_ms=300,
            )
        elif eou_type == "semantic_detection_v1":
            eou_detection = AzureSemanticDetectionEn(
                threshold_level="default",
                timeout_ms=300,
            )
        return AzureSemanticVad(
            threshold=0.5,
            prefix_padding_ms=300,
            speech_duration_ms=80,
            silence_duration_ms=silence_duration_ms,
            remove_filler_words=remove_filler,
            languages=vad_languages,
            interrupt_response=interrupt_response,
            # When a real barge-in happens mid-reply, keep the LLM's view of
            # the conversation aligned with what the user actually heard:
            # only the spoken-so-far portion is persisted to history. Per
            # learn.microsoft.com/azure/ai-services/speech-service/how-to-voice-live-auto-truncation
            # this should always be paired with interrupt_response=true.
            auto_truncate=True,
            end_of_utterance_detection=eou_detection,
        )
    else:
        return ServerVad(
            threshold=0.5,
            prefix_padding_ms=300,
            silence_duration_ms=silence_duration_ms,
        )


def build_interim_response(
    voice_config, model_binding: bool = MODEL_BINDING
) -> Optional[StaticInterimResponseConfig]:
    """A short spoken acknowledgement while a tool call is in flight.

    Grounding is the single largest item in the answer budget — measured
    714ms for the minutes index and ~280ms warm (1.2s cold) for the web —
    and until it returns the room hears nothing at all. Silence is what
    people read as slowness, so filling it moves perceived latency more than
    shaving milliseconds off retrieval.

    Deliberately the STATIC variant, not ``LlmInterimResponseConfig``. The
    LLM one generates a context-aware line, but with a second model
    (gpt-4.1-mini by default) — another round trip on the exact path we are
    trying to shorten. A canned line costs nothing and says the same thing.

    Trigger is ``tool`` only. The ``latency`` trigger would also fire on a
    slow plain answer, where there is nothing to wait for and an
    acknowledgement just delays the real reply.

    ``latency_threshold_ms`` is lowered from the SDK default of 2000ms —
    verified applied by reading back SESSION_UPDATED. At the default it never
    fired at all, because our tools return in 714ms (minutes) and ~280ms warm
    (web), so every tool call finished well inside the threshold. The model
    filled that silence itself with an improvised preamble instead, which is
    the worst of both: it is unbounded in length, it costs a model turn, and
    it directly contradicts the prompt's instruction not to narrate. Setting
    the threshold below the tool floor hands the job to the platform, where
    it is one short canned line.

    ⚠️ Requires an Azure TTS voice. Verified against the live service:

        interim_response_requires_azure_voice — "Interim response in realtime
        pipeline requires an Azure TTS voice (azure-standard, azure-custom,
        azure-personal, or avatar-voice-sync). OpenAI native voices stream
        audio directly and cannot support interim response injection."

    That is a hard 400 which fails the WHOLE session.update, not a warning, so
    it cannot be left for the service to ignore — the same failure mode as
    semantic end-of-utterance detection in model mode.

    The error also states the architecture plainly. A realtime model emits its
    own audio, and bound directly (Azure OpenAI Realtime) that is all you can
    ever get — the built-in voices, no substitution. What Voice Live adds is a
    replaceable synthesis stage: choose an Azure voice and the model's text is
    spoken by Azure TTS instead, which is what makes azure-custom (a trained
    custom neural voice) and azure-personal reachable at all. Interim
    injection is a second thing that stage buys, because there is a synthesis
    step to inject into; with a native voice the audio is already streaming
    and there is nowhere to put it.

    So this is not "Azure voice costs a TTS stage". For a branded avatar the
    synthesis stage is the reason to be on Voice Live in the first place, and
    a native OpenAI voice is the configuration that gives up custom voice,
    personal voice and interim response together.

    Set ``REALTIME_INTERIM_TEXTS=""`` to disable.
    """
    if not model_binding or not REALTIME_INTERIM_TEXTS:
        return None
    if isinstance(voice_config, OpenAIVoice):
        logger.info(
            "Interim response disabled: an OpenAI native voice streams the "
            "model's own audio, so there is no synthesis stage to inject "
            "into. Note this configuration also forgoes custom and personal "
            "neural voices — switch to an Azure voice to get all three."
        )
        return None
    return StaticInterimResponseConfig(
        triggers=[InterimResponseTrigger.TOOL],
        texts=REALTIME_INTERIM_TEXTS,
        latency_threshold_ms=REALTIME_INTERIM_THRESHOLD_MS,
    )
