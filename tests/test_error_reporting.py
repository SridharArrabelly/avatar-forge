"""Offline checks for descriptive, non-duplicated session errors."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.error_reporting import describe_error, describe_session_start_error  # noqa: E402


class ErrorDetails:
    code = "invalid_avatar"
    message = "Avatar character was not found"


class ErrorEvent:
    error = ErrorDetails()


def main() -> int:
    failures: list[str] = []
    checks = 0

    def check(label: str, actual, expected) -> None:
        nonlocal checks
        checks += 1
        if actual != expected:
            failures.append(f"{label}: expected {expected!r}, got {actual!r}")

    check(
        "SDK event message and code",
        describe_error(ErrorEvent(), "fallback"),
        "Avatar character was not found (code: invalid_avatar)",
    )
    check(
        "mapping error detail",
        describe_error({"error": {"message": "Bad model", "code": "invalid_model"}}, "fallback"),
        "Bad model (code: invalid_model)",
    )
    check(
        "unsupported model gets actionable remediation",
        describe_session_start_error(
            RuntimeError(
                "'session.input_audio_transcription.model' rejected "
                "'mai-transcribe-2'"
            ),
            {},
        ),
        "Speech recognition model 'mai-transcribe-2' is not supported by "
        "Voice Live. Set SR_MODEL=mai-transcribe.",
    )
    avatar_error = describe_session_start_error(
        Exception(),
        {
            "avatarEnabled": True,
            "avatarName": "Nuru2",
            "isPhotoAvatar": True,
            "isCustomAvatar": False,
        },
    )
    check("empty SDK exception names avatar", "Nuru2" in avatar_error, True)
    check("empty SDK exception gives required type", "AVATAR_TYPE=custom-photo" in avatar_error, True)
    check("empty SDK exception names resource", "AZURE_VOICELIVE_ENDPOINT" in avatar_error, True)

    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    check(
        "notifySystem uses chat only in developer mode",
        "if (isDeveloperMode) addMessage('system', message, false, true);" in app_js,
        True,
    )
    session_error_case = app_js[
        app_js.index("case 'session_error':"):app_js.index("case 'error':")
    ]
    check(
        "session errors use one dedicated renderer",
        "showSessionError(msg.error);" in session_error_case,
        True,
    )
    check(
        "session errors do not emit a separate toast",
        "notifySystem(" in session_error_case,
        False,
    )
    check("unknown error label removed", "Unknown error" in app_js, False)

    print(f"error reporting: {checks - len(failures)}/{checks} checks passed")
    for failure in failures:
        print(f"  FAIL {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
