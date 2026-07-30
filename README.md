# Avatar Forge

A talking, photorealistic AI avatar that answers questions from **your own
documents** — over the web and inside Microsoft Teams. The avatar speaks and listens
in real time (Azure **Voice Live**) and is grounded by a **Microsoft Foundry agent**
with Azure AI Search RAG over your corpus plus Grounding with Bing Custom Search for
live web facts. The Voice Live SDK runs **entirely server-side** (Python/FastAPI); the
browser only handles audio I/O and avatar video.

## Architecture

```
┌─────────────────────────┐         ┌─────────────────────────────┐         ┌──────────────────┐         ┌──────────────────────────────┐
│    Browser (Frontend)   │◄──WS───►│   Python Server (FastAPI)   │◄──SDK──►│ Azure Voice Live │◄───────►│    Foundry Agent Service     │
│                         │         │                             │         │     Service      │ agent_  │     (your Foundry agent)     │
│  • Audio capture (mic)  │         │  • Session management       │         └──────────────────┘ config  │  • Instructions (variant)    │
│  • Audio playback       │         │  • Voice Live SDK calls     │                                       │  • gpt-5.4 deployment        │
│  • Avatar video         │◄─WebRTC (peer-to-peer video)──────────────────────────────┘                  │  • Azure AI Search tool      │
│  • Settings / chat UI   │         │  • Event relay              │                                      │  • Grounding w/ Bing Custom  │
│  • Teams tab + bot       │         │  • Meeting catalogue inject │                                      └──────────────────────────────┘
└─────────────────────────┘         └─────────────────────────────┘
```

The Python backend bridges the browser and Azure Voice Live, binding each session to
an existing Foundry agent via `agent_config = { agent_name, project_name }`. The agent
owns the system prompt, model, and tools so RAG + grounding resolve server-side. Full
detail in **[docs/architecture.md](docs/architecture.md)**.

## Channel support

The avatar is **one brain with several front doors** — all channels share the same
backend, Voice Live session, Foundry agent and grounding. What differs is only how
audio and video get in and out, which drives cost and, more importantly, **how much
administrator access you need**.

| | Channel | Status | Extra Azure infra | Admin burden | Doc |
|---|---|---|---|---|---|
| **A** | **Web** (standalone) | ✅ Shipped | — *(the core)* | **None** | [a-web.md](docs/channels/a-web.md) |
| **B** | **Teams — personal tab** | ✅ Shipped | **None** | Upload a Teams app package | [b-teams-tab.md](docs/channels/b-teams-tab.md) |
| **C** | **Teams — conversational bot** | ✅ Shipped *(optional)* | Azure Bot + Teams channel | Entra app + **admin consent** | [c-teams-chat-bot.md](docs/channels/c-teams-chat-bot.md) |
| **D** | **Teams — in-call avatar** (Graph media bot) | ✅ Working | Azure Bot + **Windows VM** + DNS + TLS | **Highest** — incl. **Teams app access policy** | [d-in-call-media-bot.md](docs/channels/d-in-call-media-bot.md) |
| **E** | **Teams — in-call avatar** (headless browser) | 🔜 Placeholder | Container/job | TBD | [e-in-call-headless.md](docs/channels/e-in-call-headless.md) |

**A → B → C is a ladder, not a menu** — each step is additive on the one before, and
**B adds a Teams surface for zero extra Azure resources**. **D and E are rivals**: two
implementations of the *same* capability (live in-call presence), to be compared and
narrowed to one.

👉 **Start here: [docs/channels/README.md](docs/channels/README.md)** for the decision
guide, and **[docs/admin-checklist.md](docs/admin-checklist.md)** for every manual step
and who must perform it. If you have no Teams administrator, read that first — it will
tell you in one page which channels are available to you.

All Teams surfaces are **additive** — the standalone web app is unaffected, and the
Teams JS SDK is never loaded outside Teams.

## Quickstart (local)

