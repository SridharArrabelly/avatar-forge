"""Tests for the RBAC-propagation wait used by the postprovision scripts.

Needs no Azure and no credentials — it drives `wait_for_data_plane` against
callables that raise on demand.

The behaviour worth pinning is the discrimination: a 401/403 means "the role
assignment has not propagated yet, keep waiting", but a 404 means "the thing
genuinely is not there" and must surface immediately, because both setup
scripts rely on catching ResourceNotFoundError to produce their actionable
messages (a required AI Search connection is fatal; an optional Bing one is a
warning). If the wait swallowed 404s, a missing Bing connection would stall the
deploy for 15 minutes and then fail instead of degrading gracefully.

    uv run python scripts/test_rbac_propagation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rbac_propagation import is_propagation_error, wait_for_data_plane  # noqa: E402


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpError(Exception):
    """Stands in for openai.AuthenticationError / azure ClientAuthenticationError."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.response = _Response(status_code)


class _NotFound(Exception):
    def __init__(self) -> None:
        super().__init__("(ResourceNotFound) not found")
        self.response = _Response(404)


_failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        _failures.append(name)


def main() -> int:
    print("rbac_propagation")

    # --- the predicate -----------------------------------------------------
    check("401 is propagation-shaped", is_propagation_error(_HttpError(401, "PermissionDenied")))
    check("403 is propagation-shaped", is_propagation_error(_HttpError(403, "Forbidden")))
    check("404 is NOT propagation-shaped", not is_propagation_error(_NotFound()))
    check(
        "message-only match works when no status is exposed",
        is_propagation_error(Exception("Error code: 401 - {'code': 'PermissionDenied'}")),
    )
    check("unrelated error is not retried", not is_propagation_error(ValueError("bad input")))

    # --- returns the value, no waiting on success --------------------------
    check("returns the call's result", wait_for_data_plane(lambda: 42, what="x", log=lambda _: None) == 42)

    # --- retries a 401, then succeeds --------------------------------------
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _HttpError(401, "PermissionDenied")
        return "ready"

    logged: list[str] = []
    result = wait_for_data_plane(flaky, what="probing", timeout_s=120, log=logged.append)
    check("retries until the role lands", result == "ready" and attempts["n"] == 3)
    check("explains itself while waiting", any("eventually consistent" in m for m in logged))

    # --- a 404 must escape immediately -------------------------------------
    calls = {"n": 0}

    def missing() -> None:
        calls["n"] += 1
        raise _NotFound()

    try:
        wait_for_data_plane(missing, what="probing", timeout_s=120, log=lambda _: None)
        check("404 propagates to the caller", False)
    except _NotFound:
        check("404 propagates to the caller", True)
    check("404 is not retried", calls["n"] == 1)

    # --- gives up rather than hanging forever ------------------------------
    def always_denied() -> None:
        raise _HttpError(401, "PermissionDenied")

    msgs: list[str] = []
    try:
        wait_for_data_plane(always_denied, what="probing", timeout_s=1, log=msgs.append)
        check("times out and re-raises", False)
    except _HttpError:
        check("times out and re-raises", True)
    check("timeout names the missing role", any("Foundry User" in m for m in msgs))

    print(f"\n{'FAILED: ' + ', '.join(_failures) if _failures else 'All checks passed.'}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
