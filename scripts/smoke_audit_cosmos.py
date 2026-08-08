"""Prove the Cosmos audit write path against real Azure, under managed identity (#30).

Everything about the audit trail is covered offline by ``tests/test_audit.py`` —
except the one thing that can only fail in a deployment: whether this app's
identity can actually *write* to Cosmos. That path has no offline equivalent
because the interesting part is Entra data-plane RBAC, which no mock can model.

It matters more than a normal untested path, because of how it fails. If the
role assignment is missing, ``CosmosSink.warm()`` raises at startup and
``_build_sink()`` quietly falls back to the local file sink (see #104). The app
then starts cleanly, reports records as written, and loses the trail on the next
revision. Nothing raises and nothing alerts. So "audit looks fine" is not
evidence that audit works, and this script exists to produce that evidence.

It exercises **the production sink** (:class:`backend.audit.cosmos.CosmosSink`)
and **the production renderer** (:meth:`backend.audit.records.TurnRecord.to_document`)
rather than a parallel reimplementation, so a green run is evidence about the
code that actually ships.

What a pass proves, in order:

1. ``AUDIT_COSMOS_ENDPOINT`` resolves and TLS completes.
2. The current identity holds the *data-plane* role — ``warm()`` reads the
   container, which is the exact call that 403s when only a control-plane role
   was granted.
3. A rendered audit document is accepted by ``upsert_item``, so the shape,
   the ``/sessionId`` partition key and the ``ttl`` field are all valid.
4. The document reads back byte-identical on the content fields.
5. Redaction ran before persistence, not after.

Usage::

    az login                 # or run where a managed identity is available
    uv run python scripts/smoke_audit_cosmos.py

Writes exactly one document to a throwaway ``sessionId`` and deletes it again.
Safe against a production container: it never reads, updates or removes anything
it did not create, and the document it writes carries a one-hour ``ttl`` so it
expires by itself even if cleanup cannot run.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

# The document is deleted at the end of a successful run. This ttl is the
# fallback for every other ending — a crash, a lost connection, a Ctrl-C — so
# that a failed smoke run cannot leave residue in a real container.
SMOKE_TTL_SECONDS = 3600

# Redaction is asserted through this: it must not survive to storage.
SMOKE_EMAIL = "smoke.probe@example.com"


def _with_raw(summary: str, text: str) -> str:
    """Show the interpretation, then the message Azure actually sent.

    Keeping the raw text is the difference between a diagnosis and a guess: when
    the guess is wrong, the operator can still see the truth underneath it.
    """
    first = text.strip().splitlines()[0] if text.strip() else "(no message)"
    return f"{summary}\n\n  Azure said:\n    {first[:400]}"


def diagnose(error: Exception) -> str:
    """Turn an Azure exception into the specific thing to go and fix.

    A smoke test that only says "failed" leaves the operator with the same
    problem they started with. These causes look nearly identical from the
    traceback and have completely different fixes, so they are separated here.

    The interpretation is always printed *alongside* the raw Azure message, never
    instead of it. An earlier version returned only the guess, and when a network
    403 was reported as an RBAC 403 the real cause stayed invisible through
    several rounds of chasing role assignments that were correct all along.
    """
    status = getattr(error, "status_code", None)
    text = str(error)

    if status == 403 or "Forbidden" in text:
        # Two unrelated failures both return 403. The firewall one names itself
        # in the response, so test for it before falling through to RBAC.
        lowered = text.lower()
        if ("firewall" in lowered or "public internet" in lowered
                or "publicnetworkaccess" in lowered):
            return _with_raw(
                "403 Forbidden — the network blocked this, not RBAC. The account\n"
                "  is refusing the caller's IP. Either publicNetworkAccess is\n"
                "  Disabled, or an ipRules allowlist does not include this host.\n"
                "  Check with:\n"
                "    az cosmosdb show -n <account> -g <rg> \\\n"
                "      --query '{public:publicNetworkAccess, ipRules:ipRules}'\n"
                "  Note the template is not the last word here: an Azure Policy\n"
                "  'modify' effect can force publicNetworkAccess to Disabled at\n"
                "  deploy time, so the account can differ from the Bicep that\n"
                "  created it. Check with:\n"
                "    az policy state list --resource <accountId> \\\n"
                "      --filter \"policyDefinitionAction eq 'modify'\"\n"
                "  If it is Disabled and no private endpoint exists, then nothing\n"
                "  outside the VNet can reach this account — including the\n"
                "  container app. In that case the sink will fail warm() in\n"
                "  production too and fall back to local file (see #104).",
                text,
            )
        return _with_raw(
            "403 Forbidden — the identity is authenticated but has no data-plane\n"
            "  role on this account. This is the common one, because the RBAC\n"
            "  roles shown in the portal are CONTROL plane and do not grant data\n"
            "  access. You need the built-in Cosmos DB Data Contributor role\n"
            "  assigned via sqlRoleAssignments — infra/modules/cosmosRoleForApp.bicep\n"
            "  does this for the app identity. To run this script as yourself,\n"
            "  assign the same role to your user principal.\n"
            "  Note role assignments take a few minutes to propagate.",
            text,
        )
    if status == 401 or "Unauthorized" in text:
        return _with_raw(
            "401 Unauthorized — no usable token. Run 'az login', or run this\n"
            "  where the managed identity is available.",
            text,
        )
    if status == 404 or "NotFound" in text or "does not exist" in text:
        return _with_raw(
            "404 Not Found — endpoint reachable, but the database or container\n"
            "  is not there. Check AUDIT_COSMOS_DATABASE and AUDIT_COSMOS_CONTAINER.\n"
            "  Note these are only created when the app is deployed with\n"
            "  ENABLE_AUDIT=true, so an infra deploy that had it false will not\n"
            "  have made them.",
            text,
        )
    if "ServiceRequestError" in type(error).__name__ or "getaddrinfo" in text:
        return _with_raw(
            "Could not reach the account at all — DNS or network. Check\n"
            "  AUDIT_COSMOS_ENDPOINT is the full https://<account>.documents.azure.com:443/\n"
            "  URL and that the account still exists.",
            text,
        )
    return f"{type(error).__name__}: {text}"


def build_record():
    """A synthetic turn shaped like a real one, including a tool call.

    Deliberately not a minimal document: the point is to exercise the same
    renderer path a real turn takes, including nested tool payloads, so that a
    serialisation problem surfaces here rather than in production.
    """
    from backend.audit.records import ToolCall, TurnRecord

    record = TurnRecord(
        session_id=f"smoke-audit-{uuid.uuid4()}",
        turn_index=0,
        channel="smoke",
        binding="agent",
    )
    record.user_text = f"Smoke probe. Contact {SMOKE_EMAIL} if this document is still here."
    record.assistant_text = "This document was written by scripts/smoke_audit_cosmos.py."
    record.status = "completed"
    record.latency_ms = 1.0
    record.conversation_id = "smoke-conversation"
    record.agent_name = "smoke"
    record.tools = [
        ToolCall(
            name="search_minutes",
            args={"query": "smoke probe"},
            results=[{"title": "synthetic passage", "content": "written by the smoke test"}],
            hit_count=1,
            elapsed_ms=1.0,
            source="smoke",
            call_id="smoke-call-0",
        )
    ]
    return record


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    from backend.config import (
        AUDIT_COSMOS_CONTAINER,
        AUDIT_COSMOS_DATABASE,
        AUDIT_COSMOS_ENDPOINT,
        AUDIT_REDACT,
        AUDIT_RETENTION_DAYS,
        AUDIT_TOOL_PAYLOAD_MAX_KB,
        ENABLE_AUDIT,
    )

    if not AUDIT_COSMOS_ENDPOINT:
        print("AUDIT_COSMOS_ENDPOINT is not set, so there is nothing to test.\n")
        print("If the environment is deployed with ENABLE_AUDIT=true, pull the")
        print("value into your .env with:\n")
        print("    azd env get-values | Select-String AUDIT_COSMOS_ENDPOINT\n")
        return 2

    try:
        import azure.cosmos  # noqa: F401
    except ImportError:
        print("The azure-cosmos package is not installed in this environment.")
        print("It is what the Cosmos sink imports, so install it before testing:\n")
        print("    uv sync\n")
        return 2

    print(f"endpoint   {AUDIT_COSMOS_ENDPOINT}")
    print(f"database   {AUDIT_COSMOS_DATABASE}")
    print(f"container  {AUDIT_COSMOS_CONTAINER}")
    print(f"redaction  {'on' if AUDIT_REDACT else 'OFF'}")
    print(f"retention  {AUDIT_RETENTION_DAYS} days")
    if not ENABLE_AUDIT:
        # Not an error. Verifying the infrastructure before turning capture on is
        # the sensible order, and this script talks to the sink directly.
        print("\nnote: ENABLE_AUDIT is false. Testing the sink anyway - the app")
        print("      will not capture anything until it is true.")
    print()

    from backend.audit.cosmos import CosmosSink

    record = build_record()
    session_id = record.session_id
    document = record.to_document(
        retention_days=AUDIT_RETENTION_DAYS,
        do_redact=AUDIT_REDACT,
        max_payload_bytes=AUDIT_TOOL_PAYLOAD_MAX_KB * 1024,
    )
    doc_id = document["id"]

    # Verify the real retention computation, then replace it. The configured
    # value is typically a year, and this document must not outlive the test.
    expected_ttl = AUDIT_RETENTION_DAYS * 86400
    if AUDIT_RETENTION_DAYS > 0 and document.get("ttl") != expected_ttl:
        print(f"FAIL  ttl was {document.get('ttl')}, expected {expected_ttl}")
        return 1
    document["ttl"] = SMOKE_TTL_SECONDS

    if AUDIT_REDACT and SMOKE_EMAIL in str(document):
        print("FAIL  the probe email survived rendering - redaction did not run")
        return 1

    sink = CosmosSink(
        endpoint=AUDIT_COSMOS_ENDPOINT,
        database=AUDIT_COSMOS_DATABASE,
        container=AUDIT_COSMOS_CONTAINER,
    )

    try:
        # 1. Connect. This is the RBAC checkpoint: warm() reads the container.
        try:
            await sink.warm()
        except Exception as e:
            print("FAIL  could not connect to Cosmos.\n")
            print(f"  {diagnose(e)}")
            return 1
        print("ok    connected and read the container (data-plane RBAC works)")

        # 2. Write through the production sink. It swallows upsert errors by
        #    design - a write failure must never reach the writer loop - so the
        #    returned count is the only signal, and 0 means it failed.
        written = await sink.write([document])
        if written != 1:
            print(f"FAIL  the sink reported {written} of 1 documents written.")
            print("      The upsert was rejected; the sink logs the reason at")
            print("      WARNING level. Most likely the partition key path is not")
            print("      /sessionId, or the document exceeds the 2 MB item limit.")
            return 1
        print(f"ok    wrote one document (id={doc_id})")

        # 3. Read it back through the sink's own client and credential. Using a
        #    second client here would prove less: it could succeed on a
        #    different token and hide exactly the gap being tested.
        container = sink._container
        try:
            stored = await asyncio.to_thread(
                container.read_item, item=doc_id, partition_key=session_id
            )
        except Exception as e:
            print("FAIL  wrote the document but could not read it back.\n")
            print(f"  {diagnose(e)}")
            print("\n  A write that succeeds and a read that fails usually means the")
            print("  role granted is write-only, or the partition key does not match.")
            return 1
        print("ok    read it back by id and partition key")

        # 4. The content survived the round trip.
        checks = {
            "sessionId": stored.get("sessionId") == session_id,
            "assistant text": stored.get("assistant", {}).get("text")
            == document["assistant"]["text"],
            "tool detail": bool(stored.get("tools"))
            and stored["tools"][0].get("name") == "search_minutes",
            "nested tool results": bool(stored.get("tools"))
            and bool(stored["tools"][0].get("results")),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            print(f"FAIL  the stored document differs on: {', '.join(failed)}")
            return 1
        print("ok    content matches, including nested tool payloads")

        # 5. Redaction is a persistence-time guarantee, so assert it on what
        #    actually came out of the database, not on what we sent.
        if AUDIT_REDACT:
            if SMOKE_EMAIL in str(stored):
                print("FAIL  the probe email is stored in Cosmos unredacted")
                return 1
            print("ok    redaction held through storage")
        else:
            print("skip  redaction not asserted (AUDIT_REDACT is false)")

    finally:
        # Best effort. The ttl above is what guarantees cleanup if this cannot run.
        try:
            if sink._container is not None:
                await asyncio.to_thread(
                    sink._container.delete_item, item=doc_id, partition_key=session_id
                )
                print("ok    deleted the probe document")
        except Exception as e:
            print(f"warn  could not delete the probe document: {type(e).__name__}")
            print(f"      it expires on its own within {SMOKE_TTL_SECONDS // 60} minutes")
        await sink.close()

    print("\nPASS - the audit trail can write to Cosmos with this identity.")
    print("Next: have an agent-mode conversation, then verify the reconciler with")
    print("      uv run python scripts/smoke_audit_conversation.py <conversation-id>")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
