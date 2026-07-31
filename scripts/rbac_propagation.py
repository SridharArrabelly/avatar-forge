"""Retry a data-plane call while a freshly-created Azure role assignment propagates.

Why this exists
---------------
`azd provision` creates the role assignments the postprovision scripts need, then
runs those scripts seconds later. Azure RBAC is eventually consistent, and the
Cognitive Services data plane is one of the slower places for it to land — on a
greenfield deploy this was measured at roughly 15 minutes between the assignment
being created and the first successful call. Until then every request comes back
`401 PermissionDenied`, which looks exactly like a missing role and sends you off
debugging RBAC that is already correct.

So the first data-plane call of each script is wrapped in this wait. It reports
what it is doing rather than hanging silently, because a deploy that appears to
have stalled is worse than one that explains itself.

This is deliberately NOT a general-purpose retry: it only retries the
propagation-shaped failures (401/403), so a genuinely missing role still fails —
just after the wait, with the same actionable message as before.
"""
from __future__ import annotations

import os
import time
from typing import Callable, TypeVar

T = TypeVar("T")

# Long, because the failure mode it covers is slow. Measured at ~15 minutes on a
# greenfield deploy, so the budget has headroom above that. Overridable for CI,
# where waiting out a misconfiguration is worse than failing fast.
DEFAULT_TIMEOUT_S = int(os.environ.get("RBAC_PROPAGATION_TIMEOUT_S", "1200"))


def is_propagation_error(exc: BaseException) -> bool:
    """True when `exc` looks like RBAC that has not landed yet, rather than a real denial.

    Both SDKs in play raise different types for this (openai.AuthenticationError
    and azure.core.exceptions.ClientAuthenticationError), so match on the wire
    status and the service's message rather than on the exception class.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return True
    text = str(exc)
    return "PermissionDenied" in text or "does not have access to API/Operation" in text


def wait_for_data_plane(
    call: Callable[[], T],
    *,
    what: str,
    timeout_s: int | None = None,
    log: Callable[[str], None] = print,
) -> T:
    """Call `call()`, retrying propagation-shaped auth failures until `timeout_s`.

    Returns whatever `call()` returns. Re-raises the last exception if the
    timeout is reached, or immediately for any error that is not RBAC-shaped.
    """
    budget = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    started = time.monotonic()
    delay = 15
    announced = False

    while True:
        try:
            result = call()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
            if not is_propagation_error(exc):
                raise
            elapsed = time.monotonic() - started
            if elapsed + delay >= budget:
                log(
                    f"ERROR: still denied on {what} after {int(elapsed)}s. This is no longer\n"
                    "       explainable by RBAC propagation - the role is probably missing.\n"
                    "       The deploying identity needs 'Foundry User' on the Foundry ACCOUNT\n"
                    "       (subscription Owner/Contributor do NOT grant data-plane access)."
                )
                raise
            if not announced:
                log(
                    f"Waiting for role assignments to take effect before {what}.\n"
                    "  Azure RBAC is eventually consistent and the Cognitive Services data plane\n"
                    "  can take several minutes to catch up with a just-created assignment.\n"
                    f"  Retrying for up to {budget // 60} minutes (RBAC_PROPAGATION_TIMEOUT_S to change)."
                )
                announced = True
            log(f"  not ready yet ({int(elapsed)}s elapsed) - retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        if announced:
            log(f"  RBAC ready after {int(time.monotonic() - started)}s.")
        return result
