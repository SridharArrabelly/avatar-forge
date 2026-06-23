/*
 * Browser joiner for the in-call avatar (issue #27, Phase 2b — D5 option A1).
 *
 * This is the one piece that cannot run server-side: ACS Call Automation has no
 * "join Teams meeting by URL" API, so a client-side ACS Calling SDK must join the
 * meeting first (as an anonymous interop guest — governed by the meeting lobby,
 * no Teams admin needed) and surface a ServerCallId. We then hand that id to the
 * Python server (POST /api/acs/call) which attaches the bidirectional audio
 * bridge via connect_call().
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
// Phase 2b Slice 2 (avatar face): when the server enables it, the joiner sends an
// outgoing video tile so the avatar is a *visible* participant. The first
// increment is a branded placard (logo + name + a "listening" pulse) drawn to a
// canvas and sent via the ACS raw-video LocalVideoStream — the same path a live
// animated-avatar track will use next.
let avatarVideoEnabled = false;
let _LocalVideoStreamCtor = null; // captured from the SDK at join() time
let localVideoStream = null;      // ACS LocalVideoStream wrapping the placard canvas
let placardStream = null;         // MediaStream from canvas.captureStream()
let placardTimerId = null;        // canvas redraw timer (setInterval keeps frames flowing even when the tab is backgrounded)
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
let captureNode = null;         // ScriptProcessor pulling remote audio -> WS
let captureSink = null;         // zero-gain sink so the capture node runs silently
const wiredRemoteTracks = new Set();
const primingAudioEls = [];     // muted <audio> elements priming remote tracks
let micStream = null;           // getUserMedia mic stream feeding the capture node
let displayStream = null;       // getDisplayMedia stream (far-side / Teams app audio)
let displaySource = null;       // MediaStreamSource for the far-side audio
let captureFrames = 0;          // ScriptProcessor callbacks (diagnostics)
let captureMaxRms = 0;          // peak RMS since last stats report (diagnostics)
let playCursor = 0;             // scheduling cursor for outbound playback
let scheduledSources = [];      // active outbound buffer sources (for barge-in flush)
let captureMutedUntil = 0;      // half-duplex: drop mic capture until this ctx time

function setupOutboundAudio(LocalAudioStream) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: MEDIA_SAMPLE_RATE,
    });
    outboundDest = audioCtx.createMediaStreamDestination();
    // Nuru's synthesized speech is the call's *only* outgoing audio (no mic),
    // which also eliminates the laptop-mic echo we saw while testing.
    outboundLocalStream = new LocalAudioStream(outboundDest.stream);
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
    mediaWs.onopen = () => console.log("[acs-join] media WS open");
    mediaWs.onclose = () => console.log("[acs-join] media WS closed");
    mediaWs.onerror = (e) => console.warn("[acs-join] media WS error", e);
    mediaWs.onmessage = (ev) => {
        if (typeof ev.data === "string") {
            try {
                const msg = JSON.parse(ev.data);
                if (msg.type === "stop_playback") flushPlayback();
            } catch (_) { /* ignore */ }
            return;
        }
        playPcmChunk(new Int16Array(ev.data));
    };
}

// Voice Live PCM16 -> schedule into the outgoing call audio.
// A small jitter buffer (lead time) absorbs network/WS timing variance so the
// scheduled chunks play gap-free instead of underrunning into clicks/breakups.
const PLAYBACK_LEAD = 0.25; // seconds of cushion ahead of the play clock
const CAPTURE_TAIL = 0.4;   // extra mic-mute time after playback drains (anti-echo)
function playPcmChunk(int16) {
    if (!audioCtx || !outboundDest) return;
    const f32 = pcm16ToFloat(int16);
    const buf = audioCtx.createBuffer(1, f32.length, MEDIA_SAMPLE_RATE);
    buf.copyToChannel(f32, 0);
    const node = audioCtx.createBufferSource();
    node.buffer = buf;
    node.connect(outboundDest);
    const now = audioCtx.currentTime;
    // If we've fallen behind (or this is the first chunk of a turn), rebuild the
    // cushion rather than scheduling right at "now", which would underrun.
    if (playCursor < now + 0.02) playCursor = now + PLAYBACK_LEAD;
    node.start(playCursor);
    playCursor += buf.duration;
    // Half-duplex: while Nuru is speaking (and for a short tail afterwards), the
    // mic would otherwise capture her own voice from the Teams-client speaker and
    // feed it back as a new "question". Suppress capture until playback drains.
    captureMutedUntil = playCursor + CAPTURE_TAIL;
    scheduledSources.push(node);
    node.onended = () => {
        const i = scheduledSources.indexOf(node);
        if (i >= 0) scheduledSources.splice(i, 1);
    };
}

