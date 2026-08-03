/*
 * Browser joiner for the in-call avatar (channel C, issue #27).
 *
 * This is the one piece that cannot run server-side, for two separate reasons.
 * ACS Call Automation has no "join Teams meeting by URL" API, so a client-side ACS
 * Calling SDK must join the meeting (as an anonymous interop guest — governed by
 * the meeting lobby, no Teams admin needed). And live testing showed its
 * server-side media streaming does not deliver a Teams *meeting*'s audio at all
 * (every inbound frame arrived flagged silent while someone was speaking), so the
 * media stays client-side too: this page captures audio with Web Audio and streams
 * PCM16 straight to /ws/acs/browser, where BrowserVoiceBridge feeds Voice Live.
 * Server-side attach via connect_call() is NOT used here.
 *
 * Known limit of this leg: a browser only ever receives its own microphone, so the
 * avatar hears the operator, not the room. Hearing everyone is what the .NET media
 * bot (/ws/acs/audio) exists for.
 *
 * SDK delivery (important): the ACS Calling SDK relies on web workers / wasm for
 * its media stack. Loaded as a plain ES module from a CDN (esm.sh) those workers
 * fail to initialise silently, leaving join() stuck in state "None" forever. The
 * Microsoft-supported no-bundler delivery is the UMD browser bundle, which inlines
 * its workers — so we vendor it locally (frontend/vendor/) and load it via a
 * <script> tag, keeping the repo's no-Node guardrail (no build step on the server).
 * Its two small externalised deps (@azure/communication-common, @azure/logger) are
 * loaded as ES modules and exposed as the globals the UMD factory expects.
 */
// Diagnostic switch: ?mic=0 disables local-microphone capture so the only thing
// that can produce signal is the intercepted remote (room) audio. Use it when
// answering "can this leg hear the meeting?" — with the mic live, its own signal
// masks the answer.
const MIC_CAPTURE = new URLSearchParams(location.search).get("mic") !== "0";
// ?remote=0 disables the srcObject interception entirely, restoring the exact
// pre-2026-08-03 behaviour (mic-only capture). This leg is live-verified, so
// there is a way back that does not need a redeploy.
const REMOTE_CAPTURE = new URLSearchParams(location.search).get("remote") !== "0";

const CALLING_UMD = "/vendor/communication-calling-1.40.1.js";
const COMMON_ESM = "https://esm.sh/@azure/communication-common@2.3.1";
const LOGGER_ESM = "https://esm.sh/@azure/logger@1.1.4";

let _sdkPromise = null;
function loadCallingSdk() {
    if (_sdkPromise) return _sdkPromise;
    _sdkPromise = (async () => {
        const [common, logger] = await Promise.all([
            import(COMMON_ESM),
            import(LOGGER_ESM),
        ]);
        // The UMD factory reads these globals (l.communicationCommon, l.logger) at
        // evaluation time, so they must exist before the <script> runs.
        window.communicationCommon = common;
        window.logger = logger;
        await new Promise((resolve, reject) => {
            const s = document.createElement("script");
            s.src = CALLING_UMD;
            s.onload = resolve;
            s.onerror = () => reject(new Error(`failed to load ${CALLING_UMD}`));
            document.head.appendChild(s);
        });
        const sdk = window["azure-communication-calling"];
        if (!sdk || !sdk.CallClient) {
            throw new Error("ACS Calling SDK global not found after loading the UMD bundle");
        }
        console.log(`[acs-join] SDK loaded, apiVersion=${sdk.Call && sdk.Call.apiVersion ? sdk.Call.apiVersion : "?"}`);
        return {
            CallClient: sdk.CallClient,
            Features: sdk.Features,
            LocalAudioStream: sdk.LocalAudioStream,
            LocalVideoStream: sdk.LocalVideoStream,
            AzureCommunicationTokenCredential: common.AzureCommunicationTokenCredential,
        };
    })();
    return _sdkPromise;
}

const $ = (id) => document.getElementById(id);
const statusEl = $("status");
const joinBtn = $("joinBtn");
const leaveBtn = $("leaveBtn");
const muteNuruBtn = $("muteNuruBtn");
const unmuteNuruBtn = $("unmuteNuruBtn");
const farSideBtn = $("farSideBtn");
const linkEl = $("meetingLink");

let call = null;
let callAgent = null;
// Avatar brand name, resolved from the server (/api/acs/config -> AVATAR_DISPLAY_NAME)
// in ensureEnabled() so the participant name is never hardcoded.
let avatarDisplayName = "Avatar";
let _configReady = null;
// Avatar face: when the server enables it, the joiner sends an outgoing video
// tile so the avatar is a *visible* participant. The tile is a canvas we paint —
// the avatar's WebRTC video frames when the transport is up, a branded placard
// (logo + name + status pulse) before that and whenever the track goes quiet —
// handed to ACS through the raw-video LocalVideoStream. The composite is also
// what carries the thinking caption and the wake-phrase hint, which a meeting has
// no other screen for.
let avatarVideoEnabled = false;
let _LocalVideoStreamCtor = null; // captured from the SDK at join() time
let localVideoStream = null;      // ACS LocalVideoStream wrapping the tile canvas
let placardStream = null;         // MediaStream from canvas.captureStream()
let placardTimerId = null;        // canvas redraw timer (setInterval keeps frames flowing even when the tab is backgrounded)
let placardDraw = null;           // current tile paint fn, so decoded frames can drive it
let placardImg = null;            // brand logo image, loaded once from /brand/color.png

// ───────── browser-side media bridge (client-side audio path) ─────────
// Server-side Call Automation media streaming does not deliver Teams *meeting*
// audio, so we move the media through this browser leg instead: capture the
// meeting's remote audio -> our server WS -> Voice Live, and play Voice Live's
// spoken reply back out as this leg's outgoing call audio. 24 kHz mono PCM16
// end-to-end to match Voice Live (no resampling).
const MEDIA_SAMPLE_RATE = 24000;
let audioCtx = null;
let outboundDest = null;        // MediaStreamDestination -> the call's outgoing audio
let outboundLocalStream = null; // ACS LocalAudioStream wrapping outboundDest.stream
let mediaWs = null;
let captureNode = null;         // capture node feeding PCM16 -> WS (worklet, or SP fallback)
let captureSink = null;         // zero-gain sink so the capture node runs silently
let micGate = null;             // GainNode on the microphone path
let roomGate = null;            // GainNode on the room/display taps
let captureVia = "none";        // "worklet" | "scriptprocessor" (diagnostics)
let micOpenNow = true;          // last applied gate states (diagnostics)
let roomOpenNow = true;
let roomSpeakRms = 0;           // peak room-tap RMS measured WHILE she speaks
const wiredRemoteTracks = new Set();
// One record per wired remote audio track, metered and gated INDIVIDUALLY:
//   { id, label, via, analyser, gain, peak, peakSpeak, peakIdle, nSpeak, nIdle }
// "The room tap carries her voice back" has only ever been measured across the
// whole tap at once, which cannot tell an echoing track apart from a human who
// happens to be talking. Per-track levels, split by whether she was speaking,
// can. That distinction decides whether the room gate is protection or damage.
const remoteTracks = [];
const remoteWiredVia = new Set(); // which path(s) delivered remote audio: sdk / srcObject
const pendingRemoteStreams = []; // remote streams seen before audioCtx existed
let remoteRmsScratch = null;    // reusable buffer for analyser reads
let remoteMaxRms = 0;           // peak remote-only RMS since last stats report
let srcObjectHooked = false;
let remoteScanTimer = null;     // periodic sweep of call.remoteAudioStreams
const primingAudioEls = [];     // muted <audio> elements priming remote tracks
let micStream = null;           // getUserMedia mic stream feeding the capture node
let displayStream = null;       // getDisplayMedia stream (far-side / Teams app audio)
let displaySource = null;       // MediaStreamSource for the far-side audio
let captureFrames = 0;          // ScriptProcessor callbacks (diagnostics)
let captureMaxRms = 0;          // peak RMS since last stats report (diagnostics)
let playCursor = 0;             // scheduling cursor for the no-avatar PCM path
// Tile render health. The joiner tab generates the outgoing video, so anything
// that throttles its rendering (backgrounding, occlusion by the Teams window,
// GPU/power state) degrades the tile everyone else sees. Reported so a "it went
// jerky when I moved windows" report can be checked instead of guessed at.
let drawCount = 0;              // canvas repaints since the last stats report
let rvfcCount = 0;              // decoded avatar frames since the last report
let lastDrawMs = 0;
let statsLastMs = 0;
let framePumpVia = "none";      // which clock is driving the repaints
// Fingerprint of the joiner script the server is serving, captured the first time
// we ask. A tab left open across a deploy keeps running its old JS in memory no
// matter what the cache headers say — that happened here and silently invalidated
// a live test round, because the telemetry described a build that was no longer
// deployed. Compare on every config fetch and say so loudly.
let clientBuild = "";
let buildStale = false;
function noteBuildId(id) {
    if (!id) return;
    if (!clientBuild) {
        clientBuild = id;
        console.log(`[acs-join] build ${id}`);
        return;
    }
    if (id !== clientBuild && !buildStale) {
        buildStale = true;
        log("This page is running an OLD build — reload (F5) before testing.");
    }
}
// The web stage covers the wait for the first token with an on-screen "thinking"
// indicator. In a meeting there is no screen — but the avatar's video tile is a
// canvas we draw ourselves, so it can carry the same cue. Voice Live's built-in
// StaticInterimResponseConfig is not an option here: it requires the model
// binding, and the in-call session runs the Foundry agent binding for tools.
//
// The wording and the cadence are the web app's (app.js THINKING_*), so a change
// to the copy lands on both surfaces. Two differences, both deliberate:
//
//   - Shown after 250ms rather than the web's 700ms. A brief blank on a screen is
//     nothing; in a meeting the room hears silence and starts talking over her.
//   - Derived from elapsed time on each frame instead of setInterval/setTimeout.
//     Background tabs clamp timers to ~1Hz and this tab sits behind the Teams
//     window, so timer-driven rotation would lurch. Same behaviour, no timers.
let thinkingSince = 0;
const THINKING_SHOW_AFTER_MS = 250;
const THINKING_CAPTIONS = [
    "Looking through the records…",
    "Checking the latest information…",
    "Pulling the details together…",
];
const THINKING_SLOW_CAPTION = "Just a moment — getting you a reliable answer…";
const THINKING_ROTATE_MS = 2200;
const THINKING_SLOW_MS = 3500;
// Hard ceiling, mirroring the web's failsafe timer: if the "off" message is ever
// lost the cue expires on its own instead of pulsing at the room forever.
const THINKING_MAX_MS = 25000;

