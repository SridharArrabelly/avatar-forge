# Conversation audit trail

Persists, for every turn of every conversation, **what the user asked**, **what
the tools returned**, and **what the model answered** — across all four channels
and both voice bindings.

Off by default. When `ENABLE_AUDIT=false` the entire feature costs one
`is None` check per turn and no infrastructure is deployed.

> [!IMPORTANT]
> This records the substance of conversations, including anything a user says
> to the avatar and every passage retrieved on their behalf. Turning it on is a
> **policy decision, not just a config change** — confirm your retention period,
> access controls, and any notice or consent obligation before enabling it in an
> environment real people use.

---

## Why this is not just "log the messages"

Two properties fight each other, and the design exists to satisfy both.

**1. It must be complete.** An audit trail with holes is worse than none,
because it invites conclusions it cannot support. The hard part is agent
binding: when Voice Live is bound to a Foundry agent, the agent runs the tool
call *server-side* and relays **no tool events at all** to us. We see the
question and the answer, and nothing about the retrieval in between.

**2. It must not slow the avatar down.** This is a latency-sensitive
application — the number that matters is time-to-first-audio, and the whole
repo is organised around not spending it. An audit path that adds even tens of
milliseconds to the turn path is not worth having.

The resolution to (1) is the Foundry **conversation items** API, which returns
the complete turn after the fact. The resolution to (2) is that *nothing*
about audit runs on the turn path: capture is a queue push, and every
expensive operation happens in a background writer.

---

## What each binding can prove

This is the table to bring to a compliance review. "Fidelity" means: is this
the real value, or a reconstruction?

| Field | model binding — source | agent binding — source | fidelity |
|---|---|---|---|
| User text | input-audio transcription completed | same | **exact**, both |
| Assistant text | response audio-transcript / text done | same | **exact**, both |
| Turn outcome (completed / cancelled / failed) | `response.done` status | same | **exact**, both |
| Barge-in / truncation | interrupt path | same | **exact**, both |
| Tool **name** | [`backend/voice/functions.py`](../backend/voice/functions.py) | `remote_function_call.name` | **exact**, both |
| Tool **arguments** (the query) | in-process call args | `remote_function_call.arguments` | **exact**, both |
| Tool **results** (retrieved passages) | in-process return value | `remote_function_call_output.output.documents[]` | **exact**, both |
| Citations / sources | derived from results | document `id` + `content` per output doc | **exact**, both |
| Per-tool latency | measured in-process | not exposed by the API | exact / **unavailable** |
| Session & identity metadata | app | app | exact, both |

**The one real asymmetry:** per-tool latency. In model binding the tool runs in
our process, so we time it. In agent binding it runs inside Foundry and the
conversation item carries no timing, so `elapsedMs` is `null`. Everything else
is captured at full fidelity in both bindings.

Each captured tool call records **how** we learned it, in `tools[].source`:

| `source` | Meaning |
|---|---|
| `in-process` | Observed directly as we executed it (model binding). Includes timing. |
| `foundry-conversation-item` | Recovered from the Foundry conversation after the turn (agent binding). |

### Agent binding: how the gap is closed

When Voice Live creates a response it emits `response.created`, and in agent
binding that event carries a **`conversation_id`**. That single field is the
whole mechanism:

```text
response.created ──► conversation_id captured on the turn record
                          │
        (turn completes, audio already delivered to the user)
                          │
        background writer ├──► conversations.items.list(conversation_id)
                          │        ├─ message              (user)
                          │        ├─ remote_function_call (name + args + call_id)
                          │        ├─ remote_function_call_output (call_id + documents)
                          │        └─ message              (assistant)
                          └──► calls joined to outputs by call_id ──► tools[]
```

> [!WARNING]
> There is **no list operation** for Foundry conversations. If `conversation_id`
> is not captured live from `response.created`, that turn's tool detail is
> unrecoverable — permanently. This is why capture happens on the event itself
> rather than being derived later.

Until reconciliation completes, the record carries `meta.toolsPending: true`.
The reconciler clears it. Incompleteness is therefore always **explicit in the
data** rather than silently indistinguishable from "no tools were used".

Set `AUDIT_RECONCILE_AGENT_TOOLS=false` to skip reconciliation entirely; records
are still written, with tool detail absent and `toolsPending` left `true`.

---

## Latency design

The guarantee is that the turn path does no audit work beyond appending to an
in-memory object and one non-blocking queue push.

| Rule | Why |
|---|---|
| **Capture only on `*_DONE` events** | Those events carry the complete text, so nothing accumulates per delta. Roughly four capture points per turn, not thousands. |
| **`put_nowait`, never `await put`** | An `await put` on a full queue would block the event loop carrying audio. A full queue drops the record and increments a counter instead. Dropping audit data is acceptable; stalling a conversation is not. |
| **All serialisation, redaction and truncation in the writer** | Done under `asyncio.to_thread`, after the turn is over — never inline. |
| **Cosmos client and token warmed at startup** | TLS setup plus first managed-identity token can cost seconds. The lifespan pays it, so no conversation does. |
| **The Foundry fetch is never inline** | It runs in the background writer, after the answer has been delivered. Conversations outlive the session, so there is no deadline. |
| **Capture code can never raise** | Every entry point is individually wrapped. An audit bug must not be able to break a conversation. |