// Barge-in: drop everything queued so Nuru stops mid-sentence when a human talks.
function flushPlayback() {
    for (const node of scheduledSources) {
        try { node.stop(); } catch (_) { /* already stopped */ }
    }
    scheduledSources = [];
    playCursor = audioCtx ? audioCtx.currentTime : 0;
    captureMutedUntil = 0; // playback cancelled — re-open the mic immediately
}

// Capture the meeting's remote audio and stream PCM16 to the server.
function allHumansMuted() {
    // From Nuru's leg, the humans are remote participants. If at least one is
    // explicitly unmuted, someone may be talking to the meeting -> listen.
    // If there are none yet, or all are muted, suppress capture.
    try {
        const parts = (call && call.remoteParticipants) ? call.remoteParticipants : [];
        if (!parts.length) return true;
        return !parts.some((p) => p && p.isMuted === false);
    } catch (_) {
        return false; // never hard-fail capture on an inspection error
    }
}

function ensureCaptureNode() {
    if (captureNode) return;
    captureNode = audioCtx.createScriptProcessor(4096, 1, 1);
    captureNode.onaudioprocess = (e) => {
        if (!mediaWs || mediaWs.readyState !== WebSocket.OPEN) return;
        const samples = e.inputBuffer.getChannelData(0);
        // Track signal level so we can tell (from server logs) whether the
        // captured meeting audio is real or all-zero/silent.
        let sum = 0;
        for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
        const rms = Math.sqrt(sum / samples.length);
        if (rms > captureMaxRms) captureMaxRms = rms;
        captureFrames++;
        // Half-duplex gate: don't forward mic audio while Nuru is speaking, so
        // her own voice (from the Teams-client speaker) can't loop back as a
        // new question. Browser AEC can't cancel it (different app's output).
        const selfTalking = !!(audioCtx && audioCtx.currentTime < captureMutedUntil);
        // Privacy gate: Nuru taps the local mic directly, which is independent
        // of the Teams client's mute. But from Nuru's leg the human is a *remote*
        // participant, so we honour their Teams mute — if every human is muted,
        // nothing is legitimately being said to the meeting, so stop listening.
        const humanMuted = allHumansMuted();
        const muted = selfTalking || humanMuted;
        if (captureFrames % 25 === 0) {
            try {
                mediaWs.send(JSON.stringify({
                    type: "capture_stats",
                    frames: captureFrames,
                    maxRms: Number(captureMaxRms.toFixed(5)),
                    ctxRate: audioCtx ? audioCtx.sampleRate : 0,
                    selfTalking,
                    humanMuted,
                    remoteStreams: (call && call.remoteAudioStreams)
                        ? call.remoteAudioStreams.length : 0,
                    wiredTracks: wiredRemoteTracks.size,
                }));
            } catch (_) { /* ignore */ }
            captureMaxRms = 0;
        }
        if (muted) return;
        const pcm = floatToPcm16(samples);
        mediaWs.send(pcm.buffer);
    };
    // A ScriptProcessor only runs while connected to the destination; route it
    // through a zero-gain node so it processes without playing remote audio
    // locally (ACS already renders the meeting audio for us).
    captureSink = audioCtx.createGain();
    captureSink.gain.value = 0;
    captureNode.connect(captureSink);
    captureSink.connect(audioCtx.destination);
}