// The caption to show right now, or "" when the cue is down.
function thinkingCaption() {
    if (!thinkingSince) return "";
    const age = performance.now() - thinkingSince;
    if (age < THINKING_SHOW_AFTER_MS || age > THINKING_MAX_MS) return "";
    const shown = age - THINKING_SHOW_AFTER_MS;
    if (shown >= THINKING_SLOW_MS) return THINKING_SLOW_CAPTION;
    const i = Math.floor(shown / THINKING_ROTATE_MS) % THINKING_CAPTIONS.length;
    return THINKING_CAPTIONS[i];
}

// Shrink a label until it fits, then report its width. The captions are far
// longer than the single word this tile used to carry, and Teams crops the tile
// horizontally on narrow layouts, so overflow is a real outcome, not a theory.
function fitLabel(ctx, text, weight, startPx, minPx, maxWidth) {
    let px = startPx;
    const font = (p) => `${weight} ${p}px -apple-system, 'Segoe UI', system-ui, sans-serif`;
    ctx.font = font(px);
    while (ctx.measureText(text).width > maxWidth && px > minPx) {
        px -= 1;
        ctx.font = font(px);
    }
    return { px, width: ctx.measureText(text).width };
}
// A suppressed utterance looks exactly like a dead microphone from the room's
// side: she heard the question, understood it, and deliberately said nothing.
// The tile is ours to draw, so it carries the reason. Silent by design — the
// whole point of the wake phrase is not to interject.
let hintText = "";
let hintUntil = 0;
const HINT_MS = 4000;
let scheduledSources = [];      // active outbound buffer sources (for barge-in flush)
let captureMutedUntil = 0;      // room tap: hold it shut until this ctx time

function setupOutboundAudio(LocalAudioStream) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: MEDIA_SAMPLE_RATE,
    });
    outboundDest = audioCtx.createMediaStreamDestination();
    // Nuru's synthesized speech is the call's *only* outgoing audio (no mic),
    // which also eliminates the laptop-mic echo we saw while testing.
    outboundLocalStream = new LocalAudioStream(outboundDest.stream);
    // Any remote stream intercepted before the context existed can now be wired.
    drainPendingRemoteStreams();
    return outboundLocalStream;
}

function floatToPcm16(float32) {
    const out = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
        let s = Math.max(-1, Math.min(1, float32[i]));
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
}

function pcm16ToFloat(int16) {
    const out = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) out[i] = int16[i] / 0x8000;
    return out;
}

function openMediaSocket() {
    const wsUrl = `${location.origin.replace(/^http/, "ws")}/ws/acs/browser`;
    mediaWs = new WebSocket(wsUrl);
    mediaWs.binaryType = "arraybuffer";
    mediaWs.onopen = () => { console.log("[acs-join] media WS open"); flushVideoReports(); };
    mediaWs.onclose = () => console.log("[acs-join] media WS closed");
    mediaWs.onerror = (e) => console.warn("[acs-join] media WS error", e);
    mediaWs.onmessage = (ev) => {
        if (typeof ev.data === "string") {
            try {
                const msg = JSON.parse(ev.data);
                if (msg.type === "stop_playback") {
                    flushPlayback();
                } else if (msg.type === "ice_servers") {
                    setupAvatarWebRTC(msg.iceServers || []);
                } else if (msg.type === "avatar_sdp_answer") {
                    handleAvatarSdpAnswer(msg.serverSdp || "");
                } else if (msg.type === "thinking") {
                    thinkingSince = msg.active ? performance.now() : 0;
                    if (msg.active) hintUntil = 0; // answering beats nudging
                } else if (msg.type === "hint") {
                    hintText = msg.text || "";
                    hintUntil = hintText ? performance.now() + HINT_MS : 0;
                }
            } catch (_) { /* ignore */ }
            return;
        }
        playPcmChunk(new Int16Array(ev.data));
    };
}

// Voice Live PCM16 -> schedule into the outgoing call audio.
//
// This is the NO-AVATAR path only. With the avatar on, her voice arrives as a
// WebRTC media track and is wired straight to outboundDest — the server stops
// sending PCM entirely, so nothing below runs.
//
// It used to carry a second job: holding the audio back to meet a picture that
// had been through MediaSource buffering, decode, a canvas repaint and an encode.
// That reconstruction is gone, and with it the tunable lead, the drift guard and
// the silence shaver that fought each other over where the cursor should sit.
// What is left is an ordinary jitter buffer — enough cushion that chunks play
// gap-free instead of underrunning into clicks.
const PLAYBACK_LEAD = 0.12; // seconds of cushion ahead of the play clock
const MAX_PLAYBACK_LEAD = 0.4;
const CAPTURE_TAIL = 0.4;   // extra room-tap mute time after playback drains (anti-echo)
// Peak amplitude (0..1) above which a chunk counts as *speech* rather than
// digital silence. See the room-tap note below.
const SPEECH_PEAK = 0.01;
function playPcmChunk(int16) {
    if (!audioCtx || !outboundDest) return;
    const f32 = pcm16ToFloat(int16);
    let peak = 0;
    for (let i = 0; i < f32.length; i++) {
        const a = f32[i] < 0 ? -f32[i] : f32[i];
        if (a > peak) peak = a;
    }
    const buf = audioCtx.createBuffer(1, f32.length, MEDIA_SAMPLE_RATE);
    buf.copyToChannel(f32, 0);
    const node = audioCtx.createBufferSource();
    node.buffer = buf;
    node.connect(outboundDest);
    const now = audioCtx.currentTime;
    // If we've fallen behind (or this is the first chunk of a turn), rebuild the
    // cushion rather than scheduling right at "now", which would underrun. The
    // ceiling catches a burst big enough to push playback audibly late.
    if (playCursor < now + 0.02 || playCursor > now + MAX_PLAYBACK_LEAD) {
        playCursor = now + PLAYBACK_LEAD;
    }
    node.start(playCursor);
    playCursor += buf.duration;
    // While she is speaking (and for a short tail afterwards) the raw room tap
    // carries her own voice back from the call mix, and nothing echo-cancels it —
    // so that tap is held shut. The microphone is NOT: the browser has already
    // echo-cancelled it, and closing it is what used to eat the front of a
    // question and cost three attempts to be heard.
    //
    // Arm on ACTUAL SPEECH, never merely on "a chunk arrived" — Voice Live sends
    // exact digital silence between turns rather than nothing at all, so "a chunk
    // arrived" is true forever and would pin the gate shut for the whole call.
    if (peak >= SPEECH_PEAK) captureMutedUntil = playCursor + CAPTURE_TAIL;
    scheduledSources.push(node);
    node.onended = () => {
        const i = scheduledSources.indexOf(node);
        if (i >= 0) scheduledSources.splice(i, 1);
    };
}

// Barge-in. The server has already cancelled the response, so with the avatar on
// her voice track simply stops at the source and there is nothing queued here to
// drop — this reduces to reopening the room tap. The buffer flush still matters on
// the no-avatar path, where chunks are scheduled up to PLAYBACK_LEAD into the future.
function flushPlayback() {
    for (const node of scheduledSources) {
        try { node.stop(); } catch (_) { /* already stopped */ }
    }
    scheduledSources = [];
    playCursor = audioCtx ? audioCtx.currentTime : 0;
    // She has stopped, so the room tap is no longer a feedback path. The microphone
    // needs nothing done to it — it was never closed.
    captureMutedUntil = 0;
    avatarLastAudibleMs = 0;
}

// Capture the meeting's remote audio and stream PCM16 to the server.
function allHumansMuted() {
    // From Nuru's leg, the humans are remote participants. If at least one is
    // explicitly unmuted, someone may be talking to the meeting -> listen.
    //
    // An EMPTY list means we know nothing, not that everyone is muted. This SDK
    // demonstrably under-reports on Teams interop — capture stats have shown
    // remoteStreams=0 while wiredTracks=2 carried real audio — so treating
    // "no participants visible" as "all muted" silently deafens her mid-meeting
    // and the question just vanishes. That was one cause of "sometimes she does
    // not respond". Listen when we cannot tell; only stay quiet when we can
    // actually see every human muted.
    try {
        const parts = (call && call.remoteParticipants) ? call.remoteParticipants : [];
        if (!parts.length) return false;
        return !parts.some((p) => p && p.isMuted === false);
    } catch (_) {
        return false; // never hard-fail capture on an inspection error
    }
}

// The web app's capture processor, copied verbatim from app.js. That pipeline is
// the one that has been tuned over many rounds until barge-in and turn detection
// "work like a champion", so this leg runs the same code rather than a second
// implementation that would have to be tuned all over again.
const PCM16_WORKLET_SRC = `
class PCM16Processor extends AudioWorkletProcessor {
    constructor() {
        super();
        // 40ms at 24kHz = 960 samples. Smaller buffers give tighter
        // barge-in/interruption latency than the previous 100ms.
        this.bufferSize = 960;
        this.buffer = new Float32Array(this.bufferSize);
        this.offset = 0;
        this.silence = new Float32Array(128); // one render quantum
    }
    process(inputs) {
        const input = inputs[0];
        // app.js returns early when there is no input, because it has exactly one
        // always-live microphone source and so never sees that case. This leg
        // gates its sources with GainNodes, and a fully attenuated graph can hand
        // us an empty input — at which point returning early would STOP the
        // stream, which is precisely the failure app.js documents (the server VAD
        // fires speech_started, never sees speech_stopped, and the turn hangs
        // forever). Substituting silence keeps the same invariant it relies on.
        const data = (input && input[0]) ? input[0] : this.silence;
        for (let i = 0; i < data.length; i++) {
            this.buffer[this.offset++] = data[i];
            if (this.offset >= this.bufferSize) {
                const pcm16 = new Int16Array(this.bufferSize);
                for (let j = 0; j < this.bufferSize; j++) {
                    const s = Math.max(-1, Math.min(1, this.buffer[j]));
                    pcm16[j] = s < 0 ? s * 0x8000 : s * 0x7FFF;
                }
                this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
                this.buffer = new Float32Array(this.bufferSize);
                this.offset = 0;
            }
        }
        return true;
    }
}
registerProcessor('pcm16-processor', PCM16Processor);
`;

// Per-source gates.
//
// The web app has exactly one input: a microphone that the browser has already
// echo-cancelled and noise-suppressed. This leg has three summed into one node —
// that microphone PLUS the raw room tap PLUS the optional display capture. The
// extra two are the entire difference, and they are unprocessed: no echo
// canceller sees them, and the room tap carries the call's own mix, which is how
// opening the mic during her answer fed her voice straight back to Voice Live and
// she interrupted herself continuously.
//
// So the gates are per-source rather than one global drop:
//   mic  — always open, exactly as the web app leaves it
//   room — never open while she speaks; it is a feedback path, not a barge-in path
//
// There is deliberately no half-duplex mode. This channel once closed the mic while
// she spoke, and live testing (2026-08-03) was unambiguous: with it on she took three
// attempts to hear a question, because the gate ate the front of every utterance and
// the server VAD never saw a turn start. With the mic simply left open, barge-in was
// immediate and the room did not trigger her once. The web app never had this gate;
// adding it here was the divergence, and removing it is the fix.
function applyCaptureGates() {
    const speaking = !!(audioCtx && audioCtx.currentTime < captureMutedUntil);
    const humanMuted = allHumansMuted();
    // The web app deliberately does NOT gate the mic on playback state — its own
    // comment calls that fragile, since one missed response_done sticks the gate
    // and drops the mic for good. Echo-driven false turns are handled instead by
    // browser AEC + server echo cancellation + barge-in/interrupt in lock-step.
    // Same policy here now.
    micOpenNow = !humanMuted;
    roomOpenNow = !humanMuted && !speaking;
    if (micGate) micGate.gain.value = micOpenNow ? 1 : 0;
    if (roomGate) roomGate.gain.value = roomOpenNow ? 1 : 0;
}

