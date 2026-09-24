"""Offline check: one TRUSTED_WEB_SITES list, read the same way by Bing and Web IQ.

Needs **no Azure resources and no credentials**. Runs in well under a second.

The list is parsed twice: ``infra/main.bicep`` turns it into Bing Custom Search
entries, and ``backend/trusted_sites.py`` turns it into the hosts Web IQ scopes
its ``site:`` operators to. If those two drift, the two tools quietly search
different sites for the same deployment. So this pins both parsers against the
same inputs. Bicep is evaluated from the *generated* ``infra/main.json``, the
template ARM actually runs, not re-implemented here.

Also pinned:

* the format: ``+`` for SuperBoost, ``https://`` added when there is no scheme,
  whitespace trimmed, blank / lone ``+`` / ``-`` entries skipped;
* an empty list is Bing ``[]`` and Web IQ ``[]``, the open web;
* hosts drop ``www.`` and paths and keep first-seen order, as the Bicep this
  replaced did;
* the legacy ``WEBIQ_ALLOWED_DOMAINS`` still scopes a run that predates the rename;
* ``scope_chars()`` is exactly what ``build_query()`` spends of Web IQ's budget;
* preflight never blocks on an unset list, and says what that means per tool.

    uv run python tests/test_trusted_web_sites.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_webiq_binding import evaluate  # noqa: E402

from backend import trusted_sites  # noqa: E402

TEMPLATE = json.loads((ROOT / "infra" / "main.json").read_text(encoding="utf-8"))
VARIABLES = {
    key: value[1:-1] if isinstance(value, str) and value.startswith("[") else value
    for key, value in TEMPLATE["variables"].items()
}

# Every shape the MTN list uses: paths, a fragment, www. and bare hosts, a
# subdomain that must survive, and two entries on one host.
SAMPLE = (
    "+www.mtn.com/investors, +www.mtn.com/financial-results/#,"
    "www.itweb.co.za,mybroadband.co.za,+www.jse.co.za/market-data,"
    "africa.businessinsider.com,https://www.reuters.com/world/africa"
)


def bicep(raw: str) -> list[dict]:
    return evaluate("variables('bingAllowedDomains')", {"trustedWebSites": raw}, VARIABLES)


def legacy_hosts(bing: list[dict]) -> list[str]:
    """What main.bicep used to derive for Web IQ from its bingAllowedDomains array."""
    seen: list[str] = []
    for entry in bing:
        name = entry["domain"].removeprefix("https://").removeprefix("http://").split("/")[0]
        name = name.removeprefix("www.")
        if name not in seen:
            seen.append(name)
    return seen


class BicepParsing(unittest.TestCase):
    def test_format(self):
        self.assertEqual(
            bicep(" +www.mtn.com/investors , itweb.co.za,http://example.org/x "),
            [
                {"domain": "https://www.mtn.com/investors", "includeSubPages": True, "boostLevel": "SuperBoost"},
                {"domain": "https://itweb.co.za", "includeSubPages": True, "boostLevel": "Boosted"},
                {"domain": "http://example.org/x", "includeSubPages": True, "boostLevel": "Boosted"},
            ],
        )

    def test_skipped_entries(self):
        self.assertEqual([e["domain"] for e in bicep(",, + ,-www.spam.example,a.com,")], ["https://a.com"])

    def test_empty_is_no_bing(self):
        for raw in ("", " ", ",", "+", "-x.com"):
            with self.subTest(raw=raw):
                self.assertEqual(bicep(raw), [])

    def test_path_and_fragment_kept_for_bing(self):
        self.assertEqual(
            bicep("+www.mtn.com/financial-results/#")[0]["domain"],
            "https://www.mtn.com/financial-results/#",
        )


class BackendParsing(unittest.TestCase):
    def test_hosts(self):
        self.assertEqual(
            trusted_sites.hosts(SAMPLE),
            ["mtn.com", "itweb.co.za", "mybroadband.co.za", "jse.co.za",
             "africa.businessinsider.com", "reuters.com"],
        )

    def test_empty_is_open_web(self):
        for raw in ("", " ", ",", "+", "-x.com"):
            with self.subTest(raw=raw):
                self.assertEqual(trusted_sites.hosts(raw), [])

    def test_host_forms(self):
        for site, host in (
            ("www.mtn.com/investors", "mtn.com"),
            ("https://www.mtn.com/", "mtn.com"),
            ("MTN.com", "mtn.com"),
            ("senspdf.jse.co.za", "senspdf.jse.co.za"),
            ("www.mtn.com:443/x", "mtn.com"),
            ("wwwx.example.com", "wwwx.example.com"),
        ):
            with self.subTest(site=site):
                self.assertEqual(trusted_sites.host(site), host)


class Parity(unittest.TestCase):
    """Bicep and the backend must agree on every input, or the tools diverge."""

    CASES = (
        SAMPLE,
        "",
        " a.com ,, +b.org/x ,-c.net, + ,www.a.com/y",
        "+https://www.example.com/a,example.com/b,sub.example.com",
    )

    def test_same_entries(self):
        for raw in self.CASES:
            with self.subTest(raw=raw):
                bing = bicep(raw)
                entries = trusted_sites.entries(raw)
                self.assertEqual(len(bing), len(entries))
                self.assertEqual(
                    [e["boostLevel"] == "SuperBoost" for e in bing], [boost for _, boost in entries]
                )

    def test_web_iq_hosts_match_the_old_bicep_derivation(self):
        for raw in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(trusted_sites.hosts(raw), legacy_hosts(bicep(raw)))


class Configured(unittest.TestCase):
    def test_new_name_wins(self):
        env = {"TRUSTED_WEB_SITES": "+www.mtn.com/investors", "WEBIQ_ALLOWED_DOMAINS": "old.example"}
        self.assertEqual(trusted_sites.configured_hosts(env), ["mtn.com"])

    def test_legacy_name_still_scopes(self):
        self.assertEqual(
            trusted_sites.configured_hosts({"WEBIQ_ALLOWED_DOMAINS": " mtn.com, itweb.co.za ,"}),
            ["mtn.com", "itweb.co.za"],
        )
        self.assertEqual(
            trusted_sites.configured_hosts({"TRUSTED_WEB_SITES": " ", "WEBIQ_ALLOWED_DOMAINS": "a.com"}),
            ["a.com"],
        )

    def test_unset_is_open_web(self):
        self.assertEqual(trusted_sites.configured_hosts({}), [])

    def test_tools_reads_it_per_call(self):
        from backend.voice import tools

        with patch.dict(os.environ, {"TRUSTED_WEB_SITES": "www.itweb.co.za/news"}, clear=False):
            os.environ.pop("WEBIQ_ALLOWED_DOMAINS", None)
            self.assertEqual(tools._allowed_domains(), ["itweb.co.za"])
        with patch.dict(os.environ, {"TRUSTED_WEB_SITES": ""}, clear=False):
            os.environ.pop("WEBIQ_ALLOWED_DOMAINS", None)
            self.assertEqual(tools._allowed_domains(), [])


class Budget(unittest.TestCase):
    def test_scope_chars_is_what_build_query_spends(self):
        from backend.voice import tools

        question = "MTN results"
        for raw in (SAMPLE, "a.com", "a.com,b.org,c.net"):
            with self.subTest(raw=raw):
                names = trusted_sites.hosts(raw)
                built = tools.build_query(question, names)
                self.assertEqual(len(built) - len(question), trusted_sites.scope_chars(names))

    def test_open_web_costs_nothing(self):
        self.assertEqual(trusted_sites.scope_chars([]), 0)


class Preflight(unittest.TestCase):
    """Warn, never block, on an unset list; and say what it means per tool."""

    @classmethod
    def setUpClass(cls):
        import preflight

        cls.check = staticmethod(preflight.check_trusted_web_sites)

    def states(self, cfg: dict) -> list[tuple[str, str]]:
        out = []
        for r in self.check(cfg):
            out.append(("ok" if r.ok else "warn" if r.warn_only else "fail", r.detail))
        return out

    def test_unset_never_fails(self):
        for cfg in (
            {},
            {"SERVICE_APP_URI": "https://x", "BING_CUSTOM_CONFIG_NAME": "c"},
            {"VOICE_BINDING": "model"},
            {"VOICE_BINDING": "model", "SERVICE_APP_URI": "https://x"},
            {"AGENT_WEB_TOOL": "webiq", "SERVICE_APP_URI": "https://x"},
        ):
            with self.subTest(cfg=cfg):
                self.assertNotIn("fail", [state for state, _ in self.states(cfg)])

    def test_bing_without_sites_says_no_web_tool(self):
        [(state, detail)] = self.states({})
        self.assertEqual(state, "warn")
        self.assertIn("NO web tool", detail)

    def test_bing_turned_off_is_silent(self):
        self.assertEqual(self.states({"DEPLOY_BING_GROUNDING": "false"}), [])

    def test_web_iq_unset_is_open_web(self):
        [(state, detail)] = self.states({"VOICE_BINDING": "model"})
        self.assertEqual(state, "ok")
        self.assertIn("open web", detail)

    def test_deployed_web_iq_warns_until_chosen(self):
        deployed = {"VOICE_BINDING": "model", "SERVICE_APP_URI": "https://x"}
        self.assertEqual(self.states(deployed)[0][0], "warn")
        self.assertEqual(self.states({**deployed, "TRUSTED_WEB_SITES": ""})[0][0], "ok")

    def test_legacy_name_is_flagged(self):
        states = self.states({"VOICE_BINDING": "model", "WEBIQ_ALLOWED_DOMAINS": "a.com"})
        self.assertEqual(states[0][0], "warn")
        self.assertIn("no longer read", states[0][1])

    def test_scope_over_the_cap_fails(self):
        raw = ",".join(f"site{i:03d}.example.com" for i in range(40))
        self.assertIn("fail", [state for state, _ in self.states({"VOICE_BINDING": "model", "TRUSTED_WEB_SITES": raw})])
        # Bing has no such cap, so the same list is fine for it.
        self.assertNotIn("fail", [state for state, _ in self.states({"TRUSTED_WEB_SITES": raw})])

    def test_summary(self):
        [(state, detail)] = self.states({"VOICE_BINDING": "model", "TRUSTED_WEB_SITES": SAMPLE})
        self.assertEqual(state, "ok")
        self.assertIn("7 site(s)", detail)
        self.assertIn("6 host(s)", detail)


if __name__ == "__main__":
    unittest.main()
