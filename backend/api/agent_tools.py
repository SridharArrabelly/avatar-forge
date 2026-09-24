"""HTTP tools the Foundry agent calls server-side, through an OpenAPI tool.

In agent binding Foundry runs the agent's tools itself, so a tool that has to
enforce *our* rules cannot be a native one. Web grounding is the case in point.
Web IQ has no server-side allow-list, so the scope lives in
``backend/voice/tools.py``: the ``site:`` operators, the staging-mirror filter
and the passage/result budget. Exposing that same ``search_web()`` here lets the
agent use Web IQ with exactly the boundary model mode already enforces, rather
than a second, weaker copy of it written into an OpenAPI description.

The route is on the public container app, so the caller check matters more than
the body. There are two ways to pass it, and a deployment is configured for
exactly one. A key wins when both are present:

``key``
    A shared key in ``x-tool-key``, held twice: as a container-app secret
    (``AGENT_WEB_TOOL_KEY``) and in a Foundry project connection of type Custom
    keys, which Foundry injects on every call.

``entra``
    No shared secret. Foundry calls with a token from its **account's**
    system-assigned managed identity, for an audience that is the Application
    ID URI of an Entra app registration made for this deployment. We accept the
    call only if Entra signed the token, for that audience, in our tenant, *and*
    the token's ``oid`` is one of the identities we expect. The ``oid`` check is
    the authorisation: any identity in the tenant can ask Entra for a token to
    an app registration, so audience alone proves nothing about who is calling.

With neither configured the route answers 404, so a deployment that does not
use the tool does not expose it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from typing import Any

import httpx
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from ..voice.tools import search_web

logger = logging.getLogger(__name__)

TOOL_KEY_ENV = "AGENT_WEB_TOOL_KEY"
TOOL_KEY_HEADER = "x-tool-key"
AUDIENCE_ENV = "AGENT_WEB_TOOL_AUDIENCE"
# Optional second accepted audience. A v1 token carries the Application ID URI
# as `aud`; a v2 token carries the app's client ID instead. Foundry gets v1
# tokens from an app registration that does not opt into v2, but accepting the
# client ID too means a later manifest change does not break every call.
APP_ID_ENV = "AGENT_WEB_TOOL_APP_ID"
CALLER_OIDS_ENV = "AGENT_WEB_TOOL_CALLER_OIDS"
TENANT_ENV = "AGENT_WEB_TOOL_TENANT_ID"
SEARCH_WEB_PATH = "/api/tools/search-web"

AUTHORITY = "https://login.microsoftonline.com"
# Entra rotates signing keys with overlap and publishes the new one well ahead
# of use, so a day-old set is normally fine. An unknown `kid` still forces a
# refresh, rate-limited so a stream of junk tokens cannot turn into a stream
# of requests to Entra.
JWKS_TTL_S = 24 * 3600
JWKS_MIN_REFRESH_S = 300
JWKS_TIMEOUT = httpx.Timeout(5.0)
CLOCK_SKEW_S = 60

# Generous on purpose. build_query() trims the question to fit Web IQ's 1000-char
# budget, so a long model query should be trimmed, not rejected with a 422 that
# the agent sees as a failed tool call. The cap only bounds the request size.
MAX_QUERY_CHARS = 4000

router = APIRouter()


class SearchWebRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_CHARS)


def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


def auth_mode() -> str | None:
    """``"key"``, ``"entra"``, or None when the tool is not enabled here."""
    if os.getenv(TOOL_KEY_ENV):
        return "key"
    if os.getenv(AUDIENCE_ENV) and os.getenv(TENANT_ENV) and _csv(CALLER_OIDS_ENV):
        return "entra"
    return None


def tool_enabled() -> bool:
    return auth_mode() is not None


class _Jwks:
    def __init__(self, keys: dict[str, Any], tenant: str, fetched_at: float) -> None:
        self.keys = keys
        self.tenant = tenant
        self.fetched_at = fetched_at


_jwks: _Jwks | None = None
_jwks_lock = asyncio.Lock()


async def _fetch_jwks(tenant: str) -> dict[str, Any]:
    url = f"{AUTHORITY}/{tenant}/discovery/v2.0/keys"
    async with httpx.AsyncClient(timeout=JWKS_TIMEOUT) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


async def _signing_key(tenant: str, kid: str) -> Any | None:
    """The tenant's public key for ``kid``, from a cached, self-refreshing JWKS.

    A failed refresh keeps the previous set rather than dropping it, so a brief
    Entra outage does not take the tool down while the keys we hold are valid.
    """
    global _jwks
    now = time.monotonic()
    cached = _jwks
    if cached and cached.tenant == tenant and kid in cached.keys and now - cached.fetched_at < JWKS_TTL_S:
        return cached.keys[kid]
    async with _jwks_lock:
        cached = _jwks
        fresh = cached is not None and cached.tenant == tenant
        age = now - cached.fetched_at if fresh else None
        if not fresh or age >= JWKS_TTL_S or (kid not in cached.keys and age >= JWKS_MIN_REFRESH_S):
            try:
                keyset = jwt.PyJWKSet.from_dict(await _fetch_jwks(tenant))
            except Exception as exc:  # network, HTTP status, or an unusable key set
                logger.warning("Agent web tool: could not refresh Entra signing keys (%s).", exc)
            else:
                keys = {k.key_id: k for k in keyset.keys if k.key_id}
                _jwks = _Jwks(keys, tenant, time.monotonic())
        cached = _jwks
        if cached is None or cached.tenant != tenant:
            # Nothing to verify against: a transient failure, not the caller's
            # fault, so say "try again" rather than "go away".
            raise HTTPException(status_code=503)
        return cached.keys.get(kid)


async def prewarm() -> None:
    """Fetch the signing keys at startup so the agent's first call does not."""
    if auth_mode() != "entra":
        return
    try:
        await _signing_key(os.environ[TENANT_ENV], "")
    except HTTPException:
        pass