// One captured frame (Int16 PCM, 960 samples / 40ms), whichever node produced it.
function onCaptureFrame(buf) {
    // Drive the "is she speaking" meter from here rather than from a rAF loop.
    // This runs on the audio thread's cadence, which browsers do not throttle;
    // rAF stops dead when the tab is hidden, and this tab lives behind Teams.
    sampleAvatarSpeaking();
    // Render watchdog. setInterval is clamped to ~1Hz and requestVideoFrameCallback
    // stops entirely once the tab is hidden or fully occluded — and the joiner tab
    // normally sits behind the Teams window, so the outgoing tile can silently
    // collapse to a slideshow. Audio keeps flowing regardless, so drive a repaint
    // from here whenever the normal painters have gone quiet.
    if (placardDraw && performance.now() - lastDrawMs > 120) {
        try { placardDraw(); } catch (_) { /* never break capture */ }
    }
    if (!mediaWs || mediaWs.readyState !== WebSocket.OPEN) return;
    applyCaptureGates();
    const pcm = new Int16Array(buf);
    // Track signal level so we can tell (from server logs) whether the captured
    // meeting audio is real or all-zero/silent.
    let sum = 0;
    for (let i = 0; i < pcm.length; i++) {
        const s = pcm[i] / 32768;
        sum += s * s;
    }
    const rms = Math.sqrt(sum / pcm.length);
    if (rms > captureMaxRms) captureMaxRms = rms;
    captureFrames++;
    // Remote-only level, measured independently of the mic so we can tell whether
    // the srcObject interception is actually delivering room audio — and, when
    // sampled during her own speech, whether the room tap really does carry her
    // voice back (roomSpeakRms). That is the measurement behind gating it.
    const speaking = !!(audioCtx && audioCtx.currentTime < captureMutedUntil);
    if (remoteTracks.length) {
        const n = remoteTracks[0].analyser.fftSize;
        if (!remoteRmsScratch || remoteRmsScratch.length !== n) {
            remoteRmsScratch = new Float32Array(n);
        }
        for (let a = 0; a < remoteTracks.length; a++) {
            const rec = remoteTracks[a];
            rec.analyser.getFloatTimeDomainData(remoteRmsScratch);
            let rsum = 0;
            for (let i = 0; i < n; i++) rsum += remoteRmsScratch[i] * remoteRmsScratch[i];
            const rr = Math.sqrt(rsum / n);
            if (rr > remoteMaxRms) remoteMaxRms = rr;
            if (rr > rec.peak) rec.peak = rr;
            // Split by whether SHE was talking at the time. A track loud only while
            // she talks is our own audio returning; a track with energy while she is
            // silent is a human who could be interrupting. No aggregate can separate
            // those two, which is why the old single roomSpeakRms could not settle it.
            if (speaking) {
                rec.nSpeak++;
                if (rr > rec.peakSpeak) rec.peakSpeak = rr;
                if (rr > roomSpeakRms) roomSpeakRms = rr;
            } else {
                rec.nIdle++;
                if (rr > rec.peakIdle) rec.peakIdle = rr;
            }
        }
    }
    // 125 frames = ~5s at 40ms.
    if (captureFrames % 125 === 0) reportCaptureStats();
    // Always send, even when a gate is shut — the gates zero the SIGNAL, they do
    // not stop the STREAM. app.js documents why this matters: cutting the stream
    // mid-utterance orphans the server VAD, which fires speech_started and then
    // never sees speech_stopped, so the turn hangs forever. Continuous silence
    // always lets the server close the turn.
    mediaWs.send(buf);
}

function ensureCaptureNode() {
    if (micGate) return; // gates exist -> callers can wire into them immediately
    // Gates are created synchronously so sources can connect right away, while
    // the worklet module loads asynchronously behind them.
    micGate = audioCtx.createGain();
    roomGate = audioCtx.createGain();
    micGate.gain.value = 1;
    roomGate.gain.value = 1;
    startCaptureWorklet();
}

async function startCaptureWorklet() {
    try {
        const blob = new Blob([PCM16_WORKLET_SRC], { type: "application/javascript" });
        const url = URL.createObjectURL(blob);
        await audioCtx.audioWorklet.addModule(url);
        URL.revokeObjectURL(url);
        const node = new AudioWorkletNode(audioCtx, "pcm16-processor");
        node.port.onmessage = (e) => onCaptureFrame(e.data);
        wireCaptureNode(node, "worklet");
    } catch (e) {
        console.warn("[acs-join] AudioWorklet unavailable — ScriptProcessor fallback", e);
        const node = audioCtx.createScriptProcessor(4096, 1, 1);
        node.onaudioprocess = (ev) => {
            onCaptureFrame(floatToPcm16(ev.inputBuffer.getChannelData(0)).buffer);
        };
        wireCaptureNode(node, "scriptprocessor");
    }
}

function wireCaptureNode(node, via) {
    captureNode = node;
    captureVia = via;
    micGate.connect(node);
    roomGate.connect(node);
    // A capture node only runs while connected to the destination; route it
    // through a zero-gain node so it processes without playing remote audio
    // locally (ACS already renders the meeting audio for us).
    captureSink = audioCtx.createGain();
    captureSink.gain.value = 0;
    node.connect(captureSink);
    captureSink.connect(audioCtx.destination);
    console.log(`[acs-join] capture via ${via}`);
    if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
        try { mediaWs.send(JSON.stringify({ type: "capture_wired", via })); } catch (_) {}
    }
}

function reportCaptureStats() {
    const _nowMs = performance.now();
    const _dt = statsLastMs ? (_nowMs - statsLastMs) / 1000 : 0;
    statsLastMs = _nowMs;
    const drawFps = _dt > 0 ? Math.round(drawCount / _dt) : 0;
    const vFps = _dt > 0 ? Math.round(rvfcCount / _dt) : 0;
    drawCount = 0;
    rvfcCount = 0;
    try {
        mediaWs.send(JSON.stringify({
            type: "capture_stats",
            frames: captureFrames,
            maxRms: Number(captureMaxRms.toFixed(5)),
            ctxRate: audioCtx ? audioCtx.sampleRate : 0,
            capVia: captureVia,
            micOpen: micOpenNow,
            roomOpen: roomOpenNow,
            // Peak room-tap level measured WHILE she was speaking. If this is
            // non-zero the room tap is carrying her own voice back from the call
            // mix, which is why that tap — and only that tap — closes while she
            // speaks. The microphone stays open; it is echo-cancelled.
            roomSpeakRms: Number(roomSpeakRms.toFixed(5)),
            humanMuted: !micOpenNow && !roomOpenNow,
            parts: (call && call.remoteParticipants)
                ? call.remoteParticipants.length : -1,
            remoteStreams: (call && call.remoteAudioStreams)
                ? call.remoteAudioStreams.length : 0,
            wiredTracks: wiredRemoteTracks.size,
            remoteMeters: remoteTracks.length,
            remoteMaxRms: Number(remoteMaxRms.toFixed(5)),
            remoteVia: Array.from(remoteWiredVia).join("+") || "none",
            // Per-track breakdown so the logs say WHICH track carries her voice,
            // not merely that something does. spk/idl are this window's peaks while
            // she was / was not speaking; nS/nI are the frame counts behind them, so
            // a peak of 0 from 2 samples is not mistaken for a silent track.
            tracks: remoteTracks.map((r) => ({
                id: r.id.slice(0, 8),
                via: r.via,
                lbl: (r.label || "").slice(0, 24),
                spk: Number(r.peakSpeak.toFixed(4)),
                idl: Number(r.peakIdle.toFixed(4)),
                nS: r.nSpeak,
                nI: r.nIdle,
            })),
            videoState,
            avatarPic: avatarHasPicture,
            // ICE state of the avatar's peer connection. The picture and her
            // voice both ride it, so "no face and no answer" and "no face only"
            // are different faults and this is what tells them apart.
            avatarIce: avatarPc ? avatarPc.iceConnectionState : "none",
            avatarVoice: !!avatarAnalyser,
            drawFps,
            vFps,
            pumpVia: framePumpVia,
            build: clientBuild,
            stale: buildStale,
            hidden: document.visibilityState !== "visible",
            micCapture: MIC_CAPTURE,
        }));
    } catch (_) { /* ignore */ }
    captureMaxRms = 0;
    remoteMaxRms = 0;
    roomSpeakRms = 0;
    for (let a = 0; a < remoteTracks.length; a++) {
        const r = remoteTracks[a];
        r.peak = 0; r.peakSpeak = 0; r.peakIdle = 0; r.nSpeak = 0; r.nIdle = 0;
    }
}

// ───────── remote (room) audio ─────────
// Two paths, deliberately both present.
//
// 1. DOCUMENTED: RemoteAudioStream.getMediaStream() -> Promise<MediaStream>.
//    This is the supported API and is tried first.
//
//    An earlier version of this file claimed the SDK "will not hand us remote
//    audio", citing a live measurement of wiredTracks 0 / maxRms 0. That
//    measurement was real but the conclusion drawn from it was wrong twice over:
//    the code called getMediaStreamTrack(), which is NOT a member of
//    RemoteAudioStream (the interface exposes getMediaStream() and getVolume()),
//    and the function containing that call was never invoked by anything. So the
//    SDK was never actually asked.
//
// 2. FALLBACK: intercept HTMLMediaElement.prototype.srcObject. The SDK has to
//    *play* remote audio, so it assigns the MediaStream to a media element.
//    Technique taken from the ADIA reference implementation (src/web/bridge.js).
//    This is proven to work in a real meeting, but it rides an implementation
//    detail rather than a contract, which is why it is second choice.
//
// capture_stats reports `via` so the logs say which path actually delivered the
// audio. If the documented path works, `via=sdk` and the hook is redundant
// insurance; if a future SDK renders remote audio purely through Web Audio, the
// hook stops firing and remoteMaxRms returns to 0 — how the regression announces
// itself.
// Track ids we hand to ACS as our outgoing audio. If the SDK ever rendered one
// back into a media element, the srcObject hook would wire her own voice into the
// capture path and she would answer herself.
function isOwnOutboundTrack(id) {
    try {
        if (!outboundDest || !outboundDest.stream) return false;
        return outboundDest.stream.getAudioTracks().some((t) => t.id === id);
    } catch (_) {
        return false;
    }
}

