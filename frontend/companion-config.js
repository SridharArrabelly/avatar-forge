/*
 * Configuration page for the Companion configurable tab (channel D, issue #27).
 *
 * Teams opens this page when a user adds the meeting tab. It sets the tab's
 * content URL (the control panel) and marks the config valid so Save enables.
 * It only does anything inside the Teams client; standalone it is inert.
 */
const TEAMS_SDK_URL = "https://res.cdn.office.net/teams-js/2.31.1/js/MicrosoftTeams.min.js";

function configure() {
    const teams = window.microsoftTeams;
    if (!teams || !teams.app || !teams.pages || !teams.pages.config) return;

    teams.app.initialize().then(async () => {
        const origin = window.location.origin;
        // Tab name comes from the server's resolved persona name
        // (assistantName: AVATAR_DISPLAY_NAME, else the active avatar model's
        // friendly name) — never hardcode the avatar name.
        let displayName = "Avatar";
        try {
            const cfg = await (await fetch("/api/config")).json();
            displayName = (cfg.defaults && cfg.defaults.assistantName) || "Avatar";
        } catch (_) { /* keep fallback */ }
        teams.pages.config.registerOnSaveHandler((saveEvent) => {
            teams.pages.config.setConfig({
                entityId: "avatarMeetingControl",
                contentUrl: `${origin}/companion.html?inTeams=1`,
                websiteUrl: `${origin}/companion.html`,
                suggestedDisplayName: displayName,
            }).then(() => saveEvent.notifySuccess())
              .catch(() => saveEvent.notifyFailure("Failed to set tab config"));
        });
        teams.pages.config.setValidityState(true);
    }).catch(() => {});
}

function loadSdk() {
    if (window.microsoftTeams) { configure(); return; }
    const s = document.createElement("script");
    s.src = TEAMS_SDK_URL;
    s.async = true;
    s.onload = configure;
    s.onerror = () => {};
    (document.head || document.documentElement).appendChild(s);
}

loadSdk();
