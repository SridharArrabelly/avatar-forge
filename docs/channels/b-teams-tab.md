# Channel B — Teams personal tab

The same web avatar, embedded as a personal tab inside Microsoft Teams.

**This is the best-value step in the product.** It adds a Teams surface for
**zero extra Azure resources** — the manifest simply points a `staticTab` at the
container app URL channel A already serves.

Requires: [channel A](a-web.md) deployed.

---

## 1. What you get

The avatar available inside Teams, using the Teams client's own microphone and
speaker. Same voice, same grounding, same agent — no second deployment.

The Teams JS SDK is only loaded when running inside Teams, so the standalone web
app is completely unaffected.

## 2. What deploys

**No Azure resources.** Only a Teams app package:

```bash
python teams/build_package.py
```

This fills the placeholders in `teams/manifest.template.json` (schema v1.17) with
your deployed URL and IDs, and produces an installable `.zip`.

If `AVATAR_DISPLAY_NAME` is set, it names the tab and bot — the single branding
knob. See [`../configuration.md`](../configuration.md).

## 3. Manual / admin steps

| Step | Who | If blocked |
| --- | --- | --- |
| Build the package | You | — |
| **Custom app upload enabled** in the tenant | Teams admin *(often already on)* | Low-sensitivity setting; usually granted |
| Sideload the package to yourself | You | If sideloading is off, a Teams admin must publish it org-wide |

**Usually achievable without an administrator**, which is why this is the
recommended stopping point when admin access is limited. Full detail:
[`../admin-checklist.md`](../admin-checklist.md).

## 4. How to verify

1. In Teams → **Apps → Manage your apps → Upload a custom app** → select the zip
2. Open the app; the avatar stage should load
3. Ask a grounded question and confirm you hear the answer

The on-stage text composer is **always hidden inside Teams** by design (the tab
is voice-first). That is expected behaviour, not a fault — see `ENABLE_TEXT_INPUT`
in [`../configuration.md`](../configuration.md).

## 5. Cost & teardown

**No incremental cost** — it reuses channel A's deployment entirely.

To remove: uninstall the app in Teams. Nothing in Azure changes.
