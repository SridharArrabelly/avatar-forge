# Admin checklist — what automation cannot do

**Read this before deploying anything beyond the web app.**

> Commands are PowerShell on Windows — see [`channels/README.md`](channels/README.md)
> for the platform note.

Most failed deployments of this project do not fail on Azure resources. They fail
because someone reaches step 4 of 6, discovers they need a Teams administrator,
and stops. This page exists so you discover that on day zero instead.

Bicep and `azd` provision Azure *resources*. They cannot grant Entra permissions,
cannot consent on an administrator's behalf, and cannot touch Teams tenant policy.
Those are deliberate security boundaries, not gaps in our automation.

---

## What is automated vs. what is not

| Automated (`azd up` / bicep) | **Manual — needs a human with rights** |
| --- | --- |
| Container app, registry, Log Analytics, App Insights | Entra **app registration** + client secret |
| Foundry account/project, model deployment, agent | **Microsoft Graph application permissions** |
| AI Search service, index, connections | **Admin consent** for those permissions |
| Managed identity + RBAC role assignments | **Teams app access policy** (Teams admin only) |
| Azure Bot resource + Teams channel | Teams app package **upload / approval** |
| ACS resource (when `ENABLE_ACS=true`) | TLS certificate for the media endpoint |
| Windows VM, NIC, NSG, public IP, DNS label | *(choosing which sites go in the Bing allow-list — a decision, not a portal step)* |
| Bing account + site allow-list + Foundry connection (unless `DEPLOY_BING_GROUNDING=false`) | |

---

## Per-channel requirements

### A — Web

| Step | Who | Blocked? |
| --- | --- | --- |
| Azure subscription with Contributor on a resource group | You | Nothing else needed |
| Model quota in the chosen region | You / subscription owner | Request quota, or pick another region |

**No Entra admin, no Teams admin.** If you can `azd up`, you can run channel A.

### B — Teams personal tab

| Step | Who | If you cannot get it |
| --- | --- | --- |
| Build the Teams app package (`teams/build_package.py`) | You | — |
| **Custom app upload enabled** in the tenant | Teams admin *(often already on)* | Ask; this is a low-sensitivity setting |
| Sideload the package to yourself | You | If sideloading is off, a Teams admin must publish it org-wide |

**Usually achievable without an administrator.** This is why B is the recommended
stopping point when admin access is limited.

### C — In-call avatar (Graph media bot)

This is the demanding one. **Verify the Teams app access policy is achievable
before you provision the VM** — the VM is the expensive part and it is useless
without the policy.

| # | Step | Who | Notes |
| --- | --- | --- | --- |
| 1 | Entra app registration + client secret | You / Entra admin | **Must be its own app**, not shared with any other Azure Bot resource. One Entra app can back only one bot; reusing an existing one fails with `MsaAppId is already in use` |
| 2 | Graph **application** permissions: `Calls.JoinGroupCall.All`, `Calls.JoinGroupCallAsGuest.All`, **`Calls.AccessMedia.All`** | Entra admin to add | `Calls.AccessMedia.All` is what unlocks the room audio. `OnlineMeetings.Read.All` is needed **only** if you want to join by short `/meet/<id>` link — see step 5 |
| 3 | **Admin consent** for all of the above | **Entra admin** | One-time. Nothing works without it |
| 4 | Azure Bot resource with **calling enabled**, calling webhook → the VM's public HTTPS FQDN | You (bicep + portal) | The webhook URL cannot be known until the VM has its DNS label |
| 5 | **Teams application access policy** — `New-CsApplicationAccessPolicy` then `Grant-CsApplicationAccessPolicy` | **Teams administrator** | **Only needed to join by short `/meet/<id>` link.** Not required for a classic `/l/meetup-join/` link — see below |
| 6 | TLS certificate covering the VM FQDN, installed on the host | You | The media platform requires a real certificate; self-signed will not do |
| 7 | Open ports **9441** (control API) and **8445** (media/signalling) | You (NSG, in bicep) | — |
| 8 | Install/refresh the bot service on the VM | You (`meeting-bot/scripts/setup-host.ps1`) | Semi-automatable via VM extension |

> **⚠ Step 5 is narrower than it looks — it is not a hard blocker.** An earlier
> version of this page called it one, with "no workaround". That was wrong, and
> it matters because it is the only step here needing a *Teams* administrator,
> which is the rights level people are least likely to have.
>
> The policy governs the **`OnlineMeetings.*`** Graph permissions, not
> `Calls.JoinGroupCall.All` or `Calls.AccessMedia.All`. The bot only touches
> `OnlineMeetings` when it has to **resolve a short `/meet/<id>` link** into
> meeting coordinates, because that link carries no thread id. A classic
> `/l/meetup-join/...` link already contains them, so `JoinInfo.ParseJoinURL`
> parses it locally and no Graph call happens at all — see
> `MeetingBot.cs`, where `ResolveShortLinkAsync` sits behind a short-link-only
> branch and its own error message offers the classic link as the way out.
>
> **So with a classic join URL, channel C needs Entra admin consent only.**
> Grab the classic link from the meeting invite body rather than the Share
> button. Do step 5 if you want short links to work; skip it otherwise.
>
> *Verified against Microsoft's own scoping of the policy and against this
> repo's code, but not yet exercised in a tenant that lacks the policy — treat
> it as very likely, not proven.*

> **The policy is permanent once granted.** It survives redeploys and VM
> rebuilds, so step 5 is a one-time cost — worth saying to the administrator you
> are asking, because "one irreversible-looking PowerShell command" lands better
> than "ongoing access".

### D — In-call avatar (headless browser)

Requirements are not yet established — see
[channels/d-in-call-headless.md](channels/d-in-call-headless.md). The
*hypothesis* under test is that it needs **no Graph permissions and no Teams
policy**, because it joins as an ordinary (guest) participant through a browser.
If that holds, it removes every hard blocker in channel C, which is the main
reason to evaluate it.

---

## If you are blocked

| Blocker | Best available fallback |
| --- | --- |
| No Teams admin (cannot get the access policy) | Stop at **A + B**. Evaluate **D**. Channel C is not available to you |
| No Entra admin (cannot consent) | Stop at **A + B**. C requires consent; D's requirement is not yet established (see above) |
| Custom app upload disabled | **A** only, via a browser. Ask for org-wide publication of the package |
| No model quota in region | Deploy the core in a region that has quota; channels are region-independent of Teams |

---

## Handing this to an administrator

Ask for all of it **in one request** rather than discovering blockers serially —
that is the difference between one conversation and four. A complete ask is:

1. Consent to the Graph application permissions listed in C-2, for app `<app id>`
2. A Teams application access policy granting that app the right to join meetings
3. Custom app upload (or org-wide publication of the package)

State that items 1 and 2 are **one-time** and that the bot requires **no Teams
license** in the tenant.