function attachRemoteMediaStream(ms, via) {
    try {
        if (!ms || typeof ms.getAudioTracks !== "function") return;
        const tracks = ms.getAudioTracks();
        if (!tracks.length) return;
        const id = tracks[0].id;
        if (wiredRemoteTracks.has(id)) return;
        // The srcObject hook fires on EVERY media element on this page, including
        // the two we create for the avatar. Wiring her own voice into the room tap
        // would post it straight back to Voice Live as the next question, so she
        // would interrupt herself on every answer.
        if (avatarOwnTracks.has(id)) return;
        // Our own OUTGOING call audio, if the SDK ever renders it locally. Same
        // hazard as avatarOwnTracks, but checked by identity against the stream we
        // actually handed to ACS rather than relying on a registration winning a race.
        if (isOwnOutboundTrack(id)) return;
        // audioCtx is created in setupOutboundAudio() before join(), but a stream
        // arriving first must not be dropped on the floor.
        if (!audioCtx) { pendingRemoteStreams.push({ ms, via }); return; }
        wiredRemoteTracks.add(id);
        ensureCaptureNode();
        const src = audioCtx.createMediaStreamSource(ms);
        // Metered separately from the mic so the diagnostic is unambiguous. The
        // analyser sits BEFORE the gain, so it keeps reading the true level even
        // while this track is gated shut — otherwise closing the gate would erase
        // the very evidence needed to judge whether closing it was right.
        const analyser = audioCtx.createAnalyser();
        analyser.fftSize = 2048;
        const trackGain = audioCtx.createGain();
        trackGain.gain.value = 1;
        src.connect(analyser);
        src.connect(trackGain);
        trackGain.connect(roomGate);
        remoteTracks.push({
            id,
            label: tracks[0].label || "",
            via: via || "srcObject",
            analyser,
            gain: trackGain,
            peak: 0, peakSpeak: 0, peakIdle: 0, nSpeak: 0, nIdle: 0,
        });
        remoteWiredVia.add(via || "srcObject");
        console.log(`[acs-join] remote audio wired via ${via || "srcObject"}`, id);
        if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
            try { mediaWs.send(JSON.stringify({ type: "remote_wired", trackId: id, via: via || "srcObject" })); } catch (_) {}
        }
    } catch (e) {
        console.warn("[acs-join] attachRemoteMediaStream failed", e);
    }
}

function drainPendingRemoteStreams() {
    if (!audioCtx || !pendingRemoteStreams.length) return;
    const queued = pendingRemoteStreams.splice(0, pendingRemoteStreams.length);
    queued.forEach((q) => attachRemoteMediaStream(q.ms, q.via));
}

function installSrcObjectHook() {
    if (srcObjectHooked) return;
    if (!REMOTE_CAPTURE) {
        console.log("[acs-join] srcObject hook disabled (?remote=0) — mic-only capture");
        return;
    }
    const desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "srcObject");
    if (!desc || !desc.set) {
        console.warn("[acs-join] srcObject is not a configurable accessor — remote capture unavailable");
        return;
    }
    Object.defineProperty(HTMLMediaElement.prototype, "srcObject", {
        configurable: true,
        enumerable: desc.enumerable,
        get() { return desc.get.call(this); },
        set(v) { attachRemoteMediaStream(v, "srcObject"); return desc.set.call(this, v); },
    });
    srcObjectHooked = true;
    console.log("[acs-join] srcObject hook installed");
}
// Must be in place before the SDK attaches any remote audio element.
installSrcObjectHook();

function wireRemoteAudioStream(stream) {
    try {
        if (!stream) return;
        if (stream.mediaStreamType && stream.mediaStreamType !== "Audio") return;
        // The documented member of RemoteAudioStream. getMediaStreamTrack(), which
        // this used to call, does not exist on the interface.
        if (typeof stream.getMediaStream !== "function") return;
        if (stream.isAvailable === false) return;
        Promise.resolve(stream.getMediaStream()).then((ms) => {
            if (!ms) return;
            // Attach BEFORE priming: assigning srcObject below trips our own hook,
            // and whichever call lands first owns the `via` label. The documented
            // path must win that race or the diagnostic would always read
            // "srcObject" and we would never learn whether the SDK path works.
            attachRemoteMediaStream(ms, "sdk");
            // Chrome only pulls samples from a *remote* WebRTC MediaStream through
            // Web Audio while an HTMLMediaElement is also consuming it. The SDK
            // plays its own element, but muteIncomingAudio() (the echo guard in
            // startBrowserMedia) may stop that, so prime one here rather than
            // depend on the SDK's.
            const el = new Audio();
            el.muted = true;
            el.srcObject = ms;
            el.play().catch(() => { /* autoplay may defer; track still primed */ });
            primingAudioEls.push(el);
        }).catch((e) => console.warn("[acs-join] getMediaStream failed", e));
    } catch (e) {
        console.warn("[acs-join] wireRemoteAudioStream failed", e);
    }
}

// Nothing ever called wireRemoteAudioStream before, which is the other half of
// why the SDK path looked dead. Sweep the call's remote audio streams on connect
// and on a timer so participants who join later are picked up too; wiring is
// idempotent (deduped by track id), so re-scanning is free.
function scanRemoteAudioStreams() {
    try {
        const streams = (call && call.remoteAudioStreams) ? call.remoteAudioStreams : [];
        for (const s of streams) wireRemoteAudioStream(s);
    } catch (e) {
        console.warn("[acs-join] scanRemoteAudioStreams failed", e);
    }
}

function startRemoteAudioCapture() {
    ensureCaptureNode();
    // Room audio arrives via wireRemoteAudioStream (documented getMediaStream())
    // with the srcObject hook as a second path — see the block above. The mic
    // remains a SEPARATE source because it is the near-field signal for whoever
    // is at this laptop, and because it is the only one that survives if both
    // remote paths fail. echoCancellation strips Nuru's own voice (played by the
    // Teams client) out of that near-field capture.
    if (!MIC_CAPTURE) {
        console.log("[acs-join] mic capture disabled (?mic=0) — remote audio only");
        log("Mic capture disabled — listening to meeting audio only.");
        return;
    }
    navigator.mediaDevices.getUserMedia({
        audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
        },
        video: false,
    }).then((stream) => {
        micStream = stream;
        const src = audioCtx.createMediaStreamSource(stream);
        src.connect(micGate);
        console.log("[acs-join] mic capture wired ->", stream.getAudioTracks().length, "track(s)");
        if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
            try { mediaWs.send(JSON.stringify({ type: "mic_wired", tracks: stream.getAudioTracks().length })); } catch (_) {}
        }
    }).catch((e) => {
        console.warn("[acs-join] mic capture failed", e);
        log(`Microphone capture failed: ${e.message || e}. ${avatarDisplayName} can't hear questions.`);
    });
}

// ───────── live avatar face + voice (WebRTC) ─────────
// The avatar arrives here exactly as it does in the web app: Voice Live
// negotiates a peer connection and delivers the rendered face and the answer
// audio as two tracks on it, muxed and clocked by the transport.
//
// This replaced a design where the server relayed a fragmented-MP4 stream and
// this file rebuilt A/V sync by hand — MediaSource for the picture, a scheduling
// cursor with a tunable lead for the voice, plus a drift guard and a silence
// shaver to stop the two ratcheting apart. Every lip-sync complaint traced to
// that reconstruction, and none of it exists any more: the voice is wired
// straight into the outgoing call audio, and the picture is painted off a
// <video> the transport is driving. Fix something on the web app's avatar path
// now and this channel inherits it, because it IS the same path.
//
// The outgoing tile is still a canvas we composite, because a meeting has no
// screen for the "thinking" cue or the wake-phrase hint — those overlays ride on
// top of the face. Compositing costs a frame or two of CONSTANT delay; unlike a
// scheduling cursor it has nothing that can accumulate, so it cannot drift.

// Video has several ways to fail silently (startVideo rejecting, ICE never
// connecting, an SDP answer that will not apply) and every one of them used to
// land in console.warn — invisible to the operator running the joiner and
// invisible in the container logs. A face that never appears then looks identical
// to a face that was never enabled. Report the state to BOTH the on-page log and
// the backend so it is diagnosable without a browser console.
let videoState = "off";
const pendingVideoReports = [];
function flushVideoReports() {
    if (!mediaWs || mediaWs.readyState !== WebSocket.OPEN) return;
    while (pendingVideoReports.length) {
        try { mediaWs.send(JSON.stringify(pendingVideoReports.shift())); }
        catch (_) { return; }
    }
}
function reportVideo(state, detail) {
    videoState = state;
    const line = detail ? `${state}: ${detail}` : state;
    console.log(`[acs-join] video ${line}`);
    if (state === "failed" || state === "unsupported") log(`Avatar video ${line}`);
    // openMediaSocket() runs first (inside startBrowserMedia, awaited just before
    // startPlacardVideo), so the socket normally exists by the time the first video
    // report fires — but it is still CONNECTING, and send() on a CONNECTING socket
    // throws. Queue rather than drop: the error detail is the whole point, and
    // capture_stats only carries the bare state.
    pendingVideoReports.push({
        type: "video_status", state, detail: detail ? String(detail) : "",
    });
    flushVideoReports();
}

let avatarLiveVideo = false;        // server says the live avatar stream is enabled
let avatarPc = null;                // RTCPeerConnection carrying the avatar's media
let avatarVideoEl = null;           // offscreen <video> fed by the WebRTC video track
let avatarPrimeEl = null;           // muted <audio> keeping the voice track flowing
let avatarPumpTrack = null;         // cloned video track feeding the frame pump
let avatarVoiceSource = null;       // her voice, wired into the outgoing call audio
let avatarAnalyser = null;          // level meter driving "she is speaking"
let avatarLevelBuf = null;
let avatarLastAudibleMs = 0;        // last moment her voice carried real energy
let avatarHasPicture = false;       // first frame decoded -> safe to paint
let avatarLastDrawMs = 0;           // last time the picture actually advanced
// Track ids belonging to the avatar. installSrcObjectHook() intercepts EVERY
// srcObject assignment on the page, so without this her own voice would be wired
// into the room tap and posted back to Voice Live as a question — she would
// interrupt herself on every single answer.
const avatarOwnTracks = new Set();

// ── ICE, ported from app.js ──
// Read an ICE candidate's type ("host" / "srflx" / "relay"). Chromium exposes
// `.type` directly; parse the SDP candidate line as a fallback.
function iceCandidateType(candidate) {
    if (!candidate) return null;
    if (candidate.type) return candidate.type;
    const parts = String(candidate.candidate || "").split(" ");
    const i = parts.indexOf("typ");
    return i >= 0 ? (parts[i + 1] || null) : null;
}
// A "host" candidate is a private LAN address and can never reach the Azure
// avatar service; only srflx (NAT-reflexive) or relay (TURN) can. An offer
// carrying nothing but a host candidate fails exactly like an empty one, so the
// question is not "do we have a candidate?" but "do we have a reachable one?".
function isConnectableCandidate(candidate) {
    const t = iceCandidateType(candidate);
    return t === "srflx" || t === "relay";
}
// Keep gathering briefly after the first relay candidate so its siblings land in
// the same offer. This app does NOT trickle ICE — the offer must carry every
// candidate it will ever have — so whatever is in it is all there will ever be.
const ICE_RELAY_SETTLE_MS = 250;
const ICE_GATHER_TIMEOUT_MS = 1500;
const ICE_GATHER_MAX_WAIT_MS = 8000;