function wireRemoteAudioStream(stream) {
    try {
        if (!stream || (stream.mediaStreamType && stream.mediaStreamType !== "Audio")) return;
        const track = typeof stream.getMediaStreamTrack === "function"
            ? stream.getMediaStreamTrack()
            : null;
        Promise.resolve(track).then((t) => {
            if (!t || wiredRemoteTracks.has(t.id)) return;
            wiredRemoteTracks.add(t.id);
            ensureCaptureNode();
            const ms = new MediaStream([t]);
            // Chrome/Edge only pull samples from a *remote* WebRTC MediaStream
            // through Web Audio if the stream is also consumed by a playing
            // HTMLMediaElement. Prime it with a muted <audio> element so the
            // ScriptProcessor actually receives the meeting audio.
            const el = new Audio();
            el.muted = true;
            el.srcObject = ms;
            el.play().catch(() => { /* autoplay may defer; track still primed */ });
            primingAudioEls.push(el);
            const src = audioCtx.createMediaStreamSource(ms);
            src.connect(captureNode);
            console.log("[acs-join] wired remote audio track", t.id);
            if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
                try { mediaWs.send(JSON.stringify({ type: "remote_wired", trackId: t.id })); } catch (_) {}
            }
        }).catch((e) => console.warn("[acs-join] getMediaStreamTrack failed", e));
    } catch (e) {
        console.warn("[acs-join] wireRemoteAudioStream failed", e);
    }
}

function startRemoteAudioCapture() {
    ensureCaptureNode();
    // PRIMARY capture path: the local microphone. ACS's Web SDK auto-renders
    // remote meeting audio and does NOT expose it as a raw MediaStreamTrack
    // (getMediaStreamTrack on RemoteAudioStream yields nothing — confirmed:
    // wiredTracks stayed 0, maxRms 0), so we cannot capture the mixed remote
    // audio in the browser. Instead we capture this device's mic — the
    // executive asking the question is at this laptop. echoCancellation strips
    // Nuru's own voice (played by the Teams client) from the captured signal.
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
        src.connect(captureNode);
        console.log("[acs-join] mic capture wired ->", stream.getAudioTracks().length, "track(s)");
        if (mediaWs && mediaWs.readyState === WebSocket.OPEN) {
            try { mediaWs.send(JSON.stringify({ type: "mic_wired", tracks: stream.getAudioTracks().length })); } catch (_) {}
        }
    }).catch((e) => {
        console.warn("[acs-join] mic capture failed", e);
        log(`Microphone capture failed: ${e.message || e}. ${avatarDisplayName} can't hear questions.`);
    });
}