async def _verify_entra(authorization: str | None) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401)
    token = token.strip()
    tenant = os.environ[TENANT_ENV]
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401) from None
    kid = header.get("kid")
    # Pinned: never let the token choose its own algorithm (alg=none, or HS256
    # keyed with our public key).
    if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
        raise HTTPException(status_code=401)
    key = await _signing_key(tenant, kid)
    if key is None:
        raise HTTPException(status_code=401)
    audiences = [a for a in (os.getenv(AUDIENCE_ENV), os.getenv(APP_ID_ENV)) if a]
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=audiences,
            issuer=(f"https://sts.windows.net/{tenant}/", f"{AUTHORITY}/{tenant}/v2.0"),
            leeway=CLOCK_SKEW_S,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        logger.info("Agent web tool: rejected a bearer token (%s).", type(exc).__name__)
        raise HTTPException(status_code=401) from None
    oid = claims.get("oid")
    if claims.get("tid") != tenant or oid not in _csv(CALLER_OIDS_ENV):
        # A genuine token from our tenant, for our audience, from someone we did
        # not expect. Worth a warning: it is either a misconfigured caller list
        # or something probing the route. The IDs are not secrets.
        logger.warning(
            "Agent web tool: token for the right audience from an unexpected caller "
            "(oid=%s, appid=%s). Allowed callers are in %s.",
            oid, claims.get("appid") or claims.get("azp"), CALLER_OIDS_ENV,
        )
        raise HTTPException(status_code=403)


async def require_caller(
    presented_key: str | None = Header(default=None, alias=TOOL_KEY_HEADER),
    authorization: str | None = Header(default=None),
) -> None:
    """404 when the tool is not enabled here, 401/403 for a caller we do not accept.

    Runs as a route dependency, so it is decided before the body is validated —
    an unauthenticated caller cannot use 422 responses to probe the schema.
    """
    mode = auth_mode()
    if mode is None:
        raise HTTPException(status_code=404)
    if mode == "key":
        expected = os.environ[TOOL_KEY_ENV]
        if not presented_key or not hmac.compare_digest(presented_key.encode(), expected.encode()):
            raise HTTPException(status_code=401)
        return
    await _verify_entra(authorization)


@router.post(SEARCH_WEB_PATH, include_in_schema=False, dependencies=[Depends(require_caller)])
async def agent_search_web(body: SearchWebRequest) -> dict:
    """The model-mode ``search_web`` tool, unchanged, for the Foundry agent.

    Errors come back as a 200 with an ``error`` field, the same shape model mode
    hands its model, so the agent can say it found nothing instead of the
    response failing on a tool error.
    """
    return await search_web(body.query)
