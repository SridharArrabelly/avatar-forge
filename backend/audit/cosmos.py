"""Azure Cosmos DB for NoSQL sink — the production audit store.

Chosen over MongoDB and Cosmos for MongoDB vCore on one decisive point:
**Entra RBAC on the data plane**. This repo stores zero database passwords
today — Foundry, AI Search and ACS all authenticate with managed identity.
Introducing a connection string here would make the most sensitive data in the
system the only thing guarded by a stored credential.

The other two properties that matter:

* ``sessionId`` is the partition key, so "replay this conversation" is a
  single-partition query — the cheapest read Cosmos does.
* Retention is a per-item ``ttl`` field, so expiry needs no cleanup job.

Failure is contained: a write error is logged and counted, never raised into
the writer loop. Losing audit records is bad; stalling a conversation is worse.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class CosmosSink:
    """Batch writer for audit documents."""

    def __init__(self, endpoint: str, database: str, container: str):
        self.endpoint = endpoint
        self.database_name = database
        self.container_name = container
        self._client = None
        self._container = None
        self._credential = None

    async def warm(self) -> None:
        """Create the client and acquire a token at startup.

        Called from the lifespan precisely so that no conversation ever pays
        for TLS setup or the first managed-identity token acquisition, which
        together can cost seconds.
        """
        await asyncio.to_thread(self._connect)

    def _connect(self) -> None:
        try:
            from azure.cosmos import CosmosClient
        except ImportError as e:
            # Optional extra, so this is a configuration mistake rather than a
            # broken install: say which command fixes it and which settings
            # avoid needing it at all.
            raise RuntimeError(
                "AUDIT_SINK=cosmos needs the azure-cosmos package, which is an "
                "optional dependency. Install it with `uv sync --extra cosmos` "
                "(the container image already includes it), or choose "
                "AUDIT_SINK=file|none, or set ENABLE_AUDIT=false."
            ) from e

        from azure.identity import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._client = CosmosClient(self.endpoint, credential=self._credential)
        database = self._client.get_database_client(self.database_name)
        self._container = database.get_container_client(self.container_name)
        # Force a round trip so token acquisition and TLS happen now, not on
        # the first real write.
        self._container.read()
        logger.info(
            f"[AUDIT] Cosmos ready — {self.database_name}/{self.container_name}"
        )

    def _write_batch(self, documents: list[dict]) -> int:
        written = 0
        for doc in documents:
            try:
                # no_response stops Cosmos echoing the whole document back;
                # audit docs carry full transcripts, so the saved bandwidth and
                # JSON parsing is worth having.
                self._container.upsert_item(doc, no_response=True)
                written += 1
            except Exception as e:
                logger.warning(f"[AUDIT] Cosmos upsert failed for {doc.get('id')}: {e}")
        return written

    async def write(self, documents: list[dict]) -> int:
        if not documents:
            return 0
        if self._container is None:
            try:
                await asyncio.to_thread(self._connect)
            except Exception as e:
                logger.warning(f"[AUDIT] Cosmos unavailable: {e}")
                return 0
        return await asyncio.to_thread(self._write_batch, documents)

    async def close(self) -> None:
        for resource in (self._client, self._credential):
            close_fn = getattr(resource, "close", None)
            if callable(close_fn):
                try:
                    await asyncio.to_thread(close_fn)
                except Exception:
                    pass
        self._client = None
        self._container = None
        self._credential = None
