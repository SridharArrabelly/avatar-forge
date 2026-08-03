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
// Full duplex: keep listening while she speaks, so a human can cut her off
// mid-answer. Off by default because the half-duplex gate is what stops her own
// voice (played by the Teams client, which browser AEC cannot cancel — it is a
// different app's output) looping back in as a new question. On headphones there
// is no such loop, and barge-in matters more, so ?duplex=full turns the gate off.
const FULL_DUPLEX = new URLSearchParams(location.search).get("duplex") === "full";
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
// Avatar face: when the server enables it, the joiner sends an
// outgoing video tile so the avatar is a *visible* participant. The first
// increment is a branded placard (logo + name + a "listening" pulse) drawn to a
// canvas and sent via the ACS raw-video LocalVideoStream — the same path a live
// animated-avatar track will use next.
let avatarVideoEnabled = false;
let _LocalVideoStreamCtor = null; // captured from the SDK at join() time
let localVideoStream = null;      // ACS LocalVideoStream wrapping the placard canvas
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
let captureNode = null;         // ScriptProcessor pulling remote audio -> WS
let captureSink = null;         // zero-gain sink so the capture node runs silently
const wiredRemoteTracks = new Set();
const remoteAnalysers = [];     // per-remote-stream meters (remote-only RMS)
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
let playCursor = 0;             // scheduling cursor for outbound playback
let avLead = 0;                 // seconds playback is scheduled ahead of the clock
let avResyncs = 0;              // times the drift guard had to pull it back
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
let thinkingSince = 0;
const THINKING_SHOW_AFTER_MS = 250;
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
                    skipAvatarToLiveEdge();
                } else if (msg.type === "video_data") {
                    handleAvatarChunk(msg.delta);
                } else if (msg.type === "thinking") {
                    thinkingSince = msg.active ? performance.now() : 0;
                }
            } catch (_) { /* ignore */ }
            return;
        }
        playPcmChunk(new Int16Array(ev.data));
    };
}

// Voice Live PCM16 -> schedule into the outgoing call audio.
// A small jitter buffer (lead time) absorbs network/WS timing variance so the
// scheduled chunks play gap-free instead of underrunning into clicks/breakups.
// It doubles as the A/V sync offset:
// Offset between the audio we schedule and the picture MediaSource is playing.
// These are NOT equal-length paths: video goes through MediaSource buffering,
// decode, a canvas repaint and a WebRTC encode, while the PCM goes almost
// straight out — so the audio has to be held back to meet it. Measured live
// in-meeting: at 0.44s the voice trailed the lips, at 0.15s it ran ahead of
// them, so the crossover sits between. Tunable per-join with ?lead=0.30 so it
// can be dialled in during a call instead of needing a redeploy.
const _leadParam = Number(new URLSearchParams(location.search).get("lead"));
const TARGET_LEAD = (Number.isFinite(_leadParam) && _leadParam > 0 && _leadParam <= 1)
    ? _leadParam
    : 0.28;
