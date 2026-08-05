"""Offline check: every surface calls the assistant by the SAME name.

Needs **no Azure resources and no credentials**. Runs in well under a second.

Why it exists: the persona name was resolved independently in five places and
they disagreed. The web stage derived a friendly name from the *selected avatar
model*, while the Foundry agent prompt, the bot welcome, the wake phrase, the
Teams package name and the meeting roster all fell back to the literal
``"Avatar"``. A deployment running the photo avatar ``Simone`` therefore showed
"Simone" on screen and answered "I am Avatar, your executive assistant".

The agent prompt had a second, independent bug: the name was substituted into
the prompt at *import* time, which happens before ``load_settings()`` calls
``load_dotenv()`` — so setting ``AVATAR_DISPLAY_NAME`` in ``.env`` and re-running
the script by hand (the documented recovery path) silently had no effect.

What it pins:

* resolution order: ``AVATAR_DISPLAY_NAME`` > active avatar model > ``"Avatar"``
* the ``IS_*`` gates select which model variable is read — a stale
  ``CUSTOM_AVATAR_NAME`` is ignored unless ``IS_CUSTOM_AVATAR`` is on
* model suffixes are stripped (``Lisa-casual-sitting`` -> ``Lisa``)
* the agent prompt substitutes ``{{AVATAR_NAME}}`` with the resolved name, read
  at call time rather than frozen at import
* ``backend.config`` and ``/api/config`` agree with the resolver
* the exact reported case: photo avatar "Simone" -> the agent says "Simone"
* ``scripts/rename_avatar.py`` still writes enough variables to actually rename a
  deployed environment, and leaves the avatar's *character* alone -- whichever
  variable holds it in the current mode

Run from the repo root:

    uv run python tests/test_avatar_identity.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.avatar_identity import (  # noqa: E402
    active_avatar_model,
    resolve_avatar_display_name,
)

# Every variable that can influence the resolved name. Cleared before each case so
# a developer's own shell/.env cannot make the suite pass or fail spuriously.
_ENV_KEYS = (
    "AVATAR_DISPLAY_NAME",
    "AVATAR_NAME",
    "CUSTOM_AVATAR_NAME",
    "PHOTO_AVATAR_NAME",
    "IS_CUSTOM_AVATAR",
    "IS_PHOTO_AVATAR",
)

_failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        _failures.append(label)


def resolve(env: dict[str, str]) -> str:
    """Resolve against an explicit mapping (no process state involved)."""
    return resolve_avatar_display_name(env)


def main() -> int:
    print("1. Resolution order")
    check(
        "explicit knob wins over the model",
        resolve({"AVATAR_DISPLAY_NAME": "Nuru", "IS_PHOTO_AVATAR": "true",
                 "PHOTO_AVATAR_NAME": "Simone"}),
        "Nuru",
    )
    check("knob used verbatim (spaces kept)",
          resolve({"AVATAR_DISPLAY_NAME": "Ada Lovelace"}), "Ada Lovelace")
    check("knob is trimmed", resolve({"AVATAR_DISPLAY_NAME": "  Nuru  "}), "Nuru")
    check("whitespace-only knob is not a name",
          resolve({"AVATAR_DISPLAY_NAME": "   ", "AVATAR_NAME": "Lisa-casual-sitting"}),
          "Lisa")

    print("\n2. Derived from the active avatar model")
    # The reported case: nothing branded, photo avatar Simone selected.
    check("REPORTED CASE photo avatar Simone -> Simone",
          resolve({"IS_PHOTO_AVATAR": "true", "PHOTO_AVATAR_NAME": "Simone"}), "Simone")
    check("custom avatar Nuru -> Nuru",
          resolve({"IS_CUSTOM_AVATAR": "true", "CUSTOM_AVATAR_NAME": "Nuru"}), "Nuru")
    check("standard avatar suffix stripped",
          resolve({"AVATAR_NAME": "Lisa-casual-sitting"}), "Lisa")
    check("standard avatar, multi-suffix",
          resolve({"AVATAR_NAME": "Harry-business-standing"}), "Harry")
    check("nothing set at all -> default standard avatar's name",
          resolve({}), "Lisa")

    print("\n3. IS_* gates decide which model variable is read")
    # The reason deriving from the model is safe: CUSTOM_AVATAR_NAME is a Speech
    # model id that is stale/empty unless its gate is on, so it must be ignored.
    check("stale CUSTOM_AVATAR_NAME ignored when its gate is off",
          resolve({"IS_CUSTOM_AVATAR": "false", "CUSTOM_AVATAR_NAME": "StaleModelId",
                   "AVATAR_NAME": "Lisa-casual-sitting"}),
          "Lisa")
    check("stale PHOTO_AVATAR_NAME ignored when its gate is off",
          resolve({"IS_PHOTO_AVATAR": "false", "PHOTO_AVATAR_NAME": "Simone",
                   "AVATAR_NAME": "Lisa-casual-sitting"}),
          "Lisa")
    check("custom outranks photo when both gates are on",
          resolve({"IS_CUSTOM_AVATAR": "true", "CUSTOM_AVATAR_NAME": "Nuru",
                   "IS_PHOTO_AVATAR": "true", "PHOTO_AVATAR_NAME": "Simone"}),
          "Nuru")
    for spelling in ("true", "TRUE", "1", "yes", "on", " true "):
        check(f"gate accepts {spelling!r}",
              resolve({"IS_PHOTO_AVATAR": spelling, "PHOTO_AVATAR_NAME": "Simone"}),
              "Simone")
    check("photo default applies when the gate is on but no name is set",
          resolve({"IS_PHOTO_AVATAR": "true"}), "Anika")

    print("\n4. Never empty")
    check("gate on with an empty model id falls back to Avatar",
          resolve({"IS_CUSTOM_AVATAR": "true", "CUSTOM_AVATAR_NAME": ""}), "Avatar")
    check("blank standard model falls back to the default, not empty",
          resolve({"AVATAR_NAME": "   "}), "Lisa")
    check("active model honours the same gates",
          active_avatar_model({"IS_PHOTO_AVATAR": "true", "PHOTO_AVATAR_NAME": "Simone"}),
          "Simone")

    print("\n5. Agent prompt substitution (the bug the user reported)")
    saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    try:
        os.environ["IS_PHOTO_AVATAR"] = "true"
        os.environ["PHOTO_AVATAR_NAME"] = "Simone"
        # Imported AFTER the environment is set, and — critically — the module must
        # still pick up a LATER change, which is what the frozen constant broke.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import setup_foundry_agent as sfa

        prompt = sfa._load_prompt("agent", "instructions.md")
        check("prompt no longer contains the placeholder",
              "{{AVATAR_NAME}}" in prompt, False)
        check("prompt opens with the resolved name",
              prompt.splitlines()[0].startswith("You are Simone,"), True)
        check("prompt does NOT say 'You are Avatar'",
              prompt.splitlines()[0].startswith("You are Avatar,"), False)
        check("description is branded too",
              "{{AVATAR_NAME}}" in sfa.agent_description(), False)

        # Change the environment after import: a constant captured at import time
        # would keep returning "Simone" here.
        os.environ["AVATAR_DISPLAY_NAME"] = "Nuru"
        reread = sfa._load_prompt("agent", "instructions.md")
        check("re-reads the environment after import (not frozen)",
              reread.splitlines()[0].startswith("You are Nuru,"), True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\n6. backend.config and /api/config agree with the resolver")
    saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS + ("ACS_WAKE_PHRASES",)}
    try:
        os.environ["IS_PHOTO_AVATAR"] = "true"
        os.environ["PHOTO_AVATAR_NAME"] = "Simone"
        # config.py resolves at import time (module constants), so import it here
        # with the environment already in place.
        for mod in [m for m in sys.modules if m.startswith("backend.config")]:
            del sys.modules[mod]
        from backend import config as cfg

        # config.py runs load_dotenv(override=True) at import, so a developer's
        # local .env can legitimately win here. Compare against the resolver read
        # from that same post-dotenv environment: what this pins is PARITY between
        # the surfaces, which is the property that broke. The resolver's own answer
        # is pinned against fixed inputs in sections 1-4.
        expected = resolve_avatar_display_name()
        raw_knob = os.environ.get("AVATAR_DISPLAY_NAME", "").strip()
        check("config.AVATAR_DISPLAY_NAME matches the resolver",
              cfg.AVATAR_DISPLAY_NAME, expected)
        check("resolver is not falling back to the literal (env is honoured)",
              expected, "Simone" if not raw_knob else raw_knob)
        defaults = cfg.get_ui_defaults()
        check("/api/config assistantName is resolved", defaults["assistantName"], expected)
        check("/api/config avatarDisplayName stays RAW (app.js derives live)",
              defaults["avatarDisplayName"], raw_knob)
        check("wake phrases follow the name", cfg.ACS_WAKE_PHRASES,
              [f"hey {expected.lower()}", expected.lower()])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\n7. scripts/rename_avatar.py writes enough to actually rename")
    # The script is only correct while the variables it writes still outrank
    # everything else the resolver reads. Adding a new, higher-priority input to
    # avatar_identity would silently make renames stop taking effect on an
    # already-deployed environment -- a failure with no error message, which is
    # the kind this suite exists to catch.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import rename_avatar as ra

    check("every variable it writes is one the resolver reads",
          set(ra.RENAME_VARS) <= set(_ENV_KEYS), True)
    # A branding rename must never move the avatar's *character*: point the
    # renderer at a character Speech cannot resolve and the avatar stops
    # appearing with no error on any surface. Which variable holds the character
    # depends on the mode, so the default rename touches none of them -- only an
    # explicit --model may, and only the variable that mode actually reads.
    check("does NOT touch the prebuilt photo character",
          "PHOTO_AVATAR_NAME" in ra.RENAME_VARS, False)
    check("does NOT touch the custom-trained character",
          "CUSTOM_AVATAR_NAME" in ra.RENAME_VARS, False)

    # The prebuilt catalogue is parsed out of the UI's own picker so it cannot
    # drift from what a user can select. It is authoritative ONLY while
    # IS_CUSTOM_AVATAR is false: a custom-trained avatar lives in your own Speech
    # resource and is absent from this list by definition, so absence here says
    # nothing about whether that avatar exists.
    catalogue = ra.valid_photo_characters()
    check("the prebuilt catalogue parses out of the UI picker",
          "Simone" in catalogue and len(catalogue) > 20, True)

    # The flag/name combinations, and which variable each one puts on the wire.
    # Transcribed from frontend/app.js, which is what actually decides. Two of
    # the mismatched combinations fail *silently*, which is why the script has to
    # check the effective variable rather than a variable that is merely present.
    photo_custom = {"IS_PHOTO_AVATAR": "true", "IS_CUSTOM_AVATAR": "true"}
    photo_only = {"IS_PHOTO_AVATAR": "true", "IS_CUSTOM_AVATAR": "false"}
    check("custom on: reads CUSTOM_AVATAR_NAME, so PHOTO_AVATAR_NAME is inert",
          ra.character_var({**photo_custom, "CUSTOM_AVATAR_NAME": "Nuru",
                            "PHOTO_AVATAR_NAME": "Simone"}),
          "CUSTOM_AVATAR_NAME")
    check("custom on but no custom name: falls back to PHOTO_AVATAR_NAME",
          ra.character_var({**photo_custom, "CUSTOM_AVATAR_NAME": "",
                            "PHOTO_AVATAR_NAME": "Simone"}),
          "PHOTO_AVATAR_NAME")
    check("custom off: reads PHOTO_AVATAR_NAME even with a custom name present",
          ra.character_var({**photo_only, "CUSTOM_AVATAR_NAME": "Nuru",
                            "PHOTO_AVATAR_NAME": "Simone"}),
          "PHOTO_AVATAR_NAME")
    check("both gates off: reads AVATAR_NAME",
          ra.character_var({"IS_PHOTO_AVATAR": "false", "IS_CUSTOM_AVATAR": "false",
                            "CUSTOM_AVATAR_NAME": "Nuru", "PHOTO_AVATAR_NAME": "Simone"}),
          "AVATAR_NAME")

    # A live environment already branded Simone, with both gates on and a
    # character trained in its own Speech resource: the hostile case for a
    # rename, and the shape the running deployment actually has.
    deployed = {
        "AVATAR_DISPLAY_NAME": "Simone",
        "PHOTO_AVATAR_NAME": "Simone",
        "AVATAR_NAME": "Lisa-casual-sitting",
        "CUSTOM_AVATAR_NAME": "TrainedCharacter",
        "IS_PHOTO_AVATAR": "true",
        "IS_CUSTOM_AVATAR": "true",
    }
    renamed = {**deployed, **{k: "Nuru" for k in ra.RENAME_VARS}}
    check("its overrides beat every pre-existing identity variable",
          resolve(renamed), "Nuru")
    check("and the *effective* Speech character is left untouched",
          renamed[ra.character_var(renamed)], "TrainedCharacter")

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + ", ".join(_failures))
        return 1
    print("All avatar identity checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
