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
    OpenAIVoice,
    ServerVad,
    VideoCrop,
    VideoParams,
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

def build_turn_detection(config: dict):
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
    if td_type == "azure_semantic_vad":
        eou_detection = None
        if eou_type == "semantic_detection_v1_multilingual":
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

