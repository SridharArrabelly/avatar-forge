"""The trusted sites every web tool may search: ``TRUSTED_WEB_SITES``.

One comma-separated list, set with ``azd env set TRUSTED_WEB_SITES ...`` for a
deployment or in ``.env`` for a local run. Each entry is a host or a URL,
optionally with a path; a leading ``+`` marks a source Bing should rank first
(SuperBoost)::

    +www.mtn.com/investors,www.itweb.co.za,mybroadband.co.za

``infra/main.bicep`` turns the same string into Bing Custom Search entries, which
keep the path and the boost. This module turns it into what Web IQ can use: its
scoping is ``site:`` operators on the query, and ``site:`` matches a host, never
a path or a rank. So each entry is reduced to its bare host, ``www.`` dropped
(``site:www.jse.co.za`` would miss ``senspdf.jse.co.za``, where the JSE's SENS
filings live), and duplicates removed.

Both parsers skip the same entries: blank ones, a lone ``+``, and ``-``-prefixed
ones, which have no Bing meaning. An empty list means Web IQ searches the open
web.

Standard library only, so ``scripts/preflight.py`` can import it too.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

ENV_VAR = "TRUSTED_WEB_SITES"
# The Web IQ-only override it replaced; values were already bare hosts.
LEGACY_ENV_VAR = "WEBIQ_ALLOWED_DOMAINS"


def entries(raw: str) -> list[tuple[str, bool]]:
    """``(site, superboost)`` for each entry main.bicep deploys, in order."""
    out: list[tuple[str, bool]] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or item == "+" or item.startswith("-"):
            continue
        boost = item.startswith("+")
        out.append((item[1:].strip() if boost else item, boost))
    return out


def host(site: str) -> str:
    """The bare host a ``site:`` operator can match, e.g. ``www.mtn.com/x`` -> ``mtn.com``."""
    name = urlsplit(site if "//" in site else f"//{site}").hostname or ""
    return name[4:] if name.startswith("www.") else name


def hosts(raw: str) -> list[str]:
    """Distinct bare hosts, first occurrence first."""
    seen: list[str] = []
    for site, _ in entries(raw):
        name = host(site)
        if name and name not in seen:
            seen.append(name)
    return seen


def configured_hosts(environ: Mapping[str, str] | None = None) -> list[str]:
    """The hosts to scope Web IQ to; empty means the open web.

    Falls back to the older ``WEBIQ_ALLOWED_DOMAINS`` so a ``.env`` written
    before the rename keeps its scope instead of silently searching everywhere.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_VAR, "").strip()
    if raw:
        return hosts(raw)
    return [d.strip() for d in environ.get(LEGACY_ENV_VAR, "").split(",") if d.strip()]


def scope_chars(names: list[str]) -> int:
    """Characters the ``site:`` clause takes from every Web IQ query's budget.

    Mirrors how ``build_query()`` in backend/voice/tools.py renders includes,
    plus the space that joins the clause to the question.
    """
    return len(f"({' OR '.join(f'site:{name}' for name in names)})") + 1 if names else 0