// Voice Live sends its ICE servers once the session is configured; that is the
// signal to negotiate. Built from scratch each time — app.js keeps a prewarmed
// peer connection to shave startup off a user-facing page load, which buys
// nothing here: the joiner negotiates once, unattended, while the operator is
// still pasting a meeting link.
function setupAvatarWebRTC(iceServers) {
    if (avatarPc || !avatarLiveVideo) return;
    reportVideo("connecting", `${(iceServers || []).length} ICE server(s)`);
    try {
        const iceConfig = (iceServers || []).map((s) => ({
            urls: s.urls,
            username: s.username || undefined,
            credential: s.credential || undefined,
        }));
        const pc = new RTCPeerConnection({ iceServers: iceConfig, iceCandidatePoolSize: 4 });
        avatarPc = pc;
        pc.ontrack = (event) => attachAvatarWebRtcTrack(event);
        pc.oniceconnectionstatechange = () => {
            if (pc !== avatarPc) return;
            const st = pc.iceConnectionState;
            if (st === "failed" || st === "disconnected") reportVideo("failed", `ICE ${st}`);
        };

        let offerSent = false;
        let relaySettleTimer = null;
        let connectableCount = 0;
        let awaitingUsable = false;
        const sendOfferOnce = (reason) => {
            if (offerSent) return;
            offerSent = true;
            clearTimeout(relaySettleTimer);
            if (!mediaWs || mediaWs.readyState !== WebSocket.OPEN) {
                reportVideo("failed", "media socket closed before the SDP offer");
                return;
            }
            // Base64-encoded JSON in both directions — the shape Voice Live expects.
            const sdpBase64 = btoa(JSON.stringify(pc.localDescription));
            mediaWs.send(JSON.stringify({ type: "avatar_sdp_offer", clientSdp: sdpBase64 }));
            console.log(`[acs-join] avatar SDP offer sent (${reason})`);
        };

        pc.onicecandidate = (event) => {
            if (!event.candidate) { sendOfferOnce("gathering complete"); return; }
            if (isConnectableCandidate(event.candidate)) connectableCount += 1;
            // Degraded path: the backstop already passed with nothing reachable, so
            // take the first connectable candidate of either kind rather than
            // holding out for a relay that may never come.
            if (awaitingUsable) {
                if (connectableCount > 0 && !relaySettleTimer) {
                    relaySettleTimer = setTimeout(
                        () => sendOfferOnce("usable candidate after timeout"), ICE_RELAY_SETTLE_MS);
                }
                return;
            }
            if (iceCandidateType(event.candidate) === "relay" && !relaySettleTimer) {
                relaySettleTimer = setTimeout(
                    () => sendOfferOnce("relay candidate ready"), ICE_RELAY_SETTLE_MS);
            }
        };

        pc.addTransceiver("video", { direction: "sendrecv" });
        pc.addTransceiver("audio", { direction: "sendrecv" });
        pc.addEventListener("datachannel", (event) => {
            event.channel.onmessage = (e) => handleAvatarDataChannelMessage(e.data);
        });
        pc.createDataChannel("eventChannel");

        pc.createOffer()
            .then((offer) => pc.setLocalDescription(offer))
            .then(() => {
                // Backstop only — sendOfferOnce above should normally have fired
                // on a relay candidate long before this.
                setTimeout(() => {
                    if (offerSent) return;
                    if (connectableCount > 0) { sendOfferOnce("gathering timed out"); return; }
                    // Nothing reachable gathered yet. Without trickle, an offer now
                    // could never connect, so wait for a usable candidate instead
                    // of shipping a guaranteed-dead session.
                    awaitingUsable = true;
                    setTimeout(() => sendOfferOnce("no usable candidates"),
                        ICE_GATHER_MAX_WAIT_MS - ICE_GATHER_TIMEOUT_MS);
                }, ICE_GATHER_TIMEOUT_MS);
            })
            .catch((err) => reportVideo("failed", `offer: ${(err && err.message) || err}`));
    } catch (e) {
        reportVideo("failed", `setup: ${(e && e.message) || e}`);
    }
}

function handleAvatarSdpAnswer(serverSdpBase64) {
    if (!avatarPc || !serverSdpBase64) return;
    try {
        // Base64-encoded JSON: {"type":"answer","sdp":"..."}
        const answer = JSON.parse(atob(serverSdpBase64));
        avatarPc.setRemoteDescription(new RTCSessionDescription(answer))
            .then(() => console.log("[acs-join] avatar remote SDP set"))
            .catch((err) => reportVideo("failed", `sdp answer: ${(err && err.message) || err}`));
    } catch (e) {
        reportVideo("failed", `sdp parse: ${(e && e.message) || e}`);
    }
}

// One track off the avatar's peer connection: video becomes the tile's picture
// source, audio becomes the call's outgoing voice.
function attachAvatarWebRtcTrack(event) {
    try {
        const track = event.track;
        if (!track) return;
        // Register BEFORE any srcObject assignment below, or our own hook wires
        // her voice into the room tap.
        avatarOwnTracks.add(track.id);
        // Each element gets its OWN track, never event.streams[0]. Handing a
        // <video> a stream that also carries audio makes it "autoplay with sound",
        // which browsers refuse on a first visit — the avatar then never starts
        // and nothing reports why.
        const stream = new MediaStream([track]);

        if (track.kind === "video") {
            const v = document.createElement("video");
            v.autoplay = true;
            v.playsInline = true;
            // Muted, and video-only: the voice rides the separate audio track, and
            // a muted element is the one thing every autoplay policy allows
            // unconditionally. An element that will not start produces no frames,
            // and a tile with no frames looks identical to a failed join.
            v.muted = true;
            v.srcObject = stream;
            v.addEventListener("loadeddata", () => { avatarHasPicture = true; });
            v.addEventListener("playing", () => {
                if (avatarVideoEl !== v) return;
                avatarHasPicture = true;
                reportVideo("face-live", `${v.videoWidth}x${v.videoHeight}`);
                startAvatarFramePump(track, v);
            });
            v.addEventListener("error", () => {
                const err = v.error;
                reportVideo("failed", `video element: code=${err ? err.code : "?"}`);
            });
            avatarVideoEl = v;
            v.play().catch((err) => reportVideo("failed", `play: ${(err && err.name) || err}`));
            return;
        }

        if (track.kind !== "audio") return;
        if (!audioCtx || !outboundDest) {
            reportVideo("failed", "avatar voice arrived before the audio graph existed");
            return;
        }
        // Chrome only pulls samples from a REMOTE WebRTC stream through Web Audio
        // while an HTMLMediaElement is also consuming it — the same constraint the
        // room tap hits. Prime with a MUTED element: her voice must not leave this
        // laptop's speakers, or the microphone would capture it and feed it back.
        const prime = new Audio();
        prime.muted = true;
        prime.srcObject = stream;
        prime.play().catch(() => { /* autoplay may defer; the track is still primed */ });
        avatarPrimeEl = prime;

        const src = audioCtx.createMediaStreamSource(stream);
        avatarVoiceSource = src;
        // Straight into the call's outgoing audio. No queue, no cursor, no lead:
        // what the transport delivers is what the room hears, when it arrives.
        src.connect(outboundDest);
        // Tapped for level only. Deliberately NOT connected to audioCtx.destination
        // — that would play her out of the operator's speakers.
        avatarAnalyser = audioCtx.createAnalyser();
        avatarAnalyser.fftSize = 512;
        avatarAnalyser.smoothingTimeConstant = 0.3;
        avatarLevelBuf = new Uint8Array(avatarAnalyser.fftSize);
        src.connect(avatarAnalyser);
        console.log("[acs-join] avatar voice wired to the outgoing call audio");
    } catch (e) {
        reportVideo("failed", `track: ${(e && e.message) || e}`);
    }
}

// The avatar service brackets each spoken turn on the data channel. Used only as
// a decaying hint that she has started — never as a latch. A latch that misses
// its closing event wedges the capture gate shut for the rest of the call, which
// is the failure app.js warns about and this file has already lived through once.
function handleAvatarDataChannelMessage(data) {
    if (typeof data !== "string") return;
    if (data.indexOf("EVENT_TYPE_SWITCH_TO_SPEAKING") !== -1) {
        avatarLastAudibleMs = performance.now();
    }
}

// "Is she speaking right now?" — replaces the peak detector that used to live in
// playPcmChunk. In avatar mode there are no PCM chunks, so the level is measured
// off the WebRTC audio track itself, which is exactly what the room hears.
//
// Sampled from onCaptureFrame (the capture worklet's ~25Hz callback), NOT from
// requestAnimationFrame. app.js can use rAF because its tab is the one you are
// looking at; this tab sits behind the Teams window, where rAF stops entirely —
// and a gate driven by a clock that stops is a gate that never reopens.
const AVATAR_SPEAK_RMS = 0.01;
const AVATAR_SPEAK_HANGOVER_MS = 400;
function sampleAvatarSpeaking() {
    if (!avatarAnalyser || !avatarLevelBuf || !audioCtx) return;
    avatarAnalyser.getByteTimeDomainData(avatarLevelBuf);
    let sumSq = 0;
    for (let i = 0; i < avatarLevelBuf.length; i++) {
        const s = (avatarLevelBuf[i] - 128) / 128;
        sumSq += s * s;
    }
    const rms = Math.sqrt(sumSq / avatarLevelBuf.length);
    const now = performance.now();
    if (rms > AVATAR_SPEAK_RMS) avatarLastAudibleMs = now;
    // Expressed in the currency applyCaptureGates() already reads, so the gate
    // logic itself is untouched. It only ever moves forward while real energy is
    // present, so it drains on its own — it cannot stick shut.
    if (now - avatarLastAudibleMs < AVATAR_SPEAK_HANGOVER_MS) {
        captureMutedUntil = audioCtx.currentTime + (AVATAR_SPEAK_HANGOVER_MS / 1000);
    }
}