`tests/test_audit.py` pins these properties — including that a full queue drops
rather than blocks, that a broken sink is contained, and that no capture entry
point propagates an exception.

### Measured cost

[`scripts/bench_audit_latency.py`](../scripts/bench_audit_latency.py) measures
what capture charges the turn, in three arms that separate the switch, the
capture work, and a sink actually draining. Median per turn, 5,000 turns per arm:

| Arm | Config | Per turn | vs baseline |
|---|---|---|---|
| Off | `ENABLE_AUDIT=false` | 0.30 µs | baseline |
| Capture | `AUDIT_SINK=none` | 5.80 µs | **+5.5 µs** |
| Capture + sink | `AUDIT_SINK=file` | 5.50 µs | **+5.2 µs** |

Two things follow. The disabled path is the `is None` check claimed above and
nothing more, at 0.3 µs. And a **draining sink adds nothing measurable to the
turn** — the file arm lands within noise of the sink-less one, which is the
queue design doing its job rather than a suspiciously good number.

It also settles whether to A/B this on a deployment: **no.** Separating a 5 µs
difference from turn latency whose standard deviation is even 5 ms would need
roughly 15 million turns per arm, and real jitter is worse than that. A live run
would be measuring the network. What a live run *should* prove is correctness
rather than latency — that Cosmos accepts writes under managed identity, and that
the agent-mode reconciler recovers tool I/O, which is what
[`smoke_audit_conversation.py`](../scripts/smoke_audit_conversation.py) is for.

Caveat worth keeping in view: this bounds the cost *on the turn*. It says nothing
about a Cosmos sink that is slow or unreachable, where the queue fills and records
are dropped. That is a data-loss risk, not a latency one, and `stats()` counts it.

---

## Record shape

One document per **turn** — the natural audit unit.

```jsonc
{
  "id": "<sessionId>:<turnIndex>",   // upsert key
  "sessionId": "...",                // partition key
  "turnIndex": 3,
  "channel": "web | acs-browser | meeting-bot",
  "binding": "agent | model",
  "startedAt": "...", "endedAt": "...", "latencyMs": 3412,

  "user":      { "text": "...", "itemId": "...", "at": "..." },
  "tools":     [ { "name": "search_minutes", "args": { "query": "..." },
                   "results": [ ... ], "hitCount": 5,
                   "elapsedMs": 340, "error": null,
                   "source": "in-process" } ],
  "assistant": { "text": "...", "status": "completed", "truncated": false },

  "identity":  { "userId": null, "displayName": null, "tenantId": null },
  "meta":      { "agentName": "...", "model": "...", "appVersion": "...",
                 "conversationId": "...", "responseId": "...",
                 "toolsPending": false, "operationId": null },
  "ttl": 31536000
}
```

Notes:

- **One record is one exchange.** In model binding a tool call splits the
  exchange across two Voice Live responses — the function call ends the first,
  the spoken answer arrives in the second. Filing those separately would put the
  question and the answer in different records with the tools attached to
  neither, so a tool-call-only response is carried forward and merged into the
  response that answers it.
- `ttl` is a Cosmos-native per-item field, so retention expires itself with no
  cleanup job. Derived from `AUDIT_RETENTION_DAYS`; `0` or less means keep
  forever.
- `id` is deterministic, so writes are idempotent upserts — a reconciliation
  updates the same document rather than creating a second one.
- `meta.operationId` is the correlation handle. App Insights records the
  OpenTelemetry trace id as `operation_Id`, so once telemetry ships this is what
  lets a slow turn spotted in App Insights be joined to that turn's full content
  here — keeping content out of App Insights without losing the ability to move
  between the two. **It is `null` today**, because OpenTelemetry is deliberately
  not yet a dependency; the field exists now so that arrival is an additive
  change rather than a revision of a schema already holding real records.
  Capture is best-effort by design: it happens *after* the record is attached, so
  a tracer that fails costs this one field and never the question, tools or
  answer.

---

## Storage

**Azure Cosmos DB for NoSQL**, serverless, provisioned only when
`ENABLE_AUDIT=true`.

Chosen over MongoDB (and Cosmos for MongoDB vCore) on one decisive point:
**Entra RBAC on the data plane**. This repo stores zero database passwords —
Foundry, AI Search and ACS all authenticate with managed identity. Introducing a
connection string would have made the most sensitive data in the system the only
thing guarded by a stored credential. The deployed account sets
`disableLocalAuth: true`, so key-based access is not merely unused, it is
refused.

| Choice | Reason |
|---|---|
| Serverless | Audit traffic is spiky and low-volume; pay per request, no idle floor. |
| Partition key `/sessionId` | "Replay this conversation" becomes a single-partition query — the cheapest read Cosmos does. |
| Indexing excludes `/*` | Transcripts and retrieved passages are never filtered on. Indexing them would multiply write cost and storage for no query benefit. |
| Per-item `ttl` | Retention with no cleanup job. |

