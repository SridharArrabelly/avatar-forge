"""FastAPI application entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from .api import routes, websocket as ws
from .acs import build_acs_router
from .bot.app import build_bot_router, shutdown_bot
from .config import DEVELOPER_MODE, HOST, PORT, configure_logging
from .voice.auth import close_credential, create_credential
from .voice.catalog import close_search_client, prewarm_catalog

configure_logging()
logger = logging.getLogger(__name__)


async def _prewarm_credential() -> None:
    """Acquire tokens at startup so the first user doesn't pay the cold-cost.

    DefaultAzureCredential's first token acquisition can take 1-6 seconds
    (resolves AzureCliCredential / managed identity / env-based chains, then
    shells out to ``az account get-access-token`` for the first scope).

    **Tokens are cached per-scope**, so we must warm every distinct Azure
    resource the backend actually talks to:
    - ``ai.azure.com`` — Voice Live SDK (primary path, every session)
    - ``search.azure.com`` — Azure AI Search (catalogue + agent tool calls)

    Scopes warmed in parallel. The Cognitive Services scope is NOT included
    because the backend has no direct AOAI / Cognitive Services callers
    (the Foundry agent and Voice Live SDK both use ``ai.azure.com``).
    Removing the unused scope shaves one ``az`` invocation (~1.3s) off
    startup.
    """
    scopes = (
        "https://ai.azure.com/.default",
        "https://search.azure.com/.default",
    )
    try:
        credential = create_credential("")
        await asyncio.gather(*(credential.get_token(s) for s in scopes))
        logger.info(f"Credential pre-warmed at startup (scopes: {', '.join(scopes)})")
    except Exception as e:
        # Don't fail startup if pre-warm fails — the per-session path will
        # surface a real error later.
        logger.warning(f"Credential pre-warm failed (will retry on first session): {e}")


async def _prewarm_startup() -> None:
    """Sequenced startup pre-warm: credential first, THEN catalogue.

    Sequencing matters — if these run in parallel, the catalogue's AI Search
    call asks the SDK for a ``search.azure.com`` token at the same moment
    ``_prewarm_credential`` is still acquiring it. The catalogue's request
    misses the cache and spawns ITS OWN ``az account get-access-token``
    call, duplicating ~1.3s of credential work and causing the catalogue
    fetch to be measured as ~7s instead of ~3-4s.

    Sequenced: catalogue fetch starts with a hot token cache; it only pays
    the AI Search round-trip cost.
    """
    await _prewarm_credential()
    await prewarm_catalog()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hook: pre-warms credentials, closes outstanding sessions on shutdown."""
    logger.info("Avatar Forge server starting...")
    # Fire-and-forget sequenced pre-warm so startup is not blocked but the
    # catalogue fetch benefits from a hot token cache.
    asyncio.create_task(_prewarm_startup())
    yield
    # Order matters: stop session handlers first (they may still use the
    # credential to refresh tokens during teardown), THEN close the
    # SearchClient (which uses the credential), THEN close the
    # credential's underlying aiohttp.ClientSession.
    await ws.shutdown_all()
    await shutdown_bot()
    await close_search_client()
    await close_credential()
    logger.info("Avatar Forge server stopped.")


