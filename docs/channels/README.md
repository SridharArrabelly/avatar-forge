# Channels — how the avatar reaches people

The avatar is **one brain with several front doors**. Everything below shares the
same core: the FastAPI backend, the Azure Voice Live session, the Foundry agent,
and its grounding (AI Search over meeting minutes + Bing Custom Search for news).
No channel re-implements answering.

What differs between channels is only **how audio and video get in and out**, and
that is what drives cost and — far more importantly — **how much administrator
access you need**.

> **Read the admin burden column first.** It is the single best predictor of
> whether a deployment succeeds. See [`../admin-checklist.md`](../admin-checklist.md)
> for the exact manual steps, who must perform each one, and what to do when you
> cannot get them.

---

## The channels

| | Channel | Status | Extra Azure infra | Admin burden | Doc |
| --- | --- | --- | --- | --- | --- |
| **A** | **Web** (standalone) | Shipped | — *(this is the core)* | **None** beyond an Azure subscription | [a-web.md](a-web.md) |
| **B** | **Teams personal tab** | Shipped | **None** | Upload/sideload a Teams app package | [b-teams-tab.md](b-teams-tab.md) |
| **C** | **Teams conversational bot** | Shipped *(optional)* | Azure Bot + Teams channel | Entra app + **admin consent** | [c-teams-chat-bot.md](c-teams-chat-bot.md) |
| **D** | **In-call avatar** — Graph media bot | **Working** | Azure Bot (calling) + **Windows VM** + DNS + TLS | **Highest**: Graph app permissions, admin consent, **Teams app access policy** | [d-in-call-media-bot.md](d-in-call-media-bot.md) |
| **E** | **In-call avatar** — headless browser | Placeholder | Container/job | TBD (expected lower than D) | [e-in-call-headless.md](e-in-call-headless.md) |

### Two things this table is deliberately saying

**A → B → C is a ladder, not a menu.** Each step is additive on the one before.
B is the best-value step in the whole product: it adds a Teams surface for **zero
extra Azure resources** — the manifest simply points a `staticTab` at the ACA URL
the web app already serves.

**D and E are rivals, not siblings.** They are two implementations of the *same*
capability (the avatar present in a live meeting). You are expected to run the
comparison in [e-in-call-headless.md](e-in-call-headless.md#comparison) and keep
one. They are numbered separately only so each can be documented and deployed
independently while the comparison is open.

---

## Pick your path

| If you want to… | Deploy | Why |
| --- | --- | --- |
| Demo the avatar to anyone with a browser | **A** | No Teams, no admin, no manifest |
| Put it in front of Teams users with least friction | **A + B** | Zero extra infra; sideload if you lack admin rights |
| Let people `@mention` it in a Teams chat | **A + B + C** | Needs an Entra app + one-time admin consent |
| Have it **join a meeting, hear the room, and answer aloud** | **A + D** | The only path with true in-call presence |
| Evaluate a cheaper in-call option | **A + E** | Then run the comparison and drop the loser |

**Cannot get Teams admin?** Stop at **A + B** (sideloading a personal tab is
usually permitted when uploading custom apps is enabled). **D is blocked without
a Teams administrator** — it requires a Teams app access policy that only they
can create. Confirm that before investing in the VM.

---

## What actually deploys

Everything is **additive and conditional**, following the pattern already in
`infra/resources.bicep`: a deploy that does not opt in behaves exactly as it did
before the feature existed.

| Flag | Default | Effect |
| --- | --- | --- |
| *(none)* | — | Channel **A**. Container app, Foundry agent, AI Search, ACR, identity, roles. |
| `BOT_APP_ID` set | empty | Provisions `modules/botService.bicep` (Azure Bot + Teams channel). Needed by **C**, and **also by D** — see below. |
| `MEETING_BOT_ENABLED` | `false` | Serves the media-bot bridge (`/ws/acs/audio`) for **D**. |
| `ENABLE_ACS` | `false` | Provisions `modules/communicationServices.bicep`. Independent of the above. |

> **The Azure Bot resource is shared between C and D.** A Graph calling bot
> *requires* an Azure Bot registration, so "the chat bot feature" and "the bot
> registration resource" are not the same thing. You can deploy **D without
> enabling C's chat behaviour**, but you cannot deploy D without the registration.

Full variable reference: [`../configuration.md`](../configuration.md).
Deployment mechanics: [`../deployment.md`](../deployment.md).

---

## Every channel page has the same five sections

So you can compare them without re-reading prose:

1. **What you get** — the user-visible capability
2. **What deploys** — resources and flags
3. **Manual / admin steps** — what automation *cannot* do, and who must do it
4. **How to verify** — the exact commands that prove it works
5. **Cost & teardown** — what it costs to leave running, and how to stop paying

---

## Design records (the "why")

These are decision documents, kept separate from the operational pages above
because they answer a different question — why the architecture is what it is,
including options that were rejected and what they would have cost.

- [d-design-media-bot.md](d-design-media-bot.md) — why a .NET/Windows media bot
  bridged to a Python brain, and the three options evaluated
- [d-design-avatar-video.md](d-design-avatar-video.md) — why the avatar's audio
  and video must come from one synthesis, and how the video reaches the meeting
