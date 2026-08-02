"""Pin the agent/model binding switch.

The switch decides which brain answers, so the property that matters is not
"model mode works" but "agent mode is unchanged". Every environment running
today is on the agent binding, and a regression there breaks the shipped
product silently — the session would still connect, just with different
instructions, different tools, or a different end-of-utterance detector.

So the load-bearing assertions here are the negative ones: with
``VOICE_BINDING`` unset or ``agent``, no model-mode key reaches the session and
no model-mode builder returns anything.

Needs no Azure and no credentials — it builds config objects and inspects them.
Run: uv run python scripts/test_voice_binding.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, actual, expected) -> None:
    global CHECKS
    CHECKS += 1
    if actual != expected:
        FAILURES.append(f"{label}\n     expected: {expected!r}\n     actual:   {actual!r}")


def check_true(label: str, actual) -> None:
    check(label, bool(actual), True)


def _fresh_config(**env):
    """Reimport backend.config with a specific environment.

    config.py reads os.environ at import time, so a plain import would pin
    whatever the first test set. Modules that captured the old values are
    dropped too, otherwise handler/builders keep stale constants.
    """
    for key in ("VOICE_BINDING", "VOICELIVE_MODEL", "DEVELOPER_MODE",
                "REALTIME_INTERIM_TEXTS", "REALTIME_MAX_TOKENS"):
        os.environ.pop(key, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    for mod in ("backend.config", "backend.voice.builders",
                "backend.voice.handler", "backend.voice.tools"):
        sys.modules.pop(mod, None)
    import backend.config as cfg
    return cfg


# ---------------------------------------------------------------- binding
def test_binding_resolution():
    cfg = _fresh_config()
    check("unset VOICE_BINDING defaults to agent", cfg.VOICE_BINDING, "agent")
    check("unset VOICE_BINDING -> MODEL_BINDING False", cfg.MODEL_BINDING, False)

    cfg = _fresh_config(VOICE_BINDING="agent")
    check("explicit agent -> MODEL_BINDING False", cfg.MODEL_BINDING, False)

    cfg = _fresh_config(VOICE_BINDING="model")
    check("model -> MODEL_BINDING True", cfg.MODEL_BINDING, True)

    # Casing and stray whitespace come from azd env files and shell exports.
    cfg = _fresh_config(VOICE_BINDING=" MODEL ")
    check("' MODEL ' normalises to model", cfg.MODEL_BINDING, True)

    # An unrecognised value must fail SAFE — towards the shipped behaviour.
    cfg = _fresh_config(VOICE_BINDING="banana")
    check("unknown binding falls back to agent", cfg.MODEL_BINDING, False)


# ------------------------------------------------------- interim response
def test_interim_response_gating():
    # Every check in here sets REALTIME_INTERIM_TEXTS explicitly. The shipped
    # default is now empty (see backend/config.py), so any check written against
    # the default would pass for the wrong reason: it would prove the text list
    # is empty rather than that the gate under test actually works.
    ON = "One moment."

    _fresh_config(VOICE_BINDING="agent", REALTIME_INTERIM_TEXTS=ON)
    from backend.voice.builders import build_interim_response
    from azure.ai.voicelive.models import AzureStandardVoice

    azure_voice = AzureStandardVoice(name="en-US-AvaMultilingualNeural")
    check("agent mode never gets interim_response",
          build_interim_response(azure_voice), None)

    # Off by default. Live testing found the spoken filler fires on nearly every
    # tool-backed turn — which is nearly every turn — and a canned preamble ahead
    # of each answer reads as a tic. The frontend's on-screen thinking indicator
    # covers the same gap silently, in both bindings. The mechanism below is
    # untouched; only the default changed.
    _fresh_config(VOICE_BINDING="model")
    from backend.voice.builders import build_interim_response as bir_default
    check("model mode has no interim_response by default",
          bir_default(azure_voice), None)

    _fresh_config(VOICE_BINDING="model", REALTIME_INTERIM_TEXTS=ON)
    from backend.voice.builders import build_interim_response as bir
    check_true("model mode + Azure voice gets interim_response when texts are set",
               bir(azure_voice) is not None)

    # The service returns a hard 400 for an OpenAI voice and fails the WHOLE
    # session.update, so this guard is not cosmetic — getting it wrong takes
    # the entire session down, not just the filler.
    from azure.ai.voicelive.models import OpenAIVoice
    check("OpenAI native voice suppresses interim_response",
          bir(OpenAIVoice(name="alloy")), None)

    # Explicit opt-out.
    _fresh_config(VOICE_BINDING="model", REALTIME_INTERIM_TEXTS="")
    from backend.voice.builders import build_interim_response as bir2
    check("empty REALTIME_INTERIM_TEXTS disables interim", bir2(azure_voice), None)

    # Per-session override must reach the builder, not just the connect call.
    _fresh_config(VOICE_BINDING="agent", REALTIME_INTERIM_TEXTS=ON)
    from backend.voice.builders import build_interim_response as bir3
    check_true("model_binding=True override enables interim in an agent deploy",
               bir3(azure_voice, model_binding=True) is not None)
    _fresh_config(VOICE_BINDING="model", REALTIME_INTERIM_TEXTS=ON)
    from backend.voice.builders import build_interim_response as bir4
    check("model_binding=False override disables interim in a model deploy",
          bir4(azure_voice, model_binding=False), None)


# --------------------------------------------------- end-of-utterance mode
def test_turn_detection_eou():
    """Semantic EOU is rejected in model mode and must not be sent there.

    Verified against the live service: 'semantic_detection_v1 requires a local
    speech recognizer ... cascaded pipelines only'. Like interim_response this
    fails the whole session.update, so it cannot be left for the service to
    ignore.
    """
    _fresh_config(VOICE_BINDING="agent")
    from backend.voice.builders import build_turn_detection

    # Semantic EOU only exists under azure_semantic_vad — plain server_vad has
    # no EOU field at all, so the config has to select it explicitly or this
    # test would pass vacuously against two identical server_vad objects.
    cfg = {
        "turnDetectionType": "azure_semantic_vad",
        "eouDetectionType": "semantic_detection_v1",
    }
    agent_td = build_turn_detection(cfg, cascaded=True)
    model_td = build_turn_detection(cfg, cascaded=False)

    def eou_of(td):
        raw = td.as_dict() if hasattr(td, "as_dict") else getattr(td, "__dict__", {})
        return (raw or {}).get("end_of_utterance_detection")

    check_true("cascaded (agent) keeps semantic EOU", eou_of(agent_td) is not None)
    check("model mode drops semantic EOU", eou_of(model_td), None)

    # The rest of the VAD config must survive the drop — only EOU goes.
    def type_of(td):
        raw = td.as_dict() if hasattr(td, "as_dict") else getattr(td, "__dict__", {})
        return (raw or {}).get("type")

    check("model mode keeps azure_semantic_vad itself",
          type_of(model_td), type_of(agent_td))


# --------------------------------------- the binding is a deployment decision
def test_client_cannot_choose_binding():
    """The brain is chosen once, by VOICE_BINDING, and never by a client.

    There was briefly a per-session override gated on DEVELOPER_MODE. It was
    removed: which brain answers is not the same question as what the UI
    exposes, and a session-level switch makes behaviour depend on whoever
    opened the tab. Comparing the two bindings is done by redeploying.

    These checks exist so that invariant cannot regress quietly — a client can
    send anything it likes and must never move the binding.
    """
    from unittest.mock import Mock

    def handler_with(env, client_config):
        _fresh_config(**env)
        from backend.voice.handler import VoiceSessionHandler
        return VoiceSessionHandler(
            client_id="t", endpoint="https://x", credential=Mock(),
            send_message=Mock(), config=client_config,
        )

    h = handler_with({"VOICE_BINDING": "agent"}, {"voiceBinding": "model"})
    check("client asking for model in an agent deploy is ignored",
          h.model_binding, False)

    h = handler_with({"VOICE_BINDING": "model"}, {"voiceBinding": "agent"})
    check("client asking for agent in a model deploy is ignored",
          h.model_binding, True)

    # The load-bearing one: DEVELOPER_MODE used to unlock the override, so this
    # is what would break first if the gate were ever reintroduced.
    h = handler_with({"VOICE_BINDING": "agent", "DEVELOPER_MODE": "true"},
                     {"voiceBinding": "model"})
    check("DEVELOPER_MODE does not unlock a per-session override",
          h.model_binding, False)

    h = handler_with({"VOICE_BINDING": "model", "DEVELOPER_MODE": "true"},
                     {"voiceBinding": "agent"})
    check("DEVELOPER_MODE does not unlock the reverse override either",
          h.model_binding, True)

    h = handler_with({"VOICE_BINDING": "model"}, {})
    check("no voiceBinding in config -> deployment default", h.model_binding, True)

    h = handler_with({"VOICE_BINDING": "agent"}, {"voiceBinding": "banana"})
    check("unrecognised per-session value -> deployment default",
          h.model_binding, False)


# ------------------------------------------------------------ agent purity
def test_agent_mode_carries_no_model_keys():
    """The regression guard: agent mode must not gain model-mode session keys."""
    _fresh_config(VOICE_BINDING="agent")
    import backend.config as cfg

    # These knobs exist, but must be inert unless the model binding is on.
    check_true("REALTIME_MAX_TOKENS still defined", cfg.REALTIME_MAX_TOKENS > 0)
    check("agent deploy reports MODEL_BINDING False", cfg.MODEL_BINDING, False)

    from backend.voice.builders import build_interim_response
    from azure.ai.voicelive.models import AzureStandardVoice
    check("no interim_response in an agent deploy",
          build_interim_response(AzureStandardVoice(name="en-US-AvaMultilingualNeural")),
          None)


def test_connect_kwargs_match_the_installed_sdk() -> None:
    """Every key we hand ``connect()`` must be a real parameter of it.

    This is the check that would have caught a live outage. The SDK floor is a
    range, so a routine ``uv lock`` can move the installed version, and
    azure-ai-voicelive changed its agent binding from an ``agent_config`` dict
    to top-level ``agent_name``/``project_name``. Because ``connect()`` ends in
    ``**kwargs``, the stale key raised NOTHING — it was silently dropped, the
    session bound to neither an agent nor a model, and the service answered
    "Missing required parameter: model", which surfaced to us as an unrelated
    "Cannot write to closing transport" on the following write.

    So this deliberately asserts against ``inspect.signature`` of the SDK that
    is actually installed, rather than against a hardcoded list of names. It
    catches the next signature change too, not just the one already fixed.
    """
    import inspect
    from unittest.mock import Mock

    from azure.ai.voicelive.aio import connect

    accepted = {
        name
        for name, p in inspect.signature(connect).parameters.items()
        if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)
    }

    def kwargs_for(binding: str) -> dict:
        _fresh_config(VOICE_BINDING=binding, AGENT_NAME="a", AGENT_PROJECT_NAME="p",
                      VOICELIVE_MODEL="gpt-realtime")
        from backend.voice.handler import VoiceSessionHandler
        h = VoiceSessionHandler(
            client_id="t", endpoint="https://x", credential=Mock(),
            send_message=Mock(), config={},
        )
        return h._build_connect_kwargs()

    for binding in ("agent", "model"):
        kw = kwargs_for(binding)
        unknown = sorted(set(kw) - accepted)
        check(f"{binding} mode passes only parameters the SDK declares",
              unknown, [])

    agent_kw = kwargs_for("agent")
    check_true("agent mode binds via agent_name", "agent_name" in agent_kw)
    check_true("agent mode binds via project_name", "project_name" in agent_kw)
    # The specific shape that broke. Named explicitly so the failure message
    # points straight at the cause rather than at a generic 'unknown key'.
    check("agent mode does not pass a removed agent_config dict",
          "agent_config" in agent_kw, False)


def main() -> int:
    for fn in (test_binding_resolution, test_interim_response_gating,
               test_turn_detection_eou, test_client_cannot_choose_binding,
               test_agent_mode_carries_no_model_keys,
               test_connect_kwargs_match_the_installed_sdk):
        try:
            fn()
        except Exception as e:  # a raising test is a failing test
            FAILURES.append(f"{fn.__name__} raised {type(e).__name__}: {e}")

    print(f"\nvoice binding switch: {CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    for f in FAILURES:
        print(f"  FAIL {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
