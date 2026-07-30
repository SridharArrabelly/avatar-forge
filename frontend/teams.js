// Teams personal-tab integration (issue #28).
//
// This file is a strict NO-OP unless the page is actually running inside the
// Microsoft Teams client. When standalone (the port-3000 app, top-level window),
// it returns immediately and the Teams JS SDK is never even fetched — so the
// standalone experience is behaviorally unchanged.
//
// When in Teams it: loads a PINNED Teams JS SDK build, initializes the SDK, and
// mirrors the Teams host theme into the app's existing applyTheme() hook. Every
// step is wrapped so a failure can never break the avatar experience.
(function () {
  'use strict';

  // Pinned SDK build — do not float to a bare 2.x tag (avoids surprise breakage).
  var TEAMS_SDK_URL = 'https://res.cdn.office.net/teams-js/2.31.1/js/MicrosoftTeams.min.js';

  // In Teams the manifest contentUrl carries ?inTeams=1. The framed fallback
  // (parent !== window) is defensive in case the param is ever lost on an
  // internal navigation; it only matters when the page is embedded.
  function isInTeams() {
    try {
      var params = new URLSearchParams(window.location.search);
      if (params.has('inTeams')) return true;
      if (window.parent && window.parent !== window) return true;
      if (window.nativeInterface) return true; // Teams desktop webview marker
    } catch (e) {}
    return false;
  }

  if (!isInTeams()) return; // standalone: do nothing, load nothing.

  function mapTeamsTheme(theme) {
    // applyTheme accepts 'light' | 'dark' | 'system'.
    if (theme === 'dark' || theme === 'contrast') return 'dark';
    return 'light'; // 'default' and anything unexpected -> light
  }

  function applyTeamsTheme(theme) {
    try {
      if (typeof window.applyTheme === 'function') {
        window.applyTheme(mapTeamsTheme(theme));
      }
    } catch (e) {}
  }

  function initTeams() {
    try {
      var teams = window.microsoftTeams;
      if (!teams || !teams.app || typeof teams.app.initialize !== 'function') return;

      teams.app.initialize().then(function () {
        // Tell the Teams host the content loaded successfully. Without this the
        // host keeps its loading veil over the tab iframe, so the page renders
        // fine underneath but the user only sees a blank/black frame. This is
        // the required handshake for a custom tab — call it as soon as init
        // resolves (the avatar app is already interactive by now).
        try {
          if (typeof teams.app.notifySuccess === 'function') teams.app.notifySuccess();
        } catch (e) {}

        try {
          teams.app.getContext().then(function (ctx) {
            applyTeamsTheme(ctx && ctx.app && ctx.app.theme);
          }).catch(function () {});
        } catch (e) {}

        try {
          teams.app.registerOnThemeChangeHandler(applyTeamsTheme);
        } catch (e) {}
      }).catch(function () {
        // Not actually hosted by Teams (or init timed out) — stay a no-op.
      });
    } catch (e) {}
  }

  function loadSdk() {
    try {
      if (window.microsoftTeams) { initTeams(); return; }
      var s = document.createElement('script');
      s.src = TEAMS_SDK_URL;
      s.async = true;
      s.onload = initTeams;
      s.onerror = function () { /* SDK unreachable — app still works standalone-style */ };
      (document.head || document.documentElement).appendChild(s);
    } catch (e) {}
  }

  loadSdk();
})();