const PLAYBACK_LEAD = TARGET_LEAD; // seconds of cushion ahead of the play clock
// Hard ceiling before we resync. Silence-shaving (below) does the routine work;
// this only catches a burst big enough that waiting for silence would be worse.
const MAX_PLAYBACK_LEAD = TARGET_LEAD + 0.15;
const CAPTURE_TAIL = 0.4;   // extra mic-mute time after playback drains (anti-echo)
// Peak amplitude (0..1) above which a chunk counts as *speech* rather than the
// avatar's idle silence. See the half-duplex note in playPcmChunk.
const SPEECH_PEAK = 0.01;
function playPcmChunk(int16) {
    if (!audioCtx || !outboundDest) return;
    const f32 = pcm16ToFloat(int16);
    let peak = 0;
    for (let i = 0; i < f32.length; i++) {
        const a = f32[i] < 0 ? -f32[i] : f32[i];
        if (a > peak) peak = a;
    }
    const now = audioCtx.currentTime;
    // Shave the A/V lead back down during silence (see TARGET_LEAD). Dropping the
    // chunk leaves playCursor where it is, so the clock catches up to it.
    if (peak < SPEECH_PEAK && playCursor > now + TARGET_LEAD) {
        avLead = playCursor - now;
        return;
    }
    const buf = audioCtx.createBuffer(1, f32.length, MEDIA_SAMPLE_RATE);
    buf.copyToChannel(f32, 0);
    const node = audioCtx.createBufferSource();
    node.buffer = buf;
    node.connect(outboundDest);
    // If we've fallen behind (or this is the first chunk of a turn), rebuild the
    // cushion rather than scheduling right at "now", which would underrun.
    if (playCursor < now + 0.02) {
        playCursor = now + PLAYBACK_LEAD;
    } else if (playCursor > now + MAX_PLAYBACK_LEAD) {
        // A/V drift guard. playCursor only ever reset when it fell BEHIND the
        // clock, which with the avatar enabled never happens: the muxed AAC track
        // streams continuously for the whole session (she idles on camera between
        // turns), so chunks never stop arriving. Every network burst schedules a
        // clump further into the future and the lead ratchets up permanently —
        // by minutes into a call the audio ran seconds behind the MediaSource
        // video, which plays live. That is the "lips move, voice follows" gap.
        // Without the avatar there are gaps between turns, the cursor falls
        // behind and self-corrects, which is why this only showed up with video.
        avResyncs += 1;
        playCursor = now + TARGET_LEAD;
    }
    avLead = playCursor - now;
    node.start(playCursor);
    playCursor += buf.duration;
    // Half-duplex: while Nuru is speaking (and for a short tail afterwards), the
    // mic would otherwise capture her own voice from the Teams-client speaker and
    // feed it back as a new "question". Suppress capture until playback drains.
    //
    // Arm this on ACTUAL SPEECH, not merely on "a chunk arrived". With the avatar
    // enabled our PCM comes from the avatar's muxed AAC track, which streams
    // CONTINUOUSLY for the whole session (she idles on camera between turns) — so
    // "a chunk arrived" is true forever, which pinned captureMutedUntil ahead of
    // the clock and wedged the mic shut: she never heard a single question.
    if (peak >= SPEECH_PEAK) captureMutedUntil = playCursor + CAPTURE_TAIL;
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
    avLead = 0;
    captureMutedUntil = 0; // playback cancelled — re-open the mic immediately
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

function ensureCaptureNode() {
    if (captureNode) return;
    // NOTE (perf, deliberate): this is a main-thread ScriptProcessor, while the
    // web app (app.js) captures via an AudioWorklet at 960 samples / 40 ms. At
    // 24 kHz, 4096 samples is 170 ms of buffering before a sample leaves the
    // browser, and app.js carries a comment recording that smaller buffers gave
    // it tighter barge-in latency. So this leg is ~130 ms behind the web app on
    // the question path, and shares a thread with the avatar canvas + MediaSource.
    // Left as-is on purpose: this is the no-admin FALLBACK leg (the Graph media
    // bot is the real one) and the half-duplex gate below took several live
    // sessions to stabilise. Porting it to an AudioWorklet is worthwhile but is
    // a change to a live-verified media path and wants its own live test round.
    captureNode = audioCtx.createScriptProcessor(4096, 1, 1);
    captureNode.onaudioprocess = (e) => {
        // Render watchdog. setInterval is clamped to ~1Hz and requestVideoFrameCallback
        // stops entirely once the tab is hidden or fully occluded — and the joiner tab
        // normally sits behind the Teams window, so the outgoing tile can silently
        // collapse to a slideshow. The audio thread is never throttled, so drive a
        // repaint from here whenever the normal painters have gone quiet. It only
        // restores ~6fps (this callback is 4096 samples / 170ms), but that is the
        // difference between a moving tile and a frozen one.
        if (placardDraw && performance.now() - lastDrawMs > 120) {
            try { placardDraw(); } catch (_) { /* never break capture */ }
        }
        if (!mediaWs || mediaWs.readyState !== WebSocket.OPEN) return;
        const samples = e.inputBuffer.getChannelData(0);
        // Track signal level so we can tell (from server logs) whether the
        // captured meeting audio is real or all-zero/silent.
        let sum = 0;
        for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
        const rms = Math.sqrt(sum / samples.length);
        if (rms > captureMaxRms) captureMaxRms = rms;
        captureFrames++;
        // Remote-only level, measured independently of the mic so we can tell
        // whether the srcObject interception is actually delivering room audio.
        if (remoteAnalysers.length) {
            const n = remoteAnalysers[0].fftSize;
            if (!remoteRmsScratch || remoteRmsScratch.length !== n) {
                remoteRmsScratch = new Float32Array(n);
            }
            for (let a = 0; a < remoteAnalysers.length; a++) {
                remoteAnalysers[a].getFloatTimeDomainData(remoteRmsScratch);
                let rsum = 0;
                for (let i = 0; i < n; i++) rsum += remoteRmsScratch[i] * remoteRmsScratch[i];
                const rr = Math.sqrt(rsum / n);
                if (rr > remoteMaxRms) remoteMaxRms = rr;
            }
        }
        // Half-duplex gate: don't forward mic audio while Nuru is speaking, so
        // her own voice (from the Teams-client speaker) can't loop back as a
        // new question. Browser AEC can't cancel it (different app's output).
        const selfTalking = !FULL_DUPLEX
            && !!(audioCtx && audioCtx.currentTime < captureMutedUntil);
        // Privacy gate: Nuru taps the local mic directly, which is independent
        // of the Teams client's mute. But from Nuru's leg the human is a *remote*
        // participant, so we honour their Teams mute — if every human is muted,
        // nothing is legitimately being said to the meeting, so stop listening.
        const humanMuted = allHumansMuted();
        const muted = selfTalking || humanMuted;
        if (captureFrames % 25 === 0) {
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
                    selfTalking,
                    humanMuted,
                    parts: (call && call.remoteParticipants)
                        ? call.remoteParticipants.length : -1,
                    duplex: FULL_DUPLEX ? "full" : "half",
                    remoteStreams: (call && call.remoteAudioStreams)
                        ? call.remoteAudioStreams.length : 0,
                    wiredTracks: wiredRemoteTracks.size,
                    remoteMeters: remoteAnalysers.length,
                    remoteMaxRms: Number(remoteMaxRms.toFixed(5)),
                    remoteVia: Array.from(remoteWiredVia).join("+") || "none",
                    videoState,
                    videoChunks: avatarChunksIn,
                    avatarPic: avatarHasPicture,
                    avLead: Number(avLead.toFixed(3)),
                    avResyncs,
                    lead: TARGET_LEAD,
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
function attachRemoteMediaStream(ms, via) {
    try {
        if (!ms || typeof ms.getAudioTracks !== "function") return;
        const tracks = ms.getAudioTracks();
        if (!tracks.length) return;
        const id = tracks[0].id;
        if (wiredRemoteTracks.has(id)) return;
        // audioCtx is created in setupOutboundAudio() before join(), but a stream
        // arriving first must not be dropped on the floor.
        if (!audioCtx) { pendingRemoteStreams.push({ ms, via }); return; }
        wiredRemoteTracks.add(id);
        ensureCaptureNode();
        const src = audioCtx.createMediaStreamSource(ms);
        // Metered separately from the mic so the diagnostic is unambiguous.
        const analyser = audioCtx.createAnalyser();
        analyser.fftSize = 2048;
        src.connect(analyser);
        src.connect(captureNode);
        remoteAnalysers.push(analyser);
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

// ───────── live avatar face (MediaSource) ─────────
// The server runs the Voice Live session in avatar/websocket mode and relays the
// raw fragmented-MP4 stream here as {type:"video_data", delta:<base64>}. We play
// it MUTED in an offscreen <video> purely as a picture source: the answer AUDIO
// still arrives separately as decoded PCM16 on the same socket, so the whole
// turn-taking/barge-in/mute chain stays exactly where it already works. The
// <video> is then painted onto the same canvas that already feeds the outgoing
// ACS video tile, so the face replaces the placard without touching transport.
const FMP4_MIME_CODEC = 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"';

// Video has three independent ways to fail silently (startVideo rejecting,
// MediaSource refusing the codec, addSourceBuffer throwing) and every one of them
// used to land in console.warn — invisible to the operator running the joiner and
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
    // startPlacardVideo() runs immediately after openMediaSocket(), so the socket
    // is usually still CONNECTING when the first report fires. Queue rather than
    // drop: the error detail is the whole point, and capture_stats only carries
    // the bare state.
    pendingVideoReports.push({
        type: "video_status", state, detail: detail ? String(detail) : "",
    });
    flushVideoReports();
}
let avatarLiveVideo = false;      // server says the live stream is enabled
let avatarVideoEl = null;         // offscreen <video> fed by MediaSource
let avatarMediaSource = null;
let avatarSourceBuffer = null;
let avatarChunkQueue = [];
let avatarHasPicture = false;     // first decoded frame seen -> safe to paint
let avatarChunksIn = 0;           // fMP4 deltas received from the server
let appendFailed = false;         // report the first appendBuffer failure only
let avatarLastDrawMs = 0;         // last time the video actually advanced

function setupAvatarVideo() {
    if (avatarVideoEl) return;
    if (!("MediaSource" in window) || !MediaSource.isTypeSupported(FMP4_MIME_CODEC)) {
        reportVideo("unsupported", `MediaSource cannot play ${FMP4_MIME_CODEC}`);
        return;
    }
    const v = document.createElement("video");
    v.autoplay = true;
    v.playsInline = true;
    // Muted is essential: the answer audio reaches the call through the PCM path.
    // Letting this element play would double the voice and break the echo gate.
    v.muted = true;
    v.volume = 0;
    v.addEventListener("canplay", () => v.play().catch(() => {}));
    // The <video> reports decode failures here rather than by throwing, so a
    // stream MediaSource accepts but cannot actually decode would otherwise be
    // completely silent.
    v.addEventListener("error", () => {
        const err = v.error;
        reportVideo("failed", `video element: code=${err ? err.code : "?"} ${err && err.message ? err.message : ""}`);
    });
    v.addEventListener("loadeddata", () => { avatarHasPicture = true; reportVideo("face-live"); });

    avatarMediaSource = new MediaSource();
    v.src = URL.createObjectURL(avatarMediaSource);
    avatarMediaSource.addEventListener("sourceopen", () => {
        try {
            if (avatarMediaSource.readyState !== "open") return;
            avatarSourceBuffer = avatarMediaSource.addSourceBuffer(FMP4_MIME_CODEC);
            avatarSourceBuffer.addEventListener("updateend", drainAvatarQueue);
            drainAvatarQueue();
        } catch (e) {
            reportVideo("failed", `addSourceBuffer: ${e && e.message ? e.message : e}`);
        }
    });
    // Repaint the tile the instant a frame decodes, rather than resampling the
    // stream on a fixed timer. The avatar renders at ~25fps; a 15fps timer landed
    // between source frames, so some were shown twice and others skipped, and the
    // uneven cadence is what read as jerky/"robotic" motion. requestVideoFrameCallback
    // fires exactly once per decoded frame, so motion is 1:1 with the source.
    // The interval stays as a keep-alive for the placard and backgrounded tabs.
    pumpAvatarFrames(v);
    avatarVideoEl = v;
}

function pumpAvatarFrames(v) {
    // Prefer a DATA-driven clock over a timer. Measured live in-meeting: while the
    // tab is visible the tile repaints at 55fps against a 25fps source; the instant
    // it goes hidden, requestVideoFrameCallback stops dead (vFps 25 -> 0) and the
    // repaint collapses to the ~6fps audio-thread watchdog. The joiner tab normally
    // sits behind the Teams window, so that is the common case, not an edge case —
    // and lips that move 6 times a second look broken no matter what the audio
    // offset is. Reading decoded frames off the media pipeline is not a timer, so
    // it keeps delivering while hidden.
    let gotFrames = false;
    try {
        const cap = typeof v.captureStream === "function" ? v.captureStream() : null;
        const track = cap && cap.getVideoTracks ? cap.getVideoTracks()[0] : null;
        if (track && typeof window.MediaStreamTrackProcessor === "function") {
            const reader = new window.MediaStreamTrackProcessor({ track })
                .readable.getReader();
            framePumpVia = "frames";
            (async () => {
                for (;;) {
                    const { value, done } = await reader.read();
                    if (done) break;
                    try { value.close(); } catch (_) {}
                    if (avatarVideoEl !== v) break;
                    gotFrames = true;
                    rvfcCount += 1;
                    if (placardDraw) {
                        try { placardDraw(); } catch (_) { /* never break the pump */ }
                    }
                }
                try { reader.cancel(); } catch (_) {}
            })();
            // Don't trust it blindly: if nothing arrives, fall back rather than
            // leaving the tile with no clock at all.
            setTimeout(() => {
                if (!gotFrames && avatarVideoEl === v) {
                    reportVideo("pump-fallback", "no frames from MediaStreamTrackProcessor");
                    startRvfcPump(v);
                }
            }, 2500);
            return;
        }
    } catch (e) {
        reportVideo("pump-fallback", `trackprocessor: ${e && e.message ? e.message : e}`);
    }
    startRvfcPump(v);
}

function startRvfcPump(v) {
    if (typeof v.requestVideoFrameCallback !== "function") return;
    framePumpVia = framePumpVia === "frames" ? "frames+rvfc" : "rvfc";
    const step = () => {
        if (avatarVideoEl !== v) return; // torn down — let the loop die
        rvfcCount += 1;
        if (placardDraw) {
            try { placardDraw(); } catch (_) { /* never break the frame loop */ }
        }
        try { v.requestVideoFrameCallback(step); } catch (_) {}
    };
    try { v.requestVideoFrameCallback(step); } catch (_) {}
}

function handleAvatarChunk(base64Data) {
    if (!base64Data) return;
    avatarChunksIn += 1;
    if (avatarChunksIn === 1) reportVideo("chunks-arriving");
    if (!avatarVideoEl) setupAvatarVideo();
    if (!avatarVideoEl) return;
    try {
        const bin = atob(base64Data);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        avatarChunkQueue.push(bytes.buffer);
        drainAvatarQueue();
    } catch (e) {
        console.error("[acs-join] bad avatar chunk", e);
    }
}

function drainAvatarQueue() {
    const sb = avatarSourceBuffer;
    if (!sb || sb.updating) return;
    if (!avatarMediaSource || avatarMediaSource.readyState !== "open") return;
    const next = avatarChunkQueue.shift();
    if (!next) return;
    try {
        sb.appendBuffer(next);
    } catch (e) {
        // QuotaExceeded: drop what we've already played and retry once. The tile
        // is live video — old buffered media has no value.
        // A codec mismatch also surfaces here (we declare avc1.42E01E/Baseline;
        // isTypeSupported only checks the string, so a stream in a different
        // profile passes setup and fails on the first real append), so report the
        // first one rather than letting it repeat silently into a blank tile.
        if (!appendFailed) {
            appendFailed = true;
            reportVideo("failed", `appendBuffer: ${e && e.name ? e.name : ""} ${e && e.message ? e.message : e}`);
        }
        try {
            const v = avatarVideoEl;
            if (sb.buffered.length && v) {
                const end = Math.max(0, v.currentTime - 1);
                if (end > sb.buffered.start(0)) sb.remove(sb.buffered.start(0), end);
            }
        } catch (_) { /* best effort */ }
    }
}

// Barge-in: audio is flushed server-side, so jump the picture to the live edge
// instead of leaving the mouth moving through speech nobody will hear.
function skipAvatarToLiveEdge() {
    const v = avatarVideoEl;
    const sb = avatarSourceBuffer;
    if (!v || !sb) return;
    try {
        if (sb.buffered.length) {
            const end = sb.buffered.end(sb.buffered.length - 1);
            if (end - v.currentTime > 0.15) v.currentTime = end - 0.05;
        }
    } catch (_) { /* seeking a live buffer can throw; harmless */ }
}

function teardownAvatarVideo() {
    try { if (avatarVideoEl) { avatarVideoEl.pause(); avatarVideoEl.src = ""; } } catch (_) {}
    avatarVideoEl = null;
    avatarMediaSource = null;
    avatarSourceBuffer = null;
    avatarChunkQueue = [];
    avatarHasPicture = false;
    avatarChunksIn = 0;
    appendFailed = false;
    videoState = "off";
    avatarLastDrawMs = 0;
}

// Paint the current avatar frame onto the tile canvas. Returns false when there
// is nothing live to show, so the caller falls back to the branded placard.
//
// "Live" means the <video> has a picture AND its clock is still advancing: when a
// turn ends the stream simply stops, and a frozen face staring at the room looks
// broken. After AVATAR_IDLE_MS with no advance we fall back to the placard, which
// doubles as the "listening" state.
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
        return false; // stream idle between turns -> show the placard
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
        if (!thinkingSince || performance.now() - thinkingSince < THINKING_SHOW_AFTER_MS) return;
        const label = "thinking";
        ctx.font = "500 20px -apple-system, 'Segoe UI', system-ui, sans-serif";
        const w = ctx.measureText(label).width + 62;
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
        ctx.fillText(label, x + 52, y + 24);
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
        // Status pulse (and frame keep-alive). On the placard the status line
        // itself carries the cue, so no separate badge is drawn here.
        const thinking = thinkingSince
            && performance.now() - thinkingSince >= THINKING_SHOW_AFTER_MS;
        const status = thinking ? "thinking" : "listening";
        const r = 6 + 2 * Math.sin(t * (thinking ? 6 : 3));
        ctx.beginPath();
        ctx.fillStyle = thinking ? "#f59e0b" : "#22c55e";
        ctx.arc(canvas.width / 2 - 64, H * 0.823, r, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "rgba(255,255,255,.75)";
        ctx.font = "400 18px -apple-system, 'Segoe UI', system-ui, sans-serif";
        ctx.textAlign = "left";
        ctx.fillText(status, canvas.width / 2 - 48, H * 0.84);
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
    // These outlived the audioCtx before: stale analysers from a closed context
    // stay readable and keep inflating remoteMeters while always reporting 0,
    // which would make the remote-audio diagnostic lie after a rejoin.
    remoteAnalysers.length = 0;
    remoteWiredVia.clear();
    pendingRemoteStreams.length = 0;
    remoteMaxRms = 0;
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