The app's user-assigned managed identity is granted the built-in **Cosmos DB
Built-in Data Contributor** data-plane role by
[`infra/modules/cosmosRoleForApp.bicep`](../infra/modules/cosmosRoleForApp.bicep).
Note this is a *data-plane* role assignment (`sqlRoleAssignments`) — the
control-plane RBAC roles in the portal do **not** grant data access, which is a
common and confusing failure.

### Querying it

Replay one conversation, in order:

```sql
SELECT * FROM c WHERE c.sessionId = @sessionId ORDER BY c.turnIndex
```

Find turns whose agent-mode tool detail never reconciled:

```sql
SELECT c.sessionId, c.turnIndex FROM c WHERE c.meta.toolsPending = true
```

Both are answerable from the Data Explorer in the portal.

---

## Configuration

Full descriptions in [configuration.md](configuration.md#conversation-audit-trail).

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_AUDIT` | `false` | Master switch. Also a Bicep parameter — deploys Cosmos when `true`. |
| `AUDIT_SINK` | `cosmos` | `cosmos`, `file` (JSONL, local dev), or `none` (accept and discard). |
| `AUDIT_COSMOS_ENDPOINT` | — | Set automatically by infra when audit is enabled. |
| `AUDIT_RETENTION_DAYS` | `365` | Per-item TTL. `0` or less keeps records forever. |
| `AUDIT_REDACT` | `true` | Mask obvious secret/PII patterns before persisting. |
| `AUDIT_QUEUE_MAX` | `1000` | Bounded so a sink outage costs capped memory. |
| `AUDIT_TOOL_PAYLOAD_MAX_KB` | `32` | Cap per tool payload; retrieved passages are by far the largest field. |
| `AUDIT_RECONCILE_AGENT_TOOLS` | `true` | Recover agent-mode tool I/O from Foundry after the turn. |

### Try it locally, with no Azure resources

```powershell
$env:ENABLE_AUDIT = "true"
$env:AUDIT_SINK   = "file"
uv run python -m backend.main
```

Records are appended as JSONL to `audit-log.jsonl` in the working directory.
Have a conversation, then read the file — one JSON object per turn, in the
shape above.

### Enable it on a deployment

```powershell
azd env set ENABLE_AUDIT true
azd env set AUDIT_RETENTION_DAYS 365   # optional
azd up
```

`azd` provisions the Cosmos account, database and container, grants the app's
identity the data-plane role, and injects `AUDIT_COSMOS_ENDPOINT` into the
container app. Nothing is created while `ENABLE_AUDIT` is `false`.

---

## Redaction

When `AUDIT_REDACT=true` (default), captured text is scanned for obvious
secrets and high-risk identifiers before it is written — bearer tokens, JWTs,
connection-string secrets, and long digit runs that look like payment card
numbers. Redaction runs recursively over tool arguments and results, not just
the transcript.

Treat this as **defence in depth, not a compliance control**. It catches
credentials that leak into a transcript by accident; it cannot classify
free-form speech. The substantive control is retention plus access to the
Cosmos account.

---

## Failure behaviour

Designed so that no audit failure can affect a conversation.

| Failure | Behaviour |
|---|---|
| Cosmos unreachable at startup | Logged as an error; **falls back to the file sink** so the trail continues locally. The app starts normally. |
| Sink fails on write | Logged and counted; the writer continues with the next batch. |
| Queue full | Record dropped, drop counter incremented, warning throttled. Never blocks. |
| Foundry reconciliation fails or times out | Record is written without tool detail and `toolsPending` stays `true`. |
| `AUDIT_SINK=cosmos` but no endpoint configured | Warns and uses the file sink. |
| `azure-cosmos` not installed | Falls back to the file sink with a warning. |
| Shutdown | The queue is drained and in-flight batches are flushed before the process exits. |

---

## Verifying it end to end

Everything except the agent-mode recovery path is covered offline by
[`tests/test_audit.py`](../tests/test_audit.py). That one exception needs a live
conversation, so it has its own script:

```powershell
uv run python scripts/smoke_audit_conversation.py <conversation-id>
```

It replays a real Foundry conversation through the production reconciler and
prints the tool, the exact query and the passages the agent retrieved
server-side. Take the conversation id from the app log — with audit enabled,
every agent-mode turn logs the id it captured.

> [!TIP]
> If it reports no tool calls, check the turn actually triggered retrieval. A
> vague question makes the agent ask for clarification rather than search, which
> looks identical to a capture failure but is not one. Ask about a specific,
> named meeting.

---

## Related

- [architecture.md](architecture.md) — where audit sits in the system.
- [voice-binding.md](voice-binding.md) — the agent/model choice this doc's
  fidelity table depends on.
- [auth.md](auth.md) — the managed-identity model the Cosmos RBAC follows.
- [configuration.md](configuration.md) — every variable, authoritative.
