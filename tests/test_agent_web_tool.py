"""The agent's Web IQ tool endpoint: off unless configured, and caller-checked first.

The route is on the public container app, so its guard matters more than its
body. It must not exist without a caller check configured (404), must reject a
caller it cannot verify *before* validating the body, and must hand the query to
the same ``search_web()`` model mode uses.

Both caller checks are exercised. The Entra one signs real RS256 tokens with a
throwaway key and serves the matching JWKS from a fake, so every claim check —
signature, algorithm, audience, issuer, expiry, tenant, caller — runs for real.

    uv run --no-sync python tests/test_agent_web_tool.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.api import agent_tools  # noqa: E402

checks = 0
failures: list[str] = []


def check(label: str, got: Any, want: Any) -> None:
    global checks
    checks += 1
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}: expected {want!r}, got {got!r}")
        failures.append(label)


calls: list[str] = []


async def _fake_search_web(query: str) -> dict:
    calls.append(query)
    return {"results": [{"title": "t", "url": "https://www.mtn.com/x"}], "note": "n"}


ENVS = (
    agent_tools.TOOL_KEY_ENV, agent_tools.AUDIENCE_ENV, agent_tools.APP_ID_ENV,
    agent_tools.CALLER_OIDS_ENV, agent_tools.TENANT_ENV,
)


def reset_env(**values: str) -> None:
    for name in ENVS:
        os.environ.pop(name, None)
    os.environ.update(values)


agent_tools.search_web = _fake_search_web
app = FastAPI()
app.include_router(agent_tools.router)
client = TestClient(app)
PATH = agent_tools.SEARCH_WEB_PATH
KEY = "k" * 36


def post(body: Any, key: str | None = None, bearer: str | None = None):
    headers = {}
    if key is not None:
        headers[agent_tools.TOOL_KEY_HEADER] = key
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return client.post(PATH, json=body, headers=headers)


print("disabled when nothing is configured")
reset_env()
check("nothing configured -> 404", post({"query": "x"}, KEY).status_code, 404)
check("nothing configured, no header -> 404", post({"query": "x"}).status_code, 404)
check("audience alone is not enough -> 404", (reset_env(**{agent_tools.AUDIENCE_ENV: "api://a"}), post({"query": "x"}).status_code)[1], 404)
reset_env()

print("key mode: key checked before the body")
reset_env(**{agent_tools.TOOL_KEY_ENV: KEY})
check("mode is key", agent_tools.auth_mode(), "key")
check("missing header -> 401", post({"query": "x"}).status_code, 401)
check("wrong key -> 401", post({"query": "x"}, "wrong").status_code, 401)
check("wrong key + invalid body -> 401, not 422", post({"nope": 1}, "wrong").status_code, 401)
check("empty header -> 401", post({"query": "x"}, "").status_code, 401)
check("no search made for rejected calls", calls, [])

print("key mode: authorised calls reach search_web unchanged")
r = post({"query": "MTN Group CFO"}, KEY)
check("right key -> 200", r.status_code, 200)
check("query passed through verbatim", calls, ["MTN Group CFO"])
check("search_web result returned as-is", r.json()["results"][0]["url"], "https://www.mtn.com/x")

print("body bounds")
check("empty query -> 422", post({"query": ""}, KEY).status_code, 422)
check("missing query -> 422", post({}, KEY).status_code, 422)
check("query at the cap accepted", post({"query": "a" * agent_tools.MAX_QUERY_CHARS}, KEY).status_code, 200)
check("query over the cap -> 422", post({"query": "a" * (agent_tools.MAX_QUERY_CHARS + 1)}, KEY).status_code, 422)

print("not advertised")
check("route hidden from the app's OpenAPI schema", PATH in app.openapi().get("paths", {}), False)

# ── Entra mode ──────────────────────────────────────────────────────────────
TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "api://22222222-2222-2222-2222-222222222222"
APP_ID = "22222222-2222-2222-2222-222222222222"
FOUNDRY_OID = "33333333-3333-3333-3333-333333333333"
PROJECT_OID = "44444444-4444-4444-4444-444444444444"
STRANGER_OID = "55555555-5555-5555-5555-555555555555"
KID = "test-kid"

signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
jwk.update(kid=KID, use="sig", alg="RS256")
jwks_fetches: list[str] = []
jwks_fail = False


async def _fake_fetch_jwks(tenant: str) -> dict:
    jwks_fetches.append(tenant)
    if jwks_fail:
        raise RuntimeError("entra unreachable")
    return {"keys": [jwk]}


agent_tools._fetch_jwks = _fake_fetch_jwks


def token(*, key=signing_key, kid: str | None = KID, alg: str = "RS256", **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "aud": AUDIENCE, "iss": f"https://sts.windows.net/{TENANT}/", "tid": TENANT,
        "oid": FOUNDRY_OID, "appid": "foundry-app", "iat": now, "nbf": now, "exp": now + 3600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims, key, algorithm=alg, headers=headers)


print("entra mode: configuration")
reset_env(**{
    agent_tools.AUDIENCE_ENV: AUDIENCE, agent_tools.APP_ID_ENV: APP_ID,
    agent_tools.TENANT_ENV: TENANT, agent_tools.CALLER_OIDS_ENV: f"{FOUNDRY_OID}, {PROJECT_OID}",
})
check("mode is entra", agent_tools.auth_mode(), "entra")
asyncio.run(agent_tools.prewarm())
check("prewarm fetched the tenant's keys once", jwks_fetches, [TENANT])

print("entra mode: accepted callers")
calls.clear()
check("Foundry account MI, v1 token -> 200", post({"query": "q1"}, bearer=token()).status_code, 200)
check("project MI also allowed -> 200", post({"query": "q2"}, bearer=token(oid=PROJECT_OID)).status_code, 200)
check(
    "v2 issuer + client-ID audience -> 200",
    post({"query": "q3"}, bearer=token(iss=f"https://login.microsoftonline.com/{TENANT}/v2.0", aud=APP_ID)).status_code,
    200,
)
check("queries reached search_web", calls, ["q1", "q2", "q3"])
check("cached keys reused, no refetch", jwks_fetches, [TENANT])

print("entra mode: rejected before the body")
calls.clear()
rejects = {
    "no Authorization header -> 401": (post({"query": "x"}), 401),
    "a key is ignored in entra mode -> 401": (post({"query": "x"}, KEY), 401),
    "not a JWT -> 401": (post({"query": "x"}, bearer="not-a-token"), 401),
    "wrong audience -> 401": (post({"query": "x"}, bearer=token(aud="https://management.azure.com")), 401),
    "other tenant's issuer -> 401": (post({"query": "x"}, bearer=token(iss="https://sts.windows.net/other/")), 401),
    "expired -> 401": (post({"query": "x"}, bearer=token(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600)), 401),
    "missing exp -> 401": (post({"query": "x"}, bearer=token(exp=None)), 401),
    "signed by another key -> 401": (post({"query": "x"}, bearer=token(key=other_key)), 401),
    "HS256 -> 401": (post({"query": "x"}, bearer=token(key="s" * 32, alg="HS256")), 401),
    "no kid -> 401": (post({"query": "x"}, bearer=token(kid=None)), 401),
    "right audience, stranger's oid -> 403": (post({"query": "x"}, bearer=token(oid=STRANGER_OID)), 403),
    "right audience, no oid -> 403": (post({"query": "x"}, bearer=token(oid=None)), 403),
    "right issuer, other tid claim -> 403": (post({"query": "x"}, bearer=token(tid="other")), 403),
    "stranger + invalid body -> 403, not 422": (post({"nope": 1}, bearer=token(oid=STRANGER_OID)), 403),
}
for label, (resp, want) in rejects.items():
    check(label, resp.status_code, want)
check("no search made for rejected calls", calls, [])

print("entra mode: signing-key refresh")
jwks_fetches.clear()
check("unknown kid within the refresh floor -> 401, no fetch", post({"query": "x"}, bearer=token(kid="rotated")).status_code, 401)
check("no fetch inside the floor", jwks_fetches, [])
agent_tools._jwks.fetched_at -= agent_tools.JWKS_MIN_REFRESH_S + 1
post({"query": "x"}, bearer=token(kid="rotated"))
check("unknown kid after the floor refetches once", jwks_fetches, [TENANT])
jwks_fail = True
agent_tools._jwks.fetched_at -= agent_tools.JWKS_TTL_S + 1
check("failed refresh keeps the old keys -> 200", post({"query": "x"}, bearer=token()).status_code, 200)
agent_tools._jwks = None
check("no keys at all -> 503", post({"query": "x"}, bearer=token()).status_code, 503)
jwks_fail = False

print("key wins when both are configured")
os.environ[agent_tools.TOOL_KEY_ENV] = KEY
check("mode is key", agent_tools.auth_mode(), "key")
check("valid token without the key -> 401", post({"query": "x"}, bearer=token()).status_code, 401)
check("key -> 200", post({"query": "x"}, KEY).status_code, 200)

reset_env()
print(f"\n{checks - len(failures)}/{checks} checks passed")
sys.exit(1 if failures else 0)