// Repaint the tile in step with decoded frames rather than resampling the stream
// on a timer: the avatar renders at ~25fps, and a 15fps timer landing between
// source frames showed some twice and skipped others — that uneven cadence is
// what read as jerky, "robotic" motion.
//
// Prefer MediaStreamTrackProcessor, which reads decoded frames straight off the
// media pipeline. requestVideoFrameCallback stops dead the moment the tab is
// hidden (measured in-meeting: vFps 25 -> 0), and this tab normally sits behind
// the Teams window, so that is the common case rather than an edge case. The
// track is CLONED first so the processor is a second, independent sink and
// cannot starve the <video> the canvas paints from.
function startAvatarFramePump(track, v) {
    const paint = () => {
        rvfcCount += 1;
        if (placardDraw) { try { placardDraw(); } catch (_) { /* never break the pump */ } }
    };
    let gotFrames = false;
    try {
        if (typeof window.MediaStreamTrackProcessor === "function") {
            const clone = track.clone();
            avatarPumpTrack = clone;
            const reader = new window.MediaStreamTrackProcessor({ track: clone })
                .readable.getReader();
            framePumpVia = "frames";
            (async () => {
                for (;;) {
                    const { value, done } = await reader.read();
                    if (done) break;
                    try { value.close(); } catch (_) {}
                    if (avatarVideoEl !== v) break;
                    gotFrames = true;
                    paint();
                }
                try { reader.cancel(); } catch (_) {}
            })();
            // Don't trust it blindly: if nothing arrives, fall back rather than
            // leaving the tile with no clock at all.
            setTimeout(() => {
                if (!gotFrames && avatarVideoEl === v) {
                    reportVideo("pump-fallback", "no frames from MediaStreamTrackProcessor");
                    startRvfcPump(v, paint);
                }
            }, 2500);
            return;
        }
    } catch (e) {
        reportVideo("pump-fallback", `trackprocessor: ${(e && e.message) || e}`);
    }
    startRvfcPump(v, paint);
}

function startRvfcPump(v, paint) {
    if (typeof v.requestVideoFrameCallback !== "function") return;
    framePumpVia = framePumpVia === "frames" ? "frames+rvfc" : "rvfc";
    const step = () => {
        if (avatarVideoEl !== v) return; // torn down — let the loop die
        paint();
        try { v.requestVideoFrameCallback(step); } catch (_) {}
    };
    try { v.requestVideoFrameCallback(step); } catch (_) {}
}

function teardownAvatarVideo() {
    try { if (avatarPc) avatarPc.close(); } catch (_) {}
    avatarPc = null;
    try { if (avatarVideoEl) { avatarVideoEl.pause(); avatarVideoEl.srcObject = null; } } catch (_) {}
    avatarVideoEl = null;
    try { if (avatarPrimeEl) { avatarPrimeEl.pause(); avatarPrimeEl.srcObject = null; } } catch (_) {}
    avatarPrimeEl = null;
    try { if (avatarPumpTrack) avatarPumpTrack.stop(); } catch (_) {}
    avatarPumpTrack = null;
    try { if (avatarVoiceSource) avatarVoiceSource.disconnect(); } catch (_) {}
    avatarVoiceSource = null;
    avatarAnalyser = null;
    avatarLevelBuf = null;
    avatarOwnTracks.clear();
    avatarHasPicture = false;
    avatarLastDrawMs = 0;
    avatarLastAudibleMs = 0;
    framePumpVia = "none";
    videoState = "off";
}

// Paint the current avatar frame onto the tile canvas. Returns false when there
// is nothing live to show, so the caller falls back to the branded placard.
//
// "Live" means the <video> has a picture AND its clock is still advancing. On the
// WebRTC transport the track is continuous, so between turns she simply sits there
// idle and this stays true — which is what the web app shows too, and is right for
// a meeting. The idle check therefore only fires on a genuinely dead track (peer
// connection lost, sender stopped), where a frozen face staring at the room would
// look broken. Kept as that safety net, not as the between-turns behaviour.
const AVATAR_IDLE_MS = 900;
let _avatarLastTime = -1;
function drawAvatarFrame(ctx, canvas) {
    const v = avatarVideoEl;
    if (!avatarLiveVideo || !v || !avatarHasPicture) return false;
    if (!v.videoWidth || !v.videoHeight) return false;

    const now = performance.now();
    if (v.currentTime !== _avatarLastTime) {
        _avatarLastTime = v.currentTime;
        avatarLastDrawMs = now;
    } else if (now - avatarLastDrawMs > AVATAR_IDLE_MS) {
        return false; // track is dead, not idle -> show the placard
    }

    // Contain-fit: show the WHOLE frame. The avatar renders SQUARE (512x512); the
    // tile is wider, so cover-fit would centre-crop the overflow and lop off the
    // top and bottom of her head. Brand-coloured bars either side are much better
    // than a decapitated close-up. Bias the letterbox slightly upward so the eyes
    // sit near the optical centre.
    const scale = Math.min(canvas.width / v.videoWidth, canvas.height / v.videoHeight);
    const w = v.videoWidth * scale;
    const h = v.videoHeight * scale;
    ctx.drawImage(v, (canvas.width - w) / 2, (canvas.height - h) / 2, w, h);
    return true;
}

// ───────── outgoing video tile (avatar face) ─────────
// Render to a canvas and send it as the call's outgoing video, so the avatar is a
// visible participant tile instead of a faceless audio leg. Uses the ACS Calling
// SDK's raw-video LocalVideoStream (its constructor accepts a MediaStream).
//
// The canvas is kept rather than handing ACS the WebRTC video track directly,
// because a meeting has no screen for the "thinking" cue or the wake-phrase hint —
// those are composited onto the tile here. It costs a frame or two of CONSTANT
// delay, which (unlike the scheduling cursor it replaced) has nothing that can
// accumulate, so it cannot drift. The placard is the pre-connection and dead-track
// state; once the avatar's track is live, drawAvatarFrame() paints her instead.
//
// The animation also guarantees the encoder keeps emitting frames (a static canvas
// can otherwise stall the WebRTC video sender).
function loadBrandImage() {
    if (placardImg) return placardImg;
    placardImg = new Image();
    placardImg.crossOrigin = "anonymous";
    placardImg.src = "/brand/color.png";
    return placardImg;
}

async function startPlacardVideo() {
    if (!call || !_LocalVideoStreamCtor) return;
    if (localVideoStream) return; // already on
    const name = avatarDisplayName || "Avatar";
    const canvas = document.createElement("canvas");
    // 4:3 rather than 16:9. The avatar frame is square, so a wider tile just means
    // bigger empty bars once we contain-fit it; 4:3 is a legitimate webcam aspect
    // and leaves her face filling 75% of the width instead of 56%.
    canvas.width = 640;
    canvas.height = 480;
    const ctx = canvas.getContext("2d");
    const img = loadBrandImage();
    const t0 = performance.now();
    function keepFrameAlive() {
        // Force a captured frame even when requestAnimationFrame is throttled (the
        // joiner tab usually sits BEHIND the Teams app). canvas.captureStream pulls a
        // frame on canvas mutation; an explicit requestFrame() guarantees the WebRTC
        // video sender keeps emitting so the tile never freezes/blanks in background.
        try {
            const vt = placardStream && placardStream.getVideoTracks()[0];
            if (vt && typeof vt.requestFrame === "function") vt.requestFrame();
        } catch (_) { /* requestFrame not supported — captureStream fps covers it */ }
    }
    function drawThinking(t) {
        // Only after a short delay, so quick answers never flash a badge.
        const label = thinkingCaption();
        if (!label) return;
        const maxW = canvas.width * 0.94 - 62;
        const fit = fitLabel(ctx, label, "500", 20, 13, maxW);
        const w = fit.width + 62;
        const x = (canvas.width - w) / 2;
        // Keep the badge inside the 16:9 centre crop. The tile canvas is 4:3 and
        // Teams crops to its own aspect, lopping off roughly the top and bottom
        // eighth — a badge pinned near canvas.height was simply never on screen.
        const y = Math.round(canvas.height * 0.72);
        ctx.textAlign = "left";
        ctx.fillStyle = "rgba(11,16,32,.78)";
        if (ctx.roundRect) {
            ctx.beginPath(); ctx.roundRect(x, y, w, 34, 17); ctx.fill();
        } else {
            ctx.fillRect(x, y, w, 34);
        }
        // Three dots cycling, so it reads as activity rather than a frozen tile.
        for (let i = 0; i < 3; i++) {
            const on = Math.floor(t * 3) % 3 === i;
            ctx.beginPath();
            ctx.fillStyle = on ? "#f59e0b" : "rgba(255,255,255,.35)";
            ctx.arc(x + 20 + i * 12, y + 17, on ? 4.5 : 3.5, 0, Math.PI * 2);
            ctx.fill();
        }
        ctx.fillStyle = "rgba(255,255,255,.92)";
        ctx.fillText(label, x + 52, y + 17 + fit.px * 0.35);
    }
    function drawHint() {
        if (!hintUntil || performance.now() > hintUntil) return;
        // Never stack on the thinking badge — they share the safe band, and if
        // she is answering the nudge is already moot.
        if (thinkingSince) return;
        ctx.font = "500 19px -apple-system, 'Segoe UI', system-ui, sans-serif";
        const w = ctx.measureText(hintText).width + 34;
        const x = (canvas.width - w) / 2;
        const y = Math.round(canvas.height * 0.72);
        // Fade the last 600ms so it retreats rather than blinking out.
        const left = hintUntil - performance.now();
        const a = Math.min(1, left / 600);
        ctx.textAlign = "left";
        ctx.fillStyle = `rgba(11,16,32,${0.78 * a})`;
        if (ctx.roundRect) {
            ctx.beginPath(); ctx.roundRect(x, y, w, 34, 17); ctx.fill();
        } else {
            ctx.fillRect(x, y, w, 34);
        }
        ctx.fillStyle = `rgba(255,255,255,${0.92 * a})`;
        ctx.fillText(hintText, x + 17, y + 23);
    }
    function draw() {
        drawCount += 1;
        lastDrawMs = performance.now();
        const t = (performance.now() - t0) / 1000;
        ctx.fillStyle = "#0b1020";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        // Live avatar face, when we have one and it is actually advancing.
        // The avatar renders SQUARE (512x512) while the tile is 16:9, so scale to
        // cover the height and centre-crop horizontally rather than letterboxing
        // a small face into a wide black box.
        if (drawAvatarFrame(ctx, canvas)) {
            drawThinking(t);
            drawHint();
            keepFrameAlive();
            return;
        }
        if (img && img.complete && img.naturalWidth) {
            const scale = Math.min(180 / img.naturalWidth, 1);
            const w = img.naturalWidth * scale;
            const h = img.naturalHeight * scale;
            ctx.drawImage(img, (canvas.width - w) / 2, canvas.height * 0.18, w, h);
        }
        // Placard text is positioned proportionally so the layout survives a change
        // of tile aspect ratio (it was hand-tuned for 640x360, now 640x480).
        const H = canvas.height;
        ctx.textAlign = "center";
        ctx.fillStyle = "#ffffff";
        ctx.font = "600 34px -apple-system, 'Segoe UI', system-ui, sans-serif";
        ctx.fillText(name, canvas.width / 2, H * 0.79);
        // Status line (and frame keep-alive). The placard has no face to sit
        // under, so the caption rides here rather than in a separate badge —
        // stacking the two in the same safe band would collide with the name.
        // Dot and text are centred as a group so a long caption stays put.
        const caption = thinkingCaption();
        const thinking = !!caption;
        const label = thinking ? caption : "listening";
        const fit = fitLabel(ctx, label, "400", 18, 12, canvas.width * 0.9 - 24);
        const left = (canvas.width - (24 + fit.width)) / 2;
        const r = 6 + 2 * Math.sin(t * (thinking ? 6 : 3));
        ctx.beginPath();
        ctx.fillStyle = thinking ? "#f59e0b" : "#22c55e";
        ctx.arc(left + 8, H * 0.823, r, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "rgba(255,255,255,.75)";
        ctx.textAlign = "left";
        ctx.fillText(label, left + 24, H * 0.84);
        drawHint();
        keepFrameAlive();
    }
    // setInterval (unlike requestAnimationFrame) keeps firing in a backgrounded tab
    // (throttled to ~1s, which is still enough to keep the encoder alive and the
    // logo/name visible) instead of freezing to a blank tile.
    placardStream = canvas.captureStream(30);
    localVideoStream = new _LocalVideoStreamCtor(placardStream);
    placardDraw = draw;
    // 30fps keep-alive. When the avatar is live, requestVideoFrameCallback drives
    // the repaints and this merely guarantees frames keep flowing (placard state,
    // backgrounded tab) instead of the tile freezing.
    placardTimerId = setInterval(draw, 33);
    draw();
    await call.startVideo(localVideoStream);
    reportVideo("tile-on", `${canvas.width}x${canvas.height}`);
    watchTabVisibility();
}

let _visWatch = false;
function watchTabVisibility() {
    if (_visWatch) return;
    _visWatch = true;
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") return;
        // Measured: hidden drops the tile from 55fps to ~6fps and stops
        // requestVideoFrameCallback outright. Say so, because from the meeting side
        // it just looks like the avatar broke.
        log("Tab hidden — keep this tab visible, or the avatar's video degrades.");
    });
    // Re-check the served build so a tab left open across a deploy announces
    // itself instead of quietly reporting telemetry for code that is no longer live.
    setInterval(async () => {
        try {
            const r = await fetch("/api/acs/config", { cache: "no-store" });
            noteBuildId((await r.json()).buildId);
        } catch (_) { /* transient — try again next tick */ }
    }, 60000);
}

