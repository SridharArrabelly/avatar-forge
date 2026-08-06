/*
 * Nuru meeting control panel (channel D, issue #27 — optional Companion surface).
 *
 * This is the DURABLE companion surface: a "meeting control panel", NOT a
 * decorative avatar face. It is the in-meeting front door to the proven ACS
 * audio participant (the in-call media spine):
 *   - shows whether Nuru is currently live in a call (polls /api/acs/status),
 *   - launches the existing ACS joiner (acs-join.html) in a SEPARATE window so
 *     the ACS Calling leg runs OUTSIDE the Teams meeting webview (avoids a second
 *     in-client audio leg / echo — per the design review),
 *   - surfaces the consent notice + wake-phrase usage + troubleshooting.
 *
 * Runs both inside Teams (meeting side panel / shared stage) and standalone in a
 * browser. TeamsJS is loaded best-effort and is a strict no-op when not hosted by
 * Teams, mirroring frontend/teams.js — so this page never hard-depends on Teams.
 */
const $ = (id) => document.getElementById(id);
const statusEl = $("status");
const bringBtn = $("bringBtn");
const refreshBtn = $("refreshBtn");
const linkEl = $("meetingLink");
const liveDot = $("liveDot");
const liveText = $("liveText");

const TEAMS_SDK_URL = "https://res.cdn.office.net/teams-js/2.31.1/js/MicrosoftTeams.min.js";

let joinerWindow = null;
// Avatar brand name, resolved from the server (/api/config -> assistantName, i.e.
// AVATAR_DISPLAY_NAME if set, else the active avatar model's friendly name) so
// it's never hardcoded; falls back to "Avatar" until the fetch resolves.
let avatarName = "Avatar";

async function loadAvatarName() {
    try {
        const res = await fetch("/api/config");
        if (res.ok) {
            const cfg = await res.json();
            avatarName = (cfg.defaults && cfg.defaults.assistantName) || "Avatar";
            refreshStatus();
        }
    } catch (_) { /* keep the fallback */ }
}

function log(msg) {
    console.log("[companion]", msg);
    statusEl.textContent = msg;
}

function setLive(state) {
    // state: "live" | "off" | "disabled" | "unknown"
    liveDot.className = "dot" + (state === "live" ? " live" : state === "disabled" ? " off" : "");
    liveText.textContent = {
        live: `${avatarName} is in the call`,
        off: `${avatarName} is not in the call yet`,
        disabled: "In-call media is not enabled on this deployment",
        unknown: "Checking…",
    }[state] || "Checking…";
    bringBtn.disabled = state === "disabled";
}

async function refreshStatus() {
    try {
        const res = await fetch("/api/acs/status");
        const s = await res.json();
        if (!s.enabled) { setLive("disabled"); return; }
        setLive(s.active ? "live" : "off");
    } catch (e) {
        setLive("unknown");
    }
}

function bringNuruIn() {
    const link = linkEl.value.trim();
    if (!link) { log("Paste the Teams meeting join link first."); return; }
    // Open the proven ACS joiner in its own window, prefilled with the link. The
    // joiner deliberately runs outside the Teams meeting webview.
    const url = "/acs-join.html?meeting=" + encodeURIComponent(link);
    joinerWindow = window.open(url, "nuru-join", "width=520,height=660");
    if (!joinerWindow) {
        log("Couldn't open the joiner window — allow pop-ups for this site, then try again.");
        return;
    }
    log(`Joiner opened in a separate window. Click 'Join meeting' there; admit ${avatarName} from the lobby if prompted. Keep that window open during the call.`);
    // Poll a little more eagerly for a short while so the panel reflects the join.
    let ticks = 0;
    const t = setInterval(() => { refreshStatus(); if (++ticks >= 20) clearInterval(t); }, 3000);
}

bringBtn.addEventListener("click", bringNuruIn);
refreshBtn.addEventListener("click", refreshStatus);

// ── Teams host integration (best-effort, no-op when standalone) ──────────────
function isInTeams() {
    try {
        const params = new URLSearchParams(window.location.search);
        if (params.has("inTeams")) return true;
        if (window.parent && window.parent !== window) return true;
        if (window.nativeInterface) return true;
    } catch (e) {}
    return false;
}

function initTeams() {
    try {
        const teams = window.microsoftTeams;
        if (!teams || !teams.app || typeof teams.app.initialize !== "function") return;
        teams.app.initialize().then(() => {
            // Keep the optional avatar-preview deep link inside Teams when hosted.
            try {
                teams.app.getContext().then((ctx) => {
                    const frame = ctx && ctx.page && ctx.page.frameContext;
                    if (frame) console.log("[companion] frameContext:", frame);
                }).catch(() => {});
            } catch (e) {}
        }).catch(() => {});
    } catch (e) {}
}

function loadTeamsSdk() {
    if (!isInTeams()) return;
    try {
        if (window.microsoftTeams) { initTeams(); return; }
        const s = document.createElement("script");
        s.src = TEAMS_SDK_URL;
        s.async = true;
        s.onload = initTeams;
        s.onerror = () => {};
        (document.head || document.documentElement).appendChild(s);
    } catch (e) {}
}

loadTeamsSdk();
loadAvatarName();
refreshStatus();
setInterval(refreshStatus, 10000);
