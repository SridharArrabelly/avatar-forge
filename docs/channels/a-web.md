# Channel A — Web (standalone)

The core product, and the foundation every other channel builds on. A voice-first
avatar in a browser, with an optional text composer.

**Deploy this first.** Every other channel assumes it is already working, and it
is the only channel that needs no administrator involvement at all.

---

## 1. What you get

A browser page where the user speaks, the avatar listens, and it answers aloud
with a lip-synced face — grounded in meeting minutes (AI Search) and curated news
(Bing Custom Search) through the Foundry agent.

## 2. What deploys

The default `azd up`, with **no flags set**:

| Resource | Purpose |
| --- | --- |
| Container App + Environment | Serves the FastAPI backend and the SPA |
| Container Registry | Image storage |
| Foundry account + project + agent | Answering, tool routing, grounding |
| Azure AI Search | The meeting-minutes corpus |
| Managed identity + role assignments | Keyless access to Foundry and Search |
| Log Analytics + Application Insights | Diagnostics |

Nothing Teams-related is provisioned. `botService.bicep` and
`communicationServices.bicep` are both skipped.

```bash
azd up
```

Configuration reference: [`../configuration.md`](../configuration.md).
Deployment mechanics: [`../deployment.md`](../deployment.md).

## 3. Manual / admin steps

| Step | Who |
| --- | --- |
| Azure subscription + Contributor on the resource group | You |
| Model quota in the target region | You / subscription owner |
| Populate the AI Search index with minutes | You |
| Create the Bing Custom Search configuration | You |

**No Entra admin. No Teams admin.** See
[`../admin-checklist.md`](../admin-checklist.md).

## 4. How to verify

```bash
# the app answers
curl https://<your-app>.azurecontainerapps.io/api/health
```

Then open the endpoint in a browser, allow the microphone, and ask a question
that requires grounding (for example, about a recent board meeting). You should
hear a spoken answer with a moving face.

If the avatar appears but never answers, check
[`../auth.md`](../auth.md) — the agent path requires Entra ID, not an API key.

## 5. Cost & teardown

The container app scales to a low floor but the Foundry and Search resources bill
continuously. To stop paying entirely:

```bash
azd down
```