app = FastAPI(
    title="Avatar Forge",
    description="Python backend for Azure Voice Live with Avatar support",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Allow the app to be framed by the Microsoft Teams clients (web + desktop) so it
# can run as a personal tab (issue #28). We intentionally set ONLY frame-ancestors
# — a full CSP (script-src/connect-src/media-src) would break inline JS, the WSS
# voice/avatar socket, and WebRTC. No X-Frame-Options is sent (it cannot express a
# multi-origin allow-list and would conflict with this directive).
_TEAMS_FRAME_ANCESTORS = (
    "frame-ancestors 'self' "
    "https://teams.microsoft.com https://*.teams.microsoft.com "
    "https://teams.live.com https://*.teams.live.com "
    "https://*.skype.com"
)


@app.middleware("http")
async def teams_frame_ancestors(request, call_next):
    """Permit embedding in the Teams clients while leaving everything else intact."""
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _TEAMS_FRAME_ANCESTORS
    return response


@app.middleware("http")
async def cache_static(request, call_next):
    """Cache policy for static assets.

    In developer mode we fully disable caching so edits show up on reload.

    In production we send `Cache-Control: no-cache`, which still forces a
    revalidation on every request (so a deploy is never served stale) but lets
    StaticFiles answer with a 304 from its ETag/Last-Modified instead of
    re-sending the payload. The previous unconditional `no-store` meant every
    page load re-downloaded index.html + app.js + style.css in full (~150KB)
    even when nothing had changed.
    """
    response = await call_next(request)
    path = request.url.path
    if path.endswith((".js", ".css", ".html")) or path == "/":
        if DEVELOPER_MODE:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        else:
            response.headers["Cache-Control"] = "no-cache"
    return response


# Compress text responses. Registered LAST so it is the outermost HTTP middleware
# and therefore compresses the final body after the header middlewares above have
# run. The frontend ships ~200KB of uncompressed text per cold load (app.js 116KB,
# style.css 40KB, index.html 44KB) and nothing was compressing it, so every first
# visit paid full size on the critical path. WebSocket traffic is untouched —
# GZipMiddleware only handles scope type "http", so the voice/avatar sockets and
# the raw PCM frames on them are unaffected. minimum_size skips bodies too small
# for compression to pay for itself.
#
# Formats that are already compressed are skipped by extension: re-compressing
# them burns CPU and makes them marginally LARGER (measured: /brand/color.png went
# 44,895 -> 44,928 bytes). Starlette's GZipMiddleware has no content-type filter,
# and the content type isn't known until the response starts, so the path is the
# practical place to decide. API routes have no extension and stay compressed.
_INCOMPRESSIBLE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
    ".woff", ".woff2", ".zip", ".gz", ".mp4", ".webm", ".mp3", ".ogg",
)


class SelectiveGZipMiddleware(GZipMiddleware):
    """GZipMiddleware that leaves already-compressed media alone."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").lower().endswith(
            _INCOMPRESSIBLE_SUFFIXES
        ):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# compresslevel 6, not Starlette's default of 9. Measured over this frontend's
# own assets, level 9 costs 2.5x the CPU of level 6 to save 241 bytes (0.1%) —
# a bad trade on a 1-vCPU container, and worse than it looks because compression
# runs on the same event loop that has to accept the voice WebSocket while the
# page is still loading.
app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024, compresslevel=6)


app.include_router(routes.router)
app.include_router(ws.router)
# Teams bot messaging endpoint (issue #53). Mounted before the static SPA so
# POST /api/messages is handled by the bot, not the catch-all frontend mount.
app.include_router(build_bot_router())
# Teams in-call media participant (channel D, issue #27). Additive + opt-in: every
# ACS endpoint returns 503 when ACS is not configured, so this never changes a
# non-ACS deploy. Mounted before the static SPA so /api/acs/* + /ws/acs/* resolve.
app.include_router(build_acs_router())

# Canonical brand assets (logo/icons) live in assets/brand and are the single
# source of truth shared by the web app, the Teams package, and the meeting bot.
# Serve them at /brand/* so the web favicon and the Azure Bot iconUrl can both
# reference one URL (e.g. /brand/color.png) without copying the file around.
# Mounted before the catch-all SPA so /brand/* resolves here.
_brand = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "brand")
if os.path.isdir(_brand):
    app.mount("/brand", StaticFiles(directory=_brand), name="brand")

# Mount frontend
_frontend = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(_frontend):
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
else:
    @app.get("/")
    async def root():
        """Fallback when frontend/ is missing."""
        return {"message": "Avatar Forge — frontend/ directory not found."}


def run() -> None:
    """Console-script entry point (see pyproject [project.scripts])."""
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=True, log_level=os.getenv("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    run()
