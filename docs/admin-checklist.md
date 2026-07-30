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
| Windows VM, NIC, NSG, public IP, DNS label | Bing Custom Search configuration content |

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

### C — Teams conversational bot

| Step | Who | If you cannot get it |
| --- | --- | --- |
| Register an Entra application + client secret | You *(if app registration is permitted)* | Ask an admin to create it and hand you the IDs |
| **Admin consent** for the bot's permissions | **Entra admin** | Hard blocker — one-time only |
| Set `BOT_APP_ID` / `BOT_APP_PASSWORD`, redeploy | You | — |
| Add the bot to the Teams app package | You | — |

### D — In-call avatar (Graph media bot)

This is the demanding one. **Verify the Teams app access policy is achievable
before you provision the VM** — the VM is the expensive part and it is useless
without the policy.

| # | Step | Who | Notes |
| --- | --- | --- | --- |
| 1 | Entra app registration + client secret | You / Entra admin | **Must be a SECOND app, separate from C.** One Entra app can back only one Azure Bot resource; reusing C's fails with `MsaAppId is already in use` |
| 2 | Graph **application** permissions: `Calls.JoinGroupCall.All`, `Calls.JoinGroupCallAsGuest.All`, **`Calls.AccessMedia.All`**, `OnlineMeetings.Read.All` | Entra admin to add | `Calls.AccessMedia.All` is what unlocks the room audio |
| 3 | **Admin consent** for all of the above | **Entra admin** | One-time. Nothing works without it |
| 4 | Azure Bot resource with **calling enabled**, calling webhook → the VM's public HTTPS FQDN | You (bicep + portal) | The webhook URL cannot be known until the VM has its DNS label |
| 5 | **Teams application access policy** — `New-CsApplicationAccessPolicy` then `Grant-CsApplicationAccessPolicy` | **Teams administrator** | **Hard blocker.** No workaround. Grant is per-user or tenant-wide |
| 6 | TLS certificate covering the VM FQDN, installed on the host | You | The media platform requires a real certificate; self-signed will not do |
| 7 | Open ports **9441** (control API) and **8445** (media/signalling) | You (NSG, in bicep) | — |
| 8 | Install/refresh the bot service on the VM | You (`meeting-bot/scripts/setup-host.ps1`) | Semi-automatable via VM extension |

> **The policy is permanent once granted.** It survives redeploys and VM
> rebuilds, so step 5 is a one-time cost — worth saying to the administrator you
> are asking, because "one irreversible-looking PowerShell command" lands better
> than "ongoing access".

### E — In-call avatar (headless browser)

Requirements are not yet established — see
[channels/e-in-call-headless.md](channels/e-in-call-headless.md). The
*hypothesis* under test is that it needs **no Graph permissions and no Teams
policy**, because it joins as an ordinary (guest) participant through a browser.
If that holds, it removes every hard blocker in channel D, which is the main
reason to evaluate it.

---

## If you are blocked

| Blocker | Best available fallback |
| --- | --- |
| No Teams admin (cannot get the access policy) | Stop at **A + B**. Evaluate **E**. Channel D is not available to you |
| No Entra admin (cannot consent) | Stop at **A + B**. C and D both require consent |
| Custom app upload disabled | **A** only, via a browser. Ask for org-wide publication of the package |
| No model quota in region | Deploy the core in a region that has quota; channels are region-independent of Teams |

---

## Handing this to an administrator

Ask for all of it **in one request** rather than discovering blockers serially —
that is the difference between one conversation and four. A complete ask is:

1. Consent to the Graph application permissions listed in D-2, for app `<app id>`
2. A Teams application access policy granting that app the right to join meetings
3. Custom app upload (or org-wide publication of the package)

State that items 1 and 2 are **one-time** and that the bot requires **no Teams
license** in the tenant.