function teardownPlacardVideo() {
    if (placardTimerId) {
        try { clearInterval(placardTimerId); } catch (_) {}
        placardTimerId = null;
    }
    placardDraw = null;
    const lvs = localVideoStream;
    localVideoStream = null;
    if (call && lvs && typeof call.stopVideo === "function") {
        call.stopVideo(lvs).catch((e) => console.warn("[acs-join] stopVideo failed", e));
    }
    try { if (placardStream) placardStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
    placardStream = null;
}

function teardownMedia() {
    teardownPlacardVideo();
    teardownAvatarVideo();
    if (remoteScanTimer) { clearInterval(remoteScanTimer); remoteScanTimer = null; }
    try { if (mediaWs) mediaWs.close(); } catch (_) {}
    try { if (captureNode) captureNode.disconnect(); } catch (_) {}
    try { if (micGate) micGate.disconnect(); } catch (_) {}
    try { if (roomGate) roomGate.disconnect(); } catch (_) {}
    try { if (captureSink) captureSink.disconnect(); } catch (_) {}
    try { if (micStream) micStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
    try { if (displaySource) displaySource.disconnect(); } catch (_) {}
    try { if (displayStream) displayStream.getTracks().forEach((t) => t.stop()); } catch (_) {}
    try { if (audioCtx) audioCtx.close(); } catch (_) {}
    for (const el of primingAudioEls) {
        try { el.pause(); el.srcObject = null; } catch (_) {}
    }
    primingAudioEls.length = 0;
    mediaWs = null; captureNode = null; captureSink = null; audioCtx = null; micStream = null;
    micGate = null; roomGate = null; captureVia = "none";
    displayStream = null; displaySource = null;
    outboundDest = null; outboundLocalStream = null;
    wiredRemoteTracks.clear(); scheduledSources = []; playCursor = 0;
    // These outlived the audioCtx before: stale analysers from a closed context
    // stay readable and keep inflating remoteMeters while always reporting 0,
    // which would make the remote-audio diagnostic lie after a rejoin.
    remoteTracks.length = 0;
    remoteWiredVia.clear();
    pendingRemoteStreams.length = 0;
    remoteMaxRms = 0;
    // Reset the "she is speaking" clock. teardownMedia() closes the audioCtx, so the
    // next join() creates a fresh context whose clock restarts near 0. If we leave a
    // stale (large) captureMutedUntil from the previous context here, the new context's
    // currentTime stays below it for a very long time and the capture gates stay shut
    // — silently dropping audio so the avatar never hears questions after a rejoin.
    captureMutedUntil = 0;
}

// Far-side audio (hear remote participants), FALLBACK path.
//
// This is no longer the primary way to hear the room: installSrcObjectHook() taps
// the remote streams the SDK hands to its own <audio> elements, which is verified
// live (micCapture=False with a non-zero remoteMaxRms and a correct transcript).
// Note what the old comment here got wrong — the SDK's getMediaStreamTrack()
// returning nothing is a fact about ONE METHOD, not evidence that the browser
// cannot receive other participants' audio. It plainly does; that is how the call
// is audible.
//
// Keep this as the fallback for when the hook does not engage (an SDK that renders
// audio some other way). The user shares the Teams window/tab WITH audio and we mix
// its output into the room gate. Must be triggered by a user gesture.
async function startFarSideCapture() {
    if (!audioCtx) { log("Join the meeting first, then capture far-side audio."); return; }
    try {
        const stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
        const audioTracks = stream.getAudioTracks();
        if (!audioTracks.length) {
            stream.getTracks().forEach((t) => t.stop());
            log(`No audio was shared. Re-click and tick “Share audio” / “Share tab audio” in the picker so ${avatarDisplayName} can hear the far side.`);
            return;
        }
        // We only want the audio; drop the video track immediately to save resources.
        stream.getVideoTracks().forEach((t) => t.stop());
        displayStream = stream;
        ensureCaptureNode();
        // Shared (far-side) audio is a room tap like the SDK's own: unprocessed,
        // and it carries whatever the Teams window is playing — including her.
        // So it rides the room gate, not the mic gate.
        const audioOnly = new MediaStream(audioTracks);
        displaySource = audioCtx.createMediaStreamSource(audioOnly);
        displaySource.connect(roomGate);
        // If the user stops sharing via the browser bar, clean up.
        audioTracks[0].addEventListener("ended", () => {
            try { if (displaySource) displaySource.disconnect(); } catch (_) {}
            displaySource = null; displayStream = null;
            log(`Far-side audio sharing stopped. ${avatarDisplayName} now hears only this device's mic.`);
        });
        console.log("[acs-join] far-side (display) audio wired ->", audioTracks.length, "track(s)");
        if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
            try { mediaWs.send(JSON.stringify({ type: "farside_wired", tracks: audioTracks.length })); } catch (_) {}
        }
        log(`Far-side audio connected — ${avatarDisplayName} can now hear shared meeting audio. Keep the share running.`);
    } catch (e) {
        console.warn("[acs-join] getDisplayMedia failed", e);
        log(`Far-side capture cancelled or failed: ${e.message || e}`);
    }
}

// Host controls for Nuru's outgoing voice. Teams lets anyone *mute* her from the
// roster (handled via the mutedByOthers stop+re-arm above), but only the muted
// party can unmute — so the host re-enables her here. call.mute()/unmute() toggles
// her LocalAudioStream (outgoing audio) in the meeting; we also tell the server to
// suppress/resume generation so we don't waste Voice Live work while muted.
async function muteNuru() {
    try {
        if (call && typeof call.mute === "function") await call.mute();
        if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
            try { mediaWs.send(JSON.stringify({ type: "hard_mute" })); } catch (_) {}
        }
        flushPlayback();
        muteNuruBtn.disabled = true;
        unmuteNuruBtn.disabled = false;
        log(`${avatarDisplayName} muted. She won't speak until you unmute her.`);
    } catch (e) {
        console.warn("[acs-join] muteNuru failed", e);
        log(`Could not mute ${avatarDisplayName}: ${e.message || e}`);
    }
}

async function unmuteNuru() {
    try {
        if (call && typeof call.unmute === "function") await call.unmute();
        if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
            try { mediaWs.send(JSON.stringify({ type: "hard_unmute" })); } catch (_) {}
        }
        muteNuruBtn.disabled = false;
        unmuteNuruBtn.disabled = true;
        log(`${avatarDisplayName} unmuted. She'll answer when addressed.`);
    } catch (e) {
        console.warn("[acs-join] unmuteNuru failed", e);
        log(`Could not unmute ${avatarDisplayName}: ${e.message || e}`);
    }
}

function log(msg) {
    console.log("[acs-join]", msg);
    statusEl.textContent = msg;
}

// Surface any error the SDK swallows inside its async media/connect pipeline —
// the "stuck at state None with no error" symptom is exactly what these catch.
window.addEventListener("error", (e) => {
    console.error("[acs-join] window error:", e.message, e.error || "");
});
window.addEventListener("unhandledrejection", (e) => {
    console.error("[acs-join] unhandled rejection:", e.reason);
});

async function ensureEnabled() {
    const res = await fetch("/api/acs/config");
    const cfg = await res.json();
    if (!cfg.enabled) {
        log("ACS in-call media is not enabled on this deployment (set ACS_CONNECTION_STRING / ACS_ENDPOINT and ENABLE_ACS=true).");
        joinBtn.disabled = true;
        return false;
    }
    // Branding name comes from the server (AVATAR_DISPLAY_NAME) — never hardcoded.
    avatarDisplayName = (cfg.avatarDisplayName || "Avatar").trim();
    avatarVideoEnabled = !!cfg.avatarVideoEnabled;
    avatarLiveVideo = !!cfg.avatarLiveVideo;
    if (avatarLiveVideo) console.log("[acs-join] live avatar video enabled");
    noteBuildId(cfg.buildId);
    return true;
}

