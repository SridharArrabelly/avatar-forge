"""Environment loading and logging configuration."""

import logging
import os

from dotenv import load_dotenv

from .avatar_identity import (
    DEFAULT_PHOTO_AVATAR,
    DEFAULT_STANDARD_AVATAR,
    avatar_model,
    avatar_type,
    resolve_avatar_display_name,
)

load_dotenv(override=True)


class ColorFormatter(logging.Formatter):
    """Adds ANSI color codes to log output."""

    COLORS = {
        logging.DEBUG: "\033[36m",     # Cyan
        logging.INFO: "\033[32m",      # Green
        logging.WARNING: "\033[33m",   # Yellow
        logging.ERROR: "\033[31m",     # Red
        logging.CRITICAL: "\033[1;31m",  # Bold Red
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    WHITE = "\033[97m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        timestamp = self.formatTime(record, self.datefmt)
        return (
            f"{self.DIM}{timestamp}{self.RESET} "
            f"{color}{self.BOLD}{record.levelname:<8}{self.RESET} "
            f"{self.DIM}{record.name}{self.RESET} "
            f"{self.WHITE}{record.getMessage()}{self.RESET}"
        )


def configure_logging(level: int | str | None = None) -> None:
    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(ColorFormatter())
    logging.basicConfig(level=level, handlers=[handler])


# Public env-derived defaults
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3000"))
DEFAULT_VOICE = os.getenv("VOICELIVE_VOICE", "")
DEFAULT_ENDPOINT = os.getenv("AZURE_VOICELIVE_ENDPOINT", "")
DEFAULT_API_KEY = os.getenv("AZURE_VOICELIVE_API_KEY", "")
AGENT_NAME = os.getenv("AGENT_NAME", "")
AGENT_PROJECT_NAME = os.getenv("AGENT_PROJECT_NAME", "")
# Voice Live REST/WebSocket API version. The set of accepted speech-recognition
# models is gated server-side by this version. NOTE: mai-transcribe-1.5 is
# currently only available via the separate LLM Speech (batch) API, NOT the
# Voice Live realtime API used here — bumping this does not unlock it yet.
VOICELIVE_API_VERSION = os.getenv("VOICELIVE_API_VERSION", "2026-01-01-preview")
DEVELOPER_MODE = os.getenv("DEVELOPER_MODE", "false").strip().lower() == "true"

# Verbatim opening line spoken by the avatar when proactive greeting is enabled.
# Client-specific persona/wording lives in the environment, keeping this code
# generic and reusable. Falls back to a neutral greeting when unset.
PROACTIVE_GREETING = os.getenv(
    "PROACTIVE_GREETING",
    "Hello! How can I help you today?",
)

PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT", "")

# ───────── Voice Live binding: Foundry agent vs. realtime model ─────────
# Two ways to give the avatar a brain, both over the same Voice Live session:
#
#   agent  — bind to a Foundry agent. Instructions, tools and temperature live
#            in Foundry. Audio is transcribed by a separate speech-to-text model
#            (SR_MODEL) before the agent ever sees it.
#   model  — bind straight to a realtime model. It accepts audio natively, so
#            the transcription stage disappears from the answer path, and the
#            instructions and tools travel in the session instead.
#
# Defaults to `agent`, so an unconfigured deploy behaves exactly as before.
VOICE_BINDING = os.getenv("VOICE_BINDING", "agent").strip().lower()
# Realtime model used when VOICE_BINDING=model. Verified to bind (with avatar
# and tools) in swedencentral: gpt-realtime-2, gpt-realtime-1.5, gpt-realtime.
VOICELIVE_MODEL = os.getenv("VOICELIVE_MODEL", "gpt-realtime-2").strip()

# Resolved once so every module agrees on which binding is active.
MODEL_BINDING = VOICE_BINDING == "model"

# Model-mode response shaping. Both are inert in agent mode, where the Foundry
# agent owns response behaviour.
#
# A realtime model left uncapped answers at length — measured 27-30 seconds of
# speech against a prompt asking for two or three sentences. The cap is a
# backstop, not the primary control: the prompt does the shaping, this stops the
# worst case. Roughly 90 spoken seconds' worth, so it never truncates a
# reasonable answer.
REALTIME_MAX_TOKENS = int(os.getenv("REALTIME_MAX_TOKENS", "1200"))

# Spoken while a tool call is in flight, so grounding is not dead air. Voice
# Live picks one at random per turn — several short options rather than one line
# repeated, which grates within a couple of turns.
#
# DEFAULT OFF, deliberately. Live testing found the spoken filler fires on every
# tool-backed turn, which is almost every turn, and "One moment." ahead of each
# answer reads as a tic rather than as reassurance. The on-screen "thinking"
# indicator in the frontend already covers the same gap silently and is the
# mechanism both bindings share, so nothing regresses by muting the voice.
#
# Note what this costs, because it is the whole of model mode's apparent speed:
# the filler is what made model mode start speaking at ~1.0s against agent
# mode's ~2.4s. Time to the *substantive answer* was 2.42s vs 2.45s — identical.
# Muting the filler therefore removes a perceived-latency win without changing
# any real one. Channels C/D (in-call audio) have no screen to put an indicator on
# and will need a spoken cue, so this is expected to come back — as a tuned set
# of triggers and phrasings rather than a blanket preamble.
#
# Set REALTIME_INTERIM_TEXTS="Let me check.,One moment." to re-enable.
REALTIME_INTERIM_TEXTS = [
    t.strip() for t in os.getenv("REALTIME_INTERIM_TEXTS", "").split(",") if t.strip()
]

# How long a tool may run before the acknowledgement is spoken. The SDK default
# is 2000ms, which never fires here — measured tool latency is 714ms (minutes)
# and ~280ms warm (web). Set below that floor so the platform covers the gap
# instead of the model improvising a preamble to fill it.
REALTIME_INTERIM_THRESHOLD_MS = int(os.getenv("REALTIME_INTERIM_THRESHOLD_MS", "300"))

# ───────── Teams in-call media participant (channel C, issue #27) ─────────
# Channel C is the live in-call avatar. It is ADDITIVE and OPT-IN: every endpoint
# and the media bridge are gated on ACS being configured, so a deploy without ACS
# behaves exactly as the web app alone. Two legs share the same Voice Live pipeline:
#   1. the .NET Graph media bot on a Windows VM  -> wss://.../ws/acs/audio (D)
#      (hears every participant; this is the real one)
#   2. the browser joiner /acs-join.html         -> wss://.../ws/acs/browser
#      ACS Calling SDK joins as an anonymous interop guest via the meeting lobby,
#      then captures and streams the audio from the browser itself — server-side
#      connect_call() does not carry Teams *meeting* audio. It only hears the
#      operator's own mic, so it is the no-admin-rights fallback.
ACS_ENDPOINT = os.getenv("ACS_ENDPOINT", "").strip()
# Connection string for the ACS resource (preferred for Call Automation + Identity).
# When empty, the client falls back to ACS_ENDPOINT + DefaultAzureCredential.
ACS_CONNECTION_STRING = os.getenv("ACS_CONNECTION_STRING", "").strip()
# Public HTTPS base URL ACS uses for call-event callbacks and the media WebSocket.
# Empty -> derive from the inbound request's own external ingress at call time.
ACS_CALLBACK_BASE_URL = os.getenv("ACS_CALLBACK_BASE_URL", "").strip()
# PCM sample rate (Hz) for the ACS<->Voice Live audio bridge. 24000 matches Voice
# Live's PCM16 output (and accepted input), so the bridge needs no resampling.
# ACS supports 16000 or 24000; keep this aligned with the Voice Live formats.
ACS_AUDIO_SAMPLE_RATE = int(os.getenv("ACS_AUDIO_SAMPLE_RATE", "24000"))
# Wake phrases the in-call avatar listens for before answering aloud (turn-taking
# so she never talks over participants). Pipe/comma tolerated; lower-cased. The
# default derives from the resolved persona name so the wake word is whatever the
# avatar is actually called on screen — say "hey Simone", not "hey Avatar".
# Override explicitly with ACS_WAKE_PHRASES.
_avatar_name = resolve_avatar_display_name().lower()
ACS_WAKE_PHRASES = [
    p.strip().lower()
    for p in os.getenv("ACS_WAKE_PHRASES", f"hey {_avatar_name},{_avatar_name}")
    .replace("|", ",")
    .split(",")
    if p.strip()
]
# When True, the avatar only speaks if the triggering utterance contained a wake
# phrase (half-duplex turn-taking). When False, she answers every detected turn
# (useful for a 1:1 test meeting). Default True to avoid talking over a room.
ACS_REQUIRE_WAKE_PHRASE = os.getenv(
    "ACS_REQUIRE_WAKE_PHRASE", "true"
).strip().lower() in ("1", "true", "yes", "on")
# After the avatar finishes an answer, stay "armed" for this many seconds so a
# natural follow-up question does NOT need the wake phrase again (conversational
# turn-taking). Only applies when ACS_REQUIRE_WAKE_PHRASE is True. 0 disables the
# grace window (every turn then needs the wake phrase).
#
# Default 90s, raised from 30s after live testing: two questions in one session
# ("what is MTN's share price today?") landed 35s and 40s after the previous
# answer — plainly aimed at her, just outside the window — so she sat silent and
# the tester had to repeat themselves with the wake phrase. That reads as "she
# ignored me", which is worse than the risk the window guards against.
ACS_FOLLOWUP_WINDOW_S = float(os.getenv("ACS_FOLLOWUP_WINDOW_S", "90"))
# Seconds of inactivity before the participant leaves the call (0 disables).
ACS_IDLE_TIMEOUT_S = float(os.getenv("ACS_IDLE_TIMEOUT_S", "0"))
# Avatar face: when true, the browser joiner sends an outgoing
# video stream so the avatar appears as a visible participant tile (not a faceless
# audio participant). The first increment is a branded placard (logo + avatar name
# + a "listening" pulse) rendered to a canvas and sent via the ACS Calling SDK's
# raw-video LocalVideoStream — the same transport a live animated-avatar track will
# use next. Default OFF so deployments behave exactly as before until opted in.
ACS_AVATAR_VIDEO_ENABLED = os.getenv(
    "ACS_AVATAR_VIDEO_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
# In-call media bot (channel D): the .NET/Windows Graph media bot connects to the
# ``/ws/acs/audio`` bridge endpoint and speaks the AcsVoiceBridge protocol. That
# path needs Voice Live only — NOT an ACS resource — so this flag enables the
# bridge endpoint independently of ACS_ENDPOINT/ACS_CONNECTION_STRING. (The
# ACS-specific REST endpoints — /api/acs/token, /api/acs/call — still require a
# real ACS resource; the media bot does not use them.)
MEETING_BOT_ENABLED = os.getenv(
    "MEETING_BOT_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
# The avatar's FACE in the meeting: when true, the meeting
# bot's Voice Live session runs the avatar in ``websocket`` output mode and the
# bridge decodes the resulting stream into raw NV12 frames for the .NET bot's
# VideoSocket, so the avatar appears as a real lip-synced camera tile.
#
# This changes the audio path too, which is why it is a single flag rather than a
# cosmetic toggle: in avatar/websocket mode Voice Live stops emitting
# ``response.audio.delta`` and muxes the answer audio (AAC) into the same
# fragmented-MP4 stream (measured against the live service). The bridge therefore
# recovers the audio from that stream instead. Default OFF, so an audio-only
# deployment keeps the simpler, already-proven PCM path untouched.
MEETING_BOT_VIDEO_ENABLED = os.getenv(
    "MEETING_BOT_VIDEO_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
# Target NV12 size/rate for the avatar camera tile. These MUST match the format
# the .NET bot negotiates (Bot__VideoWidth/Height/Fps -> VideoFormatFor), because
# the bot drops frames whose dimensions differ and shows its placeholder instead.
MEETING_BOT_VIDEO_WIDTH = int(os.getenv("MEETING_BOT_VIDEO_WIDTH", "640"))
MEETING_BOT_VIDEO_HEIGHT = int(os.getenv("MEETING_BOT_VIDEO_HEIGHT", "360"))
MEETING_BOT_VIDEO_FPS = int(os.getenv("MEETING_BOT_VIDEO_FPS", "15"))
# The same avatar face, but for the BROWSER joiner (acs-join.html) rather than the
# Windows media bot. The browser already sends an outgoing video tile — until now
# a static branded placard — so this swaps that placard for the live lip-synced
# avatar.
#
# The split of work differs from the media-bot path: the browser plays the
# fragmented-MP4 itself in a muted MediaSource <video> and paints it onto the tile
# canvas, so the server only has to recover the AAC audio from that stream (the
# same measured behaviour applies — in avatar mode Voice Live sends no
# ``response.audio.delta``). Recovered audio goes through the existing outbound
# path, so barge-in, the wake-phrase gate and the host's "Mute" all keep working.
#
# Default OFF: with it off the joiner behaves exactly as it does today.
BROWSER_JOIN_VIDEO_ENABLED = os.getenv(
    "BROWSER_JOIN_VIDEO_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
# True when channel C/D in-call media is configured: either an ACS resource is set,
# or the Graph media bot bridge is explicitly enabled.
ACS_ENABLED = bool(ACS_ENDPOINT or ACS_CONNECTION_STRING or MEETING_BOT_ENABLED)

# The assistant's resolved persona name — what she calls herself, what the bot
# greets with, what names her in the meeting roster, and what the wake phrase
# listens for. AVATAR_DISPLAY_NAME is the explicit knob; when it is unset this
# falls back to the ACTIVE avatar model's friendly name (Simone, Lisa, ...) so the
# spoken name always matches the name on the stage. The previous behavior —
# falling back to the literal "Avatar" — is why an avatar displayed as "Simone"
# introduced itself as "Avatar". See backend/avatar_identity.py for the full rule
# and for why IS_*-gating the model lookup is what makes deriving from it safe.
# Still purely cosmetic: it does NOT select the avatar model (that is AVATAR_NAME
# / CUSTOM_AVATAR_NAME / PHOTO_AVATAR_NAME, gated by IS_*).
AVATAR_DISPLAY_NAME = resolve_avatar_display_name()


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _list(name: str, default: list[str]) -> list[str]:
    """Parse a pipe-separated env var into a list of trimmed, non-empty strings.

    Returns ``default`` when the var is unset or empty so callers always get a
    usable list (e.g. SUGGESTED_PROMPTS="Ask me X | Ask me Y").
    """
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    items = [part.strip() for part in raw.split("|")]
    items = [part for part in items if part]
    return items or default


def get_ui_defaults() -> dict:
    """Settings sent to the frontend on /api/config.

    Each value overrides the matching HTML default. Used in production
    (DEVELOPER_MODE=false) where the side panel is hidden and the session
    auto-starts with whatever is configured here.
    """
    selected_avatar_type = avatar_type()
    selected_avatar_model = avatar_model()
    is_photo_avatar = selected_avatar_type.endswith("-photo")
    is_custom_avatar = selected_avatar_type.startswith("custom-")
    legacy_avatar_name = _str("AVATAR_NAME", DEFAULT_STANDARD_AVATAR)
    legacy_custom_avatar_name = _str("CUSTOM_AVATAR_NAME", "")
    legacy_photo_avatar_name = _str("PHOTO_AVATAR_NAME", DEFAULT_PHOTO_AVATAR)
    if selected_avatar_type == "standard-video":
        legacy_avatar_name = selected_avatar_model
    elif selected_avatar_type == "standard-photo":
        legacy_photo_avatar_name = selected_avatar_model
    else:
        legacy_custom_avatar_name = selected_avatar_model

    return {
        # Conversation
        "srModel": _str("SR_MODEL", "mai-transcribe-1"),
        "recognitionLanguage": _str("RECOGNITION_LANGUAGE", "auto"),
        "useNS": _bool("USE_NOISE_SUPPRESSION", True),
        "useEC": _bool("USE_ECHO_CANCELLATION", True),
        "turnDetectionType": _str("TURN_DETECTION_TYPE", "azure_semantic_vad"),
        "turnDetectionSilenceMs": int(_str("TURN_DETECTION_SILENCE_MS", "500")),
        "enableBargeIn": _bool("ENABLE_BARGE_IN", True),
        "removeFillerWords": _bool("REMOVE_FILLER_WORDS", True),
        "eouDetectionType": _str("EOU_DETECTION_TYPE", "semantic_detection_v1"),
        "enableProactive": _bool("ENABLE_PROACTIVE", False),
        # Voice
        "voiceType": _str("VOICE_TYPE", "standard"),
        "voiceName": _str("VOICELIVE_VOICE", "en-US-AvaMultilingualNeural"),
        "voiceSpeed": int(_str("VOICE_SPEED", "100")),
        "voiceTemperature": float(_str("VOICE_TEMPERATURE", "0.9")),
        # Avatar
        "avatarEnabled": _bool("AVATAR_ENABLED", True),
        "avatarOutputMode": _str("AVATAR_OUTPUT_MODE", "webrtc"),
        "avatarType": selected_avatar_type,
        "avatarModel": selected_avatar_model,
        "isPhotoAvatar": is_photo_avatar,
        "isCustomAvatar": is_custom_avatar,
        "avatarName": legacy_avatar_name,
        "customAvatarName": legacy_custom_avatar_name,
        "photoAvatarName": legacy_photo_avatar_name,
        "avatarBackgroundImageUrl": _str("AVATAR_BACKGROUND_IMAGE_URL", ""),
        "enableAvatarSpeakingStyle": _bool("ENABLE_AVATAR_SPEAKING_STYLE", False),
        # Avatar identity shown top-left on the stage. Two related keys:
        #
        #   avatarDisplayName — the RAW knob, empty when unset. Deliberately not
        #     resolved here: app.js derives the label from the live avatar-model
        #     inputs when this is empty, which is what lets the label follow the
        #     dropdown in DEVELOPER_MODE. Resolving it server-side would pin the
        #     label to whatever was configured at page load.
        #   assistantName — the RESOLVED persona name, never empty. For the
        #     surfaces with no model inputs to derive from (brand.js token
        #     substitution, the companion panel) and for anything that just needs
        #     "what is she called". Same value the agent prompt and the bot use.
        #
        # The tagline shows under the name; empty hides that line.
        "avatarDisplayName": os.getenv("AVATAR_DISPLAY_NAME", "").strip(),
        "assistantName": AVATAR_DISPLAY_NAME,
        "avatarTagline": _str("AVATAR_TAGLINE", "Your Digital Assistant"),
        # Avatar UX (additive). The on-stage text composer shows on the
        # standalone web app (default on); the frontend always hides it inside
        # the Microsoft Teams client (the bot chat tab has Teams' native compose
        # box, and the avatar tab is voice-first — type via the chat tab or, in
        # a call, the meeting chat with an @mention). ENABLE_TEXT_INPUT is an
        # optional web-only override; it can never force the composer on in Teams.
        "enableTextInput": _bool("ENABLE_TEXT_INPUT", True),
        "enableStopButton": _bool("ENABLE_STOP_BUTTON", True),
        "enableCaptions": _bool("ENABLE_CAPTIONS", False),
        "captionsShowUser": _bool("CAPTIONS_SHOW_USER", False),
        "enableSuggestedPrompts": _bool("ENABLE_SUGGESTED_PROMPTS", True),
        # Empty by default: the frontend derives a modality-aware hint ("…or
        # type…" only when the composer is actually shown, which depends on the
        # host — see enableTextInput). An explicit ONBOARDING_HINT always wins.
        "onboardingHint": _str("ONBOARDING_HINT", ""),
        "suggestedPrompts": _list(
            "SUGGESTED_PROMPTS",
            [
                "What can you help me with?",
                "Tell me about your services",
                "How do I get started?",
            ],
        ),
    }
