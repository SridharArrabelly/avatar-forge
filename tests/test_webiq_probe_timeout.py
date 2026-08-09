"""Offline check: a stalled Web IQ token request cannot hang a conversation.

Needs **no Azure resources and no credentials**. Runs in about a second.

Why it exists: ``search_web`` is advertised only when Web IQ is reachable, and
with no API key that question is answered by asking for a token
(``web_search_available()``). Session setup awaits that answer --
``_setup_session`` -> ``build_realtime_tools()`` -> ``web_search_available()``
-- so however the probe ends, it has to end.

"No answer" is a real outcome, not a hypothetical. Measured on a dev box::

    az account get-access-token --resource https://ai.azure.com      -> token, instantly
    az account get-access-token --resource https://api.microsoft.ai  -> nothing, minutes

A token request for a scope that is not consented in the tenant can stall
instead of refusing. Unbounded, that stall reaches session setup and the first
conversation never starts -- strictly worse than the silently-missing tool this
whole mechanism replaced. So a timeout is a "no".

What this pins, using a credential that never returns:

* the probe gives up and returns False rather than hanging
* it gives up within its own stated bound
* ``build_realtime_tools()`` still produces a usable tool set, minus search_web
* the verdict is cached -- a second session does not re-pay the timeout
* an API key short-circuits the probe entirely (no token call at all)

Run from the repo root:

    uv run python tests/test_webiq_probe_timeout.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}", end="")
    if not ok:
        print(f": expected {want!r}, got {got!r}", end="")
        FAILURES.append(label)
    print()


class HangingCredential:
    """A credential whose token request never completes."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_token(self, *scopes, **kwargs):
        self.calls += 1
        await asyncio.sleep(3600)

    async def close(self) -> None:
        pass


class CountingCredential:
    """Records whether a token was ever requested."""

    def __init__(self) -> None:
        self.calls = 0

    async def get_token(self, *scopes, **kwargs):
        self.calls += 1
        raise AssertionError("a key was set; the probe should not have run")

    async def close(self) -> None:
        pass


def _reset(tools, credential) -> None:
    """Install a fake credential and clear any cached verdict."""
    tools._aad_credential = credential
    tools._web_probe = None


async def main() -> int:
    os.environ.pop("WEBIQ_API_KEY", None)
    from backend.voice import tools

    bound = tools.WEBIQ_PROBE_TIMEOUT_S
    print(f"Stalled credential, probe bound {bound:.0f}s")
    print("-" * 62)

    cred = HangingCredential()
    _reset(tools, cred)

    started = time.monotonic()
    available = await tools.web_search_available()
    elapsed = time.monotonic() - started

    check("a stalled token request resolves to 'unavailable'", available, False)
    check("the probe actually ran", cred.calls, 1)
    # Allow slack for scheduling, but nowhere near the 3600s the fake would take.
    within = elapsed < bound + 5
    check(f"it gave up in {elapsed:.1f}s, inside its {bound:.0f}s bound", within, True)

    print()
    print("Session setup survives it")
    print("-" * 62)
    names = [t["name"] for t in await tools.build_realtime_tools()]
    check("search_web is withheld", "search_web" in names, False)
    check("the other tools still ship", len(names) > 0, True)

    print()
    print("The verdict is cached, so only the first probe pays")
    print("-" * 62)
    started = time.monotonic()
    again = await tools.web_search_available()
    second = time.monotonic() - started
    check("same answer", again, False)
    check("no second token request", cred.calls, 1)
    check(f"returned immediately ({second:.2f}s)", second < 1.0, True)

    print()
    print("An API key short-circuits the probe")
    print("-" * 62)
    counting = CountingCredential()
    _reset(tools, counting)
    os.environ["WEBIQ_API_KEY"] = "k"
    try:
        keyed = await tools.web_search_available()
    finally:
        os.environ.pop("WEBIQ_API_KEY", None)
    check("available with a key", keyed, True)
    check("no token was requested", counting.calls, 0)

    tools._aad_credential = None
    tools._web_probe = None

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