You need Python 3.10+, [`uv`](https://docs.astral.sh/uv/), and a Foundry resource in a
Voice Live region (see [docs/development.md](docs/development.md) for prerequisites).

```bash
# 1. Install uv (macOS/Linux; see docs for Windows)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Configure — copy the template and fill in the required values
cp .env.example .env        # edit AZURE_VOICELIVE_ENDPOINT, AGENT_*, PROJECT_ENDPOINT

# 3. Authenticate (the agent path requires Entra ID — no API key)
az login

# 4. Run
uv run avatar-forge         # → http://localhost:3000
```

Full walkthrough — building the search index, smoke tests, developer mode — in
**[docs/development.md](docs/development.md)**.

## Documentation

| Doc | What's in it |
|---|---|
| **[docs/channels/README.md](docs/channels/README.md)** | **Start here.** The channel ladder, comparison, and decision guide — which front door to deploy and why. |
| **[docs/admin-checklist.md](docs/admin-checklist.md)** | **Every manual step automation cannot do**, per channel, with who must perform it and what to do when you're blocked. |
| **[docs/development.md](docs/development.md)** | Run locally, build the AI Search index, smoke-test the index and agent, dev-only knobs. |
| **[docs/configuration.md](docs/configuration.md)** | **Every** environment variable, grouped by concern — the single source of truth. |
| **[docs/architecture.md](docs/architecture.md)** | System design, tool-calling accuracy, meeting-catalogue injection, frontend UX, project structure. |
| **[docs/deployment.md](docs/deployment.md)** | Deploy to Azure with `azd`: topology, region preflight, BYO Foundry/Search, cross-RG RBAC, post-deploy. |
| **[docs/auth.md](docs/auth.md)** | `DefaultAzureCredential`, required roles, startup pre-warm, IMDS skip, token caching. |
| **[teams/README.md](teams/README.md)** | Building and sideloading the Teams app package (serves channels B and C). |
| **[docs/channels/d-design-media-bot.md](docs/channels/d-design-media-bot.md)** | **Design record** — the three in-call options evaluated, why Python + a thin .NET/Windows media bot, and the final architecture. |
| **[docs/channels/d-design-avatar-video.md](docs/channels/d-design-avatar-video.md)** | **Design record** — the avatar's synced video face as a meeting camera tile, and why audio + video share one synthesis. |
| **[docs/testing-meetings.md](docs/testing-meetings.md)** | **How to test the two in-meeting paths** — browser joiner vs. media bot: what each can and cannot hear, runbooks, healthy logs, rollback. |
| **[meeting-bot/README.md](meeting-bot/README.md)** | The .NET/Windows media bot itself: project layout, configuration, operator runbook, and the traps that cost real debugging time. |
| **[prompts/README.md](prompts/README.md)** | Agent prompt content, the reasoning/non-reasoning variants, and the edit workflow. |

## References & Acknowledgements

Avatar Forge was built by referencing the following Microsoft samples and documentation. Thanks to the teams behind them.

- **Azure AI VoiceLive samples** — the project started from and the real-time avatar/voice implementation is based on these official samples: [microsoft-foundry/voicelive-samples (Python)](https://github.com/microsoft-foundry/voicelive-samples/tree/main/python) ([`azure-ai-voicelive` SDK](https://pypi.org/project/azure-ai-voicelive/)).
- **Azure AI Search** — retrieval/grounding index: [Azure AI Search documentation](https://learn.microsoft.com/en-us/azure/search/).
- **Azure AI Foundry (Agent Service)** — agent orchestration and tool-calling: [Azure AI Foundry documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/).
- **Grounding with Bing Custom Search** — domain-scoped web grounding for the agent: [Bing Custom Search tool](https://learn.microsoft.com/en-us/azure/foundry-classic/agents/how-to/tools-classic/bing-custom-search).
- **Foundry web search (Grounding with Bing Search) tool** — real-time web grounding: [Grounding with Bing Search tools](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/bing-tools).

## License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file
for the full text.

Copyright (c) 2026 Sridhar Arrabelly
