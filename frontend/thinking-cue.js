// Single source of truth for the "thinking" cue: the indicator shown between a
// question and the first word of the answer, so the user knows she heard them.
//
// Two front ends render it and they render it very differently — the web stage
// (app.js) as a DOM pill, the in-call meeting tile (acs-join.js) composited onto
// a canvas, because a meeting has no screen to put a pill on. What they must NOT
// differ on is what the cue says and when it says it, so the wording and the
// timings live here and both sides read them.
//
// Loaded as a classic script, like brand.js, because index.html loads app.js as
// a classic script while acs-join.html loads its script as a module. Classic
// scripts run before deferred module scripts, so this is in place either way.
(function (global) {
    // Keyed by the tool name the backend reports (or predicts). Anything not
    // listed here deliberately produces no caption: the cue falls back to a
    // wordless "still working" rather than inventing a claim about the work.
    const CAPTIONS = {
        search_minutes: "Checking the records…",
        search_web: "Searching the web…",
    };

    global.THINKING_CUE = {
        CAPTIONS,

        // Neutral phase. The web stage shows animated dots and no text; the
        // meeting tile has no room for dots, so it shows this instead.
        NEUTRAL_CAPTION: "…",

        // Shown once the wait outruns the caption it started with.
        SLOW_CAPTION: "Still working, nearly there…",

        // When a PREDICTED retrieval may be promoted to a caption, measured from
        // response_created. Only matters in agent binding, where the Foundry
        // agent runs its tools server-side and Voice Live reports no tool event
        // at all — a prediction is then the only thing available. Sits above the
        // measured no-retrieval turn time (~1.1-1.5s to first token) so a
        // conversational reply is already on screen before its guess can show.
        PREDICT_MS: 1800,

        // How long any one caption stands before escalating to SLOW_CAPTION.
        // Measured from the current caption, not the start of the turn.
        SLOW_MS: 3500,

        // Failsafe. If the "answer started" signal is ever lost, the cue expires
        // instead of pulsing forever.
        MAX_MS: 25000,

        captionFor(name) {
            return (name && CAPTIONS[name]) || "";
        },
    };
})(typeof globalThis !== "undefined" ? globalThis : window);
