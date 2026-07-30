# Channel C — Teams conversational bot

An installable, `@mentionable` bot that answers in Teams chat via the same
Foundry agent, and deep-links into the tab.

**Status: shipped, and optional.** It is deliberately *not* on the critical path.
Deploy it if you want a text surface or a fallback for users who cannot use
in-call media; skip it otherwise.

Requires: [channel A](a-web.md) deployed. Pairs naturally with
[channel B](b-teams-tab.md), since both ship in one Teams app package.

---

## 1. What you get

Text answers (and adaptive cards) in Teams chat, hosted in-process by the same
FastAPI app at `POST /api/messages` via the Microsoft 365 Agents SDK.

**Explicitly not in scope:** live in-call media. The bot does not join meetings
or hear audio — that boundary is [channel D](d-in-call-media-bot.md)'s job.

## 2. What deploys

| Resource | Gated on |
| --- | --- |
| Azure Bot + Teams channel (`modules/botService.bicep`) | `BOT_APP_ID` being non-empty |

```bash
azd env set BOT_APP_ID <app-id>
azd env set BOT_APP_PASSWORD <secret>
azd up
```

If `BOT_APP_ID` / `TEAMS_BOT_ID` is unset, the bot infra is skipped entirely and
the deployment behaves exactly as channel A.

> **The Azure Bot resource is shared with [channel D](d-in-call-media-bot.md).**
> A Graph calling bot requires a bot registration too. So "the chat feature" and
> "the bot registration" are separable: D needs the resource, but not this
> channel's chat behaviour.

## 3. Manual / admin steps

| Step | Who | If blocked |
| --- | --- | --- |
| Entra app registration + client secret | You, if app registration is permitted | Ask an admin to create it and hand you the IDs |
| **Admin consent** for the bot's permissions | **Entra admin** | Hard blocker — but one-time |
| Add the bot entry to the Teams app package | You | — |

See [`../admin-checklist.md`](../admin-checklist.md).

## 4. How to verify

```bash
# the messaging endpoint is mounted
curl -i https://<your-app>.azurecontainerapps.io/api/messages
```

Then in Teams, `@mention` the bot in a chat and ask a grounded question. You
should get a text answer from the same agent the voice channels use — if the web
app answers but the bot does not, the problem is the bot registration or consent,
not the agent.

## 5. Cost & teardown

The Azure Bot resource itself is inexpensive; the cost is essentially channel A's.

To remove: unset `BOT_APP_ID` and redeploy — the module is skipped and the bot
resource is removed.