// ───────── outgoing video tile (Phase 2b Slice 2: avatar face) ─────────
// Render a branded placard (logo + avatar name + a "listening" pulse) to a canvas
// and send it as the call's outgoing video, so the avatar is a visible participant
// tile instead of a faceless audio leg. We use the ACS Calling SDK's raw-video
// LocalVideoStream (its constructor accepts a MediaStream), which is the exact
// transport a live animated-avatar video track will use in the next increment.
// The animation also guarantees the encoder keeps emitting frames (a static
// canvas can otherwise stall the WebRTC video sender).
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
    canvas.width = 640;
    canvas.height = 360;
    const ctx = canvas.getContext("2d");
    const img = loadBrandImage();
    const t0 = performance.now();
    function draw() {
        const t = (performance.now() - t0) / 1000;
        ctx.fillStyle = "#0b1020";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        if (img && img.complete && img.naturalWidth) {
            const scale = Math.min(180 / img.naturalWidth, 1);
            const w = img.naturalWidth * scale;
            const h = img.naturalHeight * scale;
            ctx.drawImage(img, (canvas.width - w) / 2, 64, w, h);
        }
        ctx.textAlign = "center";
        ctx.fillStyle = "#ffffff";
        ctx.font = "600 34px -apple-system, 'Segoe UI', system-ui, sans-serif";
        ctx.fillText(name, canvas.width / 2, 286);
        // "listening" status with a gentle pulse (and frame keep-alive).
        const r = 6 + 2 * Math.sin(t * 3);
        ctx.beginPath();
        ctx.fillStyle = "#22c55e";
        ctx.arc(canvas.width / 2 - 64, 318, r, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "rgba(255,255,255,.75)";
        ctx.font = "400 18px -apple-system, 'Segoe UI', system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.fillText("listening", canvas.width / 2 - 48, 324);
        // Force a captured frame even when requestAnimationFrame is throttled (the
        // joiner tab usually sits BEHIND the Teams app). canvas.captureStream pulls a
        // frame on canvas mutation; an explicit requestFrame() guarantees the WebRTC
        // video sender keeps emitting so the tile never freezes/blanks in background.
        try {
            const vt = placardStream && placardStream.getVideoTracks()[0];
            if (vt && typeof vt.requestFrame === "function") vt.requestFrame();
        } catch (_) { /* requestFrame not supported — captureStream fps covers it */ }
    }
    // setInterval (unlike requestAnimationFrame) keeps firing in a backgrounded tab
    // (throttled to ~1s, which is still enough to keep the encoder alive and the
    // logo/name visible) instead of freezing to a blank tile.
    placardStream = canvas.captureStream(15);
    localVideoStream = new _LocalVideoStreamCtor(placardStream);
    placardTimerId = setInterval(draw, 66);
    draw();
    await call.startVideo(localVideoStream);
    console.log("[acs-join] placard video started");
}

function teardownPlacardVideo() {
    if (placardTimerId) {
        try { clearInterval(placardTimerId); } catch (_) {}
        placardTimerId = null;
    }
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
    try { if (mediaWs) mediaWs.close(); } catch (_) {}
    try { if (captureNode) captureNode.disconnect(); } catch (_) {}
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
    displayStream = null; displaySource = null;
    outboundDest = null; outboundLocalStream = null;
    wiredRemoteTracks.clear(); scheduledSources = []; playCursor = 0;
    // Reset the half-duplex mute clock. teardownMedia() closes the audioCtx, so the
    // next join() creates a fresh context whose clock restarts near 0. If we leave a
    // stale (large) captureMutedUntil from the previous context here, the new context's
    // currentTime stays below it for a very long time and selfTalking gets wedged True
    // — silently dropping the mic so the avatar never hears questions after a rejoin.
    captureMutedUntil = 0;
}

// Far-side audio (hear remote participants). The ACS/WebRTC client only exposes
// THIS device's mic — Teams isolates per-client audio by design, so we cannot tap
// other participants' streams from the browser. The supported production path is a
// server-side Teams meeting bot (Graph + Real-Time Media), which needs Graph
// permissions + tenant admin consent. As a no-admin workaround, capture the Teams
// app's *output* audio at the OS level via getDisplayMedia (the user shares the
// Teams window/tab WITH audio) and mix it into the same capture node as the mic.
// Must be triggered by a user gesture.
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
        // Route the shared (far-side) audio into the same capture node as the mic;
        // the half-duplex + human-mute gates already apply to the mixed signal.
        const audioOnly = new MediaStream(audioTracks);
        displaySource = audioCtx.createMediaStreamSource(audioOnly);
        displaySource.connect(captureNode);
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
    // Defensive: clear any stale half-duplex mute / playback cursor from a prior
    // session so a fresh join always starts listening (a new audioCtx restarts the
    // clock near 0, and a leftover captureMutedUntil would otherwise wedge the mic).
    captureMutedUntil = 0; playCursor = 0;

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
        try {
            const deviceManager = await callClient.getDeviceManager();
            await deviceManager.askDevicePermission({ audio: true, video: false });
        } catch (permErr) {
            console.warn("[acs-join] device permission (non-fatal):", permErr);
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
                    // Best-effort: a video failure must never break the audio leg.
                    try { await startPlacardVideo(); }
                    catch (e) { console.warn("[acs-join] placard video failed", e); }
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