// Teams exposes two join-link shapes and ACS needs a different locator for each:
//   - classic: https://teams.microsoft.com/l/meetup-join/19%3ameeting_...  -> { meetingLink }
//   - new:     https://teams.microsoft.com/meet/<id>?p=<passcode>          -> { meetingId, passcode }
// The new short links are NOT accepted by the meetingLink locator, so detect and
// translate them to a TeamsMeetingIdLocator. Returns the locator object to pass to join().
function buildMeetingLocator(raw) {
    const link = (raw || "").trim();
    if (link.includes("/l/meetup-join/")) {
        return { meetingLink: link };
    }
    // New "/meet/<id>" links (with the passcode in ?p=).
    const meetMatch = link.match(/\/meet\/([^/?#]+)/i);
    if (meetMatch) {
        const meetingId = decodeURIComponent(meetMatch[1]);
        let passcode = "";
        try { passcode = new URL(link).searchParams.get("p") || ""; } catch (e) { /* ignore */ }
        return passcode ? { meetingId, passcode } : { meetingId };
    }
    // Bare numeric meeting id (passcode would have to be appended as ?p=...).
    if (/^\d{6,}$/.test(link)) {
        return { meetingId: link };
    }
    // Last resort: hand it over as a meetingLink and let ACS validate it.
    return { meetingLink: link };
}

function describeEndReason(r) {
    // Common ACS interop subCodes seen when joining Teams meetings.
    const map = {
        5854: "The meeting link/id was rejected by the service (wrong format or expired).",
        5300: "Join was rejected — the tenant may block anonymous/interop participants (tenant policy).",
        5000: "Removed from the call.",
        10037: "Could not be admitted from the lobby (timed out or declined).",
    };
    if (r && map[r.subCode]) return map[r.subCode];
    if (r && r.code === 0) return "Normal hang up.";
    return "See the browser console for the full error.";
}

async function join() {
    const meetingLink = linkEl.value.trim();
    if (!meetingLink) { log("Paste a Teams meeting join link first."); return; }
    // Guard: only ever hand ACS something that actually looks like a Teams meeting
    // reference. (Defends against the input being polluted with status text or
    // other stray content, which otherwise produces a cryptic "Join failed".)
    const looksLikeMeeting =
        /teams\.microsoft\.com/i.test(meetingLink) ||
        /meetup-join/i.test(meetingLink) ||
        /\/meet\//i.test(meetingLink) ||
        /^\d{6,}$/.test(meetingLink);
    if (!looksLikeMeeting) {
        log("That doesn't look like a Teams meeting link. Clear the box and paste the full join link from the meeting invite (it contains \"teams.microsoft.com\").");
        return;
    }
    joinBtn.disabled = true;
    // Defensive: clear any stale room-tap mute / playback cursor from a prior
    // session so a fresh join always starts listening (a new audioCtx restarts the
    // clock near 0, and a leftover captureMutedUntil would otherwise wedge the tap).
    captureMutedUntil = 0; playCursor = 0; avatarLastAudibleMs = 0;

    try {
        // Ensure the branding name (AVATAR_DISPLAY_NAME) has loaded before we
        // create the call agent with it.
        try { await _configReady; } catch (_) { /* falls back to "Avatar" */ }
        log("Requesting an ACS access token…");
        const tokRes = await fetch("/api/acs/token", { method: "POST" });
        if (!tokRes.ok) throw new Error(`token endpoint returned ${tokRes.status}`);
        const { token } = await tokRes.json();

        log("Loading the ACS Calling SDK…");
        const { CallClient, Features, LocalAudioStream, LocalVideoStream, AzureCommunicationTokenCredential } = await loadCallingSdk();
        _LocalVideoStreamCtor = LocalVideoStream;

        log("Initialising the ACS Calling SDK…");
        const callClient = new CallClient();
        const credential = new AzureCommunicationTokenCredential(token);

        // Build Nuru's outgoing audio from synthesized speech (not a mic), so she
        // can speak into the meeting and there is no laptop-mic echo. This is the
        // call's local audio stream, passed into join() below.
        const localAudio = setupOutboundAudio(LocalAudioStream);
        try { await audioCtx.resume(); } catch (_) { /* resumes on first gesture */ }

        // Initialise the device manager (some SDK builds require it before join).
        // Mic permission is best-effort only — we send synthesized audio, not mic.
        //
        // VIDEO permission matters even though the outgoing tile is a canvas
        // MediaStream that never touches a camera: the SDK gates startVideo() on
        // the browser's video permission state regardless of the stream's origin
        // (its own failure text is "Failed to start video ... ensure to allow
        // video permissions"). Asking for audio only, as this did, meant
        // startVideo() could never succeed and the avatar had no tile at all.
        try {
            const deviceManager = await callClient.getDeviceManager();
            const perms = await deviceManager.askDevicePermission({ audio: true, video: true });
            console.log(`[acs-join] device permissions audio=${perms && perms.audio} video=${perms && perms.video}`);
            if (perms && perms.video === false) {
                log("Camera permission was not granted — the avatar's video tile will not appear.");
            }
        } catch (permErr) {
            // A machine with no camera can reject the video half outright; the
            // audio leg must still come up, so fall back rather than abort.
            console.warn("[acs-join] device permission (non-fatal):", permErr);
            try {
                const dm = await callClient.getDeviceManager();
                await dm.askDevicePermission({ audio: true, video: false });
            } catch (_) { /* best effort */ }
        }

        callAgent = await callClient.createCallAgent(credential, {
            displayName: `${avatarDisplayName} (AI assistant)`,
        });

        const locator = buildMeetingLocator(meetingLink);
        log(`Joining the Teams meeting via ${locator.meetingId ? "meeting id" : "meeting link"} (you may wait in the lobby until admitted)…`);
        console.log("[acs-join] locator", locator);
        // Join with our synthesized audio as the local stream (no mic). Empty
        // options otherwise — passing a populated videoOptions stalls the join.
        call = callAgent.join(locator, {
            audioOptions: { localAudioStreams: [localAudio], muted: false },
        });
        console.log(`[acs-join] join() returned, call.id=${call && call.id}, state=${call && call.state}`);

        // Network + media user-facing diagnostics — these reveal ICE/connectivity or
        // device problems that otherwise leave the call silently stuck at "None".
        try {
            const getFeature = (call.feature || call.api).bind(call);
            const diag = getFeature(Features.UserFacingDiagnostics);
            diag.network.on("diagnosticChanged", (d) =>
                console.warn(`[acs-join] network diag: ${d.diagnostic}=${d.value} (${d.valueType})`));
            diag.media.on("diagnosticChanged", (d) =>
                console.warn(`[acs-join] media diag: ${d.diagnostic}=${d.value} (${d.valueType})`));
        } catch (diagErr) {
            console.warn("[acs-join] could not attach UserFacingDiagnostics:", diagErr);
        }

        leaveBtn.disabled = false;
        // Poll the call state for ~30s so we can see whether it's stuck in
        // "Connecting"/"InLobby" vs never leaving "None" (diagnostics for the spike).
        let polls = 0;
        const poller = setInterval(() => {
            polls += 1;
            console.log(`[acs-join] poll#${polls} call.state=${call && call.state}`);
            if (!call || polls > 15 || call.state === "Connected" || call.state === "Disconnected") {
                clearInterval(poller);
            }
        }, 2000);
        call.on("stateChanged", async () => {
            log(`Call state: ${call.state}`);
            if (call.state === "Connected") {
                await startBrowserMedia();
                if (avatarVideoEnabled) {
                    // Best-effort: a video failure must never break the audio leg — but it
                    // must be VISIBLE, or "no face" is indistinguishable from "not enabled".
                    try { await startPlacardVideo(); }
                    catch (e) { reportVideo("failed", `startVideo: ${e && e.message ? e.message : e}`); }
                }
            }
            if (call.state === "Disconnected") {
                teardownPlacardVideo();
                const r = call.callEndReason || {};
                log(`Call ended (code ${r.code ?? "?"}, subCode ${r.subCode ?? "?"}). ${describeEndReason(r)}`);
                leaveBtn.disabled = true;
                joinBtn.disabled = false;
                muteNuruBtn.disabled = true;
                unmuteNuruBtn.disabled = true;
                farSideBtn.disabled = true;
            }
        });
    } catch (e) {
        log(`Join failed: ${e.message || e}`);
        joinBtn.disabled = false;
    }
}

async function startBrowserMedia() {
    try {
        log(`Connected. Bridging meeting audio to ${avatarDisplayName}…`);
        openMediaSocket();
        startRemoteAudioCapture();
        // Ask the SDK for the room audio (documented path). Re-scan on a timer so
        // participants who join after us are picked up; wiring is deduped by track
        // id, so repeat scans are no-ops.
        scanRemoteAudioStreams();
        if (remoteScanTimer) clearInterval(remoteScanTimer);
        remoteScanTimer = setInterval(scanRemoteAudioStreams, 3000);
        // Stop the SDK from rendering the meeting's incoming audio out the local
        // speaker. We capture it for Voice Live via Web Audio (muted <audio>
        // priming keeps the WebRTC track flowing), so local rendering is pure
        // echo when Nuru's leg shares a device with the user's Teams client.
        setTimeout(async () => {
            try {
                if (call && typeof call.muteIncomingAudio === "function") {
                    await call.muteIncomingAudio();
                    console.log("[acs-join] incoming audio muted (echo guard)");
                    if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
                        try { mediaWs.send(JSON.stringify({ type: "incoming_muted" })); } catch (_) {}
                    }
                }
            } catch (e) {
                console.warn("[acs-join] muteIncomingAudio failed", e);
            }
        }, 1500);
        // Let meeting participants stop Nuru mid-answer using the standard Teams
        // "mute participant" action: when she's muted by others, cut the current
        // answer immediately, then auto-unmute so she's ready for the next
        // question (she's a bot, so others can't unmute her — she re-arms herself).
        try {
            call.on("mutedByOthers", () => {
                console.log("[acs-join] muted by others -> stop talking + re-arm");
                flushPlayback();
                if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
                    try { mediaWs.send(JSON.stringify({ type: "interrupt" })); } catch (_) {}
                }
                setTimeout(() => {
                    if (call && typeof call.unmute === "function") {
                        call.unmute().catch((e) => console.warn("[acs-join] re-unmute failed", e));
                    }
                }, 600);
            });
        } catch (_) { /* event not in this SDK build */ }
        log(`${avatarDisplayName} is live in the call. Ask a question aloud and she'll answer.`);
        muteNuruBtn.disabled = false;
        unmuteNuruBtn.disabled = true;
        farSideBtn.disabled = false;
    } catch (e) {
        log(`Media bridge failed: ${e.message || e}`);
    }
}

async function leave() {
    leaveBtn.disabled = true;
    try {
        if (call) await call.hangUp();
    } catch (e) {
        console.warn("hangUp error", e);
    }
    teardownMedia();
    call = null;
    log("Left the meeting.");
    joinBtn.disabled = false;
    muteNuruBtn.disabled = true;
    unmuteNuruBtn.disabled = true;
    farSideBtn.disabled = true;
}

joinBtn.addEventListener("click", join);
leaveBtn.addEventListener("click", leave);
muteNuruBtn.addEventListener("click", muteNuru);
unmuteNuruBtn.addEventListener("click", unmuteNuru);
farSideBtn.addEventListener("click", startFarSideCapture);

// The Companion control panel (companion.html, opened in a separate window so the
// ACS Calling leg runs OUTSIDE the Teams meeting webview) hands the meeting link
// over via ?meeting=. Prefill it so the user does not paste twice.
try {
    const prefill = new URLSearchParams(window.location.search).get("meeting");
    if (prefill) linkEl.value = prefill;
} catch (e) { /* ignore */ }

_configReady = ensureEnabled();
