"""Environment loading and logging configuration."""

import logging
import os

from dotenv import load_dotenv

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

# ───────── Teams bot (issue #53, Phase 2a) ─────────
# The Foundry agent is resolved by ID when available (durable identifier), with
# AGENT_NAME as a dev-only fallback (fails fast on zero/multiple matches).
AGENT_ID = os.getenv("AGENT_ID", "")
# Teams app id used to build deep links back to the Phase 1 static tab (#28).
# Defaults to TEAMS_APP_ID if that is what the package was built with.
TEAMS_APP_ID = os.getenv("TEAMS_APP_ID", "")
# entityId of the personal static tab in teams/manifest.template.json.
TEAMS_TAB_ENTITY_ID = os.getenv("TEAMS_TAB_ENTITY_ID", "avatarForgeHome")
# The bot's Entra app (client) id, used to send proactive messages back to a
# conversation. Read from the same env var the Agents SDK uses for the service
# connection so there is a single source of truth (set by infra/containerApp).
BOT_APP_ID = os.getenv(
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID",
    os.getenv("BOT_APP_ID", ""),
)
# Max seconds to let a Foundry run execute in the background before giving up and
# posting a "took too long" reply. Because answers are delivered proactively
# (ack-then-background-run), this is NOT bound by the Teams ~15s turn window.
BOT_RUN_TIMEOUT_S = float(os.getenv("BOT_RUN_TIMEOUT_S", "60"))

# ───────── Teams in-call media participant (issue #27, Phase 2b) ─────────
# Phase 2b is the live in-call audio avatar (ACS Call Automation Participant
# mode, D5=A1 browser joiner). It is ADDITIVE and OPT-IN: every endpoint and the
# media bridge are gated on ACS being configured, so a deploy without ACS behaves
# exactly as Phase 2a. The path is:
#   browser ACS Calling Web SDK joins the Teams meeting (anonymous, lobby-governed)
#   -> emits a ServerCallId -> POST /api/acs/call -> server connect_call() attaches
#   bidirectional audio over wss://.../ws/acs/audio -> AcsVoiceBridge <-> Voice Live.
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
# default derives from the avatar's brand name (AVATAR_DISPLAY_NAME) so the wake
# word tracks the configured name instead of hardcoding it; override explicitly
# with ACS_WAKE_PHRASES.
_avatar_name = (os.getenv("AVATAR_DISPLAY_NAME", "").strip() or "Avatar").lower()
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
# grace window (every turn then needs the wake phrase). Default 30s.
ACS_FOLLOWUP_WINDOW_S = float(os.getenv("ACS_FOLLOWUP_WINDOW_S", "30"))
# Seconds of inactivity before the participant leaves the call (0 disables).
ACS_IDLE_TIMEOUT_S = float(os.getenv("ACS_IDLE_TIMEOUT_S", "0"))
# Phase 2b Slice 2 (avatar face): when true, the browser joiner sends an outgoing
# video stream so the avatar appears as a visible participant tile (not a faceless
# audio participant). The first increment is a branded placard (logo + avatar name
# + a "listening" pulse) rendered to a canvas and sent via the ACS Calling SDK's
# raw-video LocalVideoStream — the same transport a live animated-avatar track will
# use next. Default OFF so deployments behave exactly as before until opted in.
ACS_AVATAR_VIDEO_ENABLED = os.getenv(
    "ACS_AVATAR_VIDEO_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
# Phase 2b Slice 1: the .NET/Windows Graph media bot connects to the
# ``/ws/acs/audio`` bridge endpoint and speaks the AcsVoiceBridge protocol. That
# path needs Voice Live only — NOT an ACS resource — so this flag enables the
# bridge endpoint independently of ACS_ENDPOINT/ACS_CONNECTION_STRING. (The
# ACS-specific REST endpoints — /api/acs/token, /api/acs/call — still require a
# real ACS resource; the media bot does not use them.)
MEETING_BOT_ENABLED = os.getenv(
    "MEETING_BOT_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
# Phase 2b Slice 2A (the avatar's FACE in the meeting): when true, the meeting
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
# True when Phase 2b in-call media is configured: either an ACS resource is set,
# or the Graph media bot bridge is explicitly enabled.
ACS_ENABLED = bool(ACS_ENDPOINT or ACS_CONNECTION_STRING or MEETING_BOT_ENABLED)

# The assistant's persona / brand name. This is the SINGLE branding knob: it
# names the Teams bot (welcome message + manifest) AND, when set, the bold name
# shown top-left on the avatar stage in the web app / Tab (see get_ui_defaults'
# "avatarDisplayName"). It is a purely cosmetic label — it does NOT select the
# avatar model (that is AVATAR_NAME / CUSTOM_AVATAR_NAME / PHOTO_AVATAR_NAME,
# gated by IS_*). It is intentionally NOT derived from CUSTOM_AVATAR_NAME: that
# is a Speech custom-avatar *model* id, valid only when IS_CUSTOM_AVATAR=true and
# empty/stale otherwise, so coupling them would let an avatar-model change
# silently rename the assistant. For the bot, unset falls back to "Avatar"; for
# the stage label, unset means "derive from the selected avatar model" (so the
# web app keeps its existing behavior when the knob is not set).
AVATAR_DISPLAY_NAME = os.getenv("AVATAR_DISPLAY_NAME", "").strip() or "Avatar"


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
        "isPhotoAvatar": _bool("IS_PHOTO_AVATAR", False),
        "isCustomAvatar": _bool("IS_CUSTOM_AVATAR", False),
        "avatarName": _str("AVATAR_NAME", "Lisa-casual-sitting"),
        "customAvatarName": _str("CUSTOM_AVATAR_NAME", ""),
        "photoAvatarName": _str("PHOTO_AVATAR_NAME", "Anika"),
        "avatarBackgroundImageUrl": _str("AVATAR_BACKGROUND_IMAGE_URL", ""),
        # Avatar identity shown top-left on the stage. The bold name line prefers
        # AVATAR_DISPLAY_NAME (the single branding knob, also used for the Teams
        # bot); empty here means "derive from the selected avatar model" (the
        # default behavior). The tagline shows under it; empty hides that line.
        "avatarDisplayName": os.getenv("AVATAR_DISPLAY_NAME", "").strip(),
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
