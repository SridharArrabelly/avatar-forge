/**
 * Avatar Forge - Client-side JavaScript
 * Handles audio capture (AudioWorklet 24kHz PCM16), WebSocket communication,
 * WebRTC avatar video, and UI state management.
 */

// ===== State =====
let ws = null;
let audioContext = null;
let workletNode = null;
let mediaStream = null;
let playbackContext = null;
let playbackBufferQueue = [];
let nextPlaybackTime = 0;
let isConnected = false;
let isConnecting = false;
let isRecording = false;
let audioChunksSent = 0;
let isDeveloperMode = false;
let avatarEnabled = false;
// True from the moment connectSession() starts until the avatar video is ready
// (or the session ends). Lets us show the avatar frame + loading placeholder
// immediately instead of waiting for session_started, smoothing the startup.
let avatarConnecting = false;
let pendingAvatarEnabled = false;
let avatarLoadingHideTimer = null;
let micRevealTimer = null;
// Teams-only safety net: when embedded in the Teams webview the app auto-connects
// in kiosk mode, and if the avatar video never reveals (e.g. WebRTC/media blocked
// by the host) the user is left staring at a silent dark stage with no feedback.
// This watchdog converts that into a visible, tappable "tap to restart" state.
// It is armed ONLY inside Teams, so the standalone web experience is unchanged.
let avatarRevealWatchdog = null;
// Avatar "thinking" indicator: shown while the agent works (response_created ->
// first token) so the user isn't staring at a silent face during the 2-3s
// grounding gap. Purely visual — never touches the audio/avatar pipeline.
let thinkingShowTimer = null;
let thinkingRotateTimer = null;
let thinkingSlowTimer = null;
let thinkingMaxTimer = null;
let thinkingActive = false;
let thinkingCaptionIndex = 0;
// Monotonic generation token. Bumped every time a new turn arms the indicator,
// so a late event from a previous (e.g. cancelled) response can't show or
// cancel the current turn's pill.
let thinkingGen = 0;
const THINKING_CAPTIONS = [
    'Looking through the records…',
    'Checking the latest information…',
    'Pulling the details together…',
];
const THINKING_SLOW_CAPTION = 'Just a moment — getting you a reliable answer…';
const THINKING_SHOW_DELAY_MS = 700;
const THINKING_ROTATE_MS = 2200;
const THINKING_SLOW_MS = 3500;
// Hard ceiling so the pill can never get stuck if response_done never arrives.
const THINKING_MAX_MS = 25000;
let peerConnection = null;
let avatarVideoElement = null;
let avatarAudioElement = null;
// Handler installed when a browser autoplay policy refuses play(). The next user
// gesture retries, so a blocked first visit recovers without a page reload. Held
// as a reference (not a flag) so teardown can detach it.
let avatarGestureRetry = null;
const AVATAR_GESTURE_EVENTS = ['pointerdown', 'keydown', 'touchstart'];
let isSpeaking = false;
let avatarOutputMode = 'webrtc';
let cachedIceServers = null;
let peerConnectionQueue = [];

// ===== Avatar UX features (captions, speaking glow, suggested prompts) =====
// Populated from /api/config.defaults in fetchServerConfig(); defaults here are
// safe fallbacks if config is unavailable.
let captionsEnabled = false;
let captionsShowUser = false;
let suggestedPromptsEnabled = true;
// Text composer (bottom-center of the avatar stage) in normal mode. Shown on
// the standalone web app; ALWAYS hidden inside the Microsoft Teams client (the
// bot chat tab has Teams' native compose box, and the avatar tab is voice-first
// — type via the chat tab or, in a call, the meeting chat with an @mention).
// ENABLE_TEXT_INPUT (/api/config -> enableTextInput) is an optional web-only
// override; isEmbeddedInTeams() can never be overridden on.
let textInputEnabled = false;
// Stop-speaking button (next to the mic) in normal mode. Env-gated via
// ENABLE_STOP_BUTTON (/api/config defaults -> enableStopButton). Shown only
// while the avatar is actually talking.
let stopButtonEnabled = true;
// Normal-mode connection/permission status surfaced on #stageStatus. One of:
// null, 'reconnecting', 'ended', 'mic-blocked', 'mic-missing', 'error'.
let connectionState = null;
// Set true right before a user-initiated stop so the socket's later onclose
// doesn't mistake an intentional disconnect for an unexpected drop.
let intentionalDisconnect = false;
let onboardingHintText = 'Tap the mic to ask me anything';
let avatarTaglineText = '';
let avatarDisplayNameText = '';
// Server-resolved persona name (/api/config -> assistantName): AVATAR_DISPLAY_NAME
// if set, else the active avatar model's friendly name. Never empty. Used as the
// stage-label fallback so the label can never come out blank, and so it agrees
// with what the agent calls itself. See backend/avatar_identity.py.
let assistantNameText = 'Avatar';
let suggestedPrompts = [];
// Onboarding lifecycle: dismissed permanently after the first real user action;
// hidden temporarily while the avatar speaks (e.g. the proactive greeting).
let onboardingDismissed = false;
// Speaking glow: driven by actual audio playback, not the response lifecycle
// (buffered audio keeps playing after response_done). A per-chunk watchdog
// guarantees the glow can never get stuck on.
let avatarSpeaking = false;
let speakingWatchdogTimer = null;
const SPEAKING_WATCHDOG_MS = 4000;
let captionClearTimer = null;

// WebRTC avatar speaking detection. The avatar's audio rides a WebRTC media
// track (not _playAudioPCM16), and the response lifecycle (response_done) fires
// when generation finishes — long before the avatar stops talking. So we detect
// REAL speech from two playback-tied signals, either of which keeps the glow +
// caption alive for the full spoken duration:
//   1. The avatar service's data-channel speaking events
//      (EVENT_TYPE_SWITCH_TO_SPEAKING / EVENT_TYPE_SWITCH_TO_IDLE).
//   2. An AnalyserNode tapping the live remote audio track (real audio energy).
// The watchdog remains the final backstop so the glow can never stick on.
let avatarAudioCtx = null;
let avatarAudioAnalyser = null;
let avatarAudioData = null;
let avatarAudioRafId = null;
let avatarLastAudibleTs = 0;
// Latched by the data-channel SPEAKING/IDLE events. While a speaking turn is
// active we keep the glow lit through short between-sentence pauses (the events
// bracket the whole turn, so we don't blink on every silent gap).
let avatarSpeechTurnActive = false;
// Once the analyser has actually observed audio energy we trust it to drive the
// OFF transition too. Until proven it only confirms ON, so a silent/blocked
// AnalyserNode (e.g. an autoplay-suspended context) can never wrongly kill the
// glow.
let avatarAnalyserProven = false;
const AVATAR_SPEAK_RMS_ON = 0.015;
const AVATAR_SPEAK_HANGOVER_MS = 600;

// Volume animation state
let analyserNode = null;
let analyserDataArray = null;
let micAnalyserNode = null;
let micAnalyserDataArray = null;
let recordAnimationFrameId = null;
let playChunkAnimationFrameId = null;

// WebSocket video playback (MediaSource Extensions)
let mediaSource = null;
let sourceBuffer = null;
let videoChunksQueue = [];
let pendingWsVideoElement = null;

const clientId = 'client-' + Math.random().toString(36).substr(2, 9);

// ===== DOM Ready =====
document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    setupUIBindings();
    updateConditionalFields();
    updateControlStates();
    fetchServerConfig();
});

// ===== Theme =====
// Lightweight theme hook. 'system' (default) removes the data-theme attribute so
// the `prefers-color-scheme` media query in style.css decides; 'light'/'dark'
// set an explicit attribute that overrides the OS preference. Teams theme wiring
// (issue #28) can later call applyTheme() directly — no SDK is loaded here.
function applyTheme(theme) {
    const root = document.documentElement;
    if (theme === 'light' || theme === 'dark') {
        root.setAttribute('data-theme', theme);
    } else {
        theme = 'system';
        root.removeAttribute('data-theme');
    }
    try { localStorage.setItem('avatarForgeTheme', theme); } catch (e) {}
    return theme;
}

function initTheme() {
    let stored = null;
    try { stored = localStorage.getItem('avatarForgeTheme'); } catch (e) {}
    applyTheme(stored || 'system');
}

// Construct (and immediately close) a throwaway RTCPeerConnection at page load
// so the browser loads/initializes its WebRTC native code, codec backends, and
// JS-to-native bridges before the user clicks Connect. Real ICE candidates are
// NOT pre-gathered here (we don't have the Azure ICE servers yet, and using
// public STUN as a stand-in proved unreliable — see the note above
// preparePeerConnection). The only thing this saves is the first-time
// engine-warm cost, which is small but free.
// Construct (and immediately close) a throwaway RTCPeerConnection so the browser
// loads/initializes its WebRTC native code, codec backends, and JS-to-native
// bridges before we build the real peer connection. Real ICE candidates are NOT
// pre-gathered here (we don't have the Azure ICE servers yet, and using public
// STUN as a stand-in proved unreliable — see the note above preparePeerConnection).
//
// This is expensive and SYNCHRONOUS: measured at ~1.2s of main-thread block on a
// cold browser profile, after which the real peer connections construct in ~1ms.
// So it is a fixed one-time cost that has to be paid somewhere, and the only
// thing that matters is WHERE. It used to run at DOMContentLoaded, where it
// delayed the /api/config response callback and therefore everything downstream.
// Deferring it to requestIdleCallback did NOT help — the thread is idle exactly
// while the config request is in flight, so it ran at the same moment.
//
// It is now called once the voice WebSocket is connecting, so it overlaps a wait
// that is purely network-bound (~0.6-3s) and costs nothing on the main thread.
let webrtcEngineWarmed = false;

function warmWebRTCEngine() {
    if (webrtcEngineWarmed) return;
    webrtcEngineWarmed = true;
    if (typeof RTCPeerConnection === 'undefined') return;
    try {
        const warm = new RTCPeerConnection({});
        // Closing immediately is fine — we just wanted the constructor to run.
        warm.close();
    } catch (e) {
        // Ignore — older browsers / restrictive policies. Not fatal.
    }
}

// ===== Server Config =====
function applyServerDefaults(defaults) {
    if (!defaults) return;
    for (const [id, val] of Object.entries(defaults)) {
        const el = document.getElementById(id);
        if (!el) continue;
        if (el.type === 'checkbox') {
            el.checked = !!val;
        } else {
            const stringValue = String(val);
            if (el.tagName === 'SELECT' && stringValue && !Array.from(el.options).some(option => option.value === stringValue)) {
                el.add(new Option(stringValue, stringValue));
            }
            el.value = stringValue;
        }
        // Notify listeners (range displays, conditional fields).
        el.dispatchEvent(new Event('change', { bubbles: true }));
        if (el.type === 'range') el.dispatchEvent(new Event('input', { bubbles: true }));
    }
}

// True when the SPA is embedded in the Microsoft Teams client (tab host now,
// in-call later). Self-contained (mirrors teams.js) so it never depends on
// teams.js load order. In Teams we hide the in-app text composer entirely; the
// composer only ever appears on the standalone web app.
function isEmbeddedInTeams() {
    try {
        const params = new URLSearchParams(window.location.search);
        if (params.has('inTeams')) return true;
        if (window.parent && window.parent !== window) return true;
        if (window.nativeInterface) return true; // Teams desktop webview marker
    } catch (e) {}
    return false;
}

async function fetchServerConfig() {
    try {
        const resp = await fetch('/api/config');
        const config = await resp.json();

        // Apply env-driven defaults to all matching controls.
        applyServerDefaults(config.defaults);

        // Avatar UX flags have no matching DOM controls, so read them directly.
        // Nullish coalescing (not ||) so an env value of false is honored.
        const d = config.defaults || {};
        captionsEnabled = d.enableCaptions ?? false;
        captionsShowUser = d.captionsShowUser ?? false;
        // Composer defaults on for the web app, but is always suppressed inside
        // Teams (host wins over the env override).
        textInputEnabled = (d.enableTextInput ?? true) && !isEmbeddedInTeams();
        stopButtonEnabled = d.enableStopButton ?? true;
        suggestedPromptsEnabled = d.enableSuggestedPrompts ?? true;
        // Hint is modality-aware and follows the EFFECTIVE composer state above
        // (so Teams, where the composer is hidden, never says "…or type…"). An
        // explicit ONBOARDING_HINT from the backend always wins.
        const explicitHint = typeof d.onboardingHint === 'string' ? d.onboardingHint.trim() : '';
        onboardingHintText = explicitHint || (textInputEnabled
            ? 'Tap the mic or type to ask me anything'
            : 'Tap the mic to ask me anything');
        avatarTaglineText = d.avatarTagline ?? avatarTaglineText;
        avatarDisplayNameText = d.avatarDisplayName ?? avatarDisplayNameText;
        assistantNameText = d.assistantName || assistantNameText;
        const taglineEl = document.getElementById('avatarTagline');
        if (taglineEl) taglineEl.textContent = avatarTaglineText || '';
        suggestedPrompts = Array.isArray(d.suggestedPrompts) ? d.suggestedPrompts : [];
        buildOnboarding();

        // Back-compat: top-level voice (older /api/config shape).
        if (config.voice) {
            const v = document.getElementById('voiceName');
            if (v && !config.defaults?.voiceName) v.value = config.voice;
        }

        // Refresh any cascaded visibility now that values are in place.
        if (typeof updateConditionalFields === 'function') updateConditionalFields();

        // Sidebar is hidden by default in markup to avoid a flash on load.
        // Reveal it only when developer mode is on; otherwise leave it hidden
        // and tidy auxiliary dev-only controls, then auto-connect.
        if (config.developerMode === true) {
            const sidebar = document.getElementById('sidebar');
            if (sidebar) sidebar.hidden = false;
            const headerBar = document.getElementById('headerBar');
            if (headerBar) headerBar.hidden = false;
        } else {
            const devToggle = document.getElementById('developerMode');
            if (devToggle) {
                const label = devToggle.closest('label');
                if (label) label.style.display = 'none';
            }
            document.querySelectorAll('.mobile-menu').forEach(el => { el.style.display = 'none'; });
            const clearBtn = document.getElementById('clearChatBtn');
            if (clearBtn) clearBtn.style.display = 'none';
            try { await connectSession(); }
            catch (err) { console.error('Auto-connect failed', err); }
        }
    } catch (e) {
        console.warn('No server config available, using defaults:', e?.message || e);
    }
}

// ===== UI Bindings =====
function setupUIBindings() {
    // Voice type change
    document.getElementById('voiceType').addEventListener('change', updateConditionalFields);
    // Voice name change
    document.getElementById('voiceName').addEventListener('change', updateConditionalFields);
    // Avatar enabled
    document.getElementById('avatarEnabled').addEventListener('change', updateConditionalFields);
    // Photo avatar
    document.getElementById('isPhotoAvatar').addEventListener('change', updateConditionalFields);
    // Custom avatar
    document.getElementById('isCustomAvatar').addEventListener('change', updateConditionalFields);
    // Developer mode
    document.getElementById('developerMode').addEventListener('change', (e) => {
        isDeveloperMode = e.target.checked;
        updateDeveloperModeLayout();
    });
    // Turn detection type
    document.getElementById('turnDetectionType').addEventListener('change', updateConditionalFields);
    // SR Model
    document.getElementById('srModel').addEventListener('change', updateConditionalFields);

    // Range sliders - display values
    setupRangeDisplay('temperature', 'tempValue', v => v);
    setupRangeDisplay('voiceTemperature', 'voiceTempValue', v => v);
    setupRangeDisplay('voiceSpeed', 'voiceSpeedValue', v => v + '%');
    setupRangeDisplay('sceneZoom', 'sceneZoomLabel', v => 'Zoom: ' + v + '%');
    setupRangeDisplay('scenePositionX', 'scenePositionXLabel', v => 'Position X: ' + v + '%');
    setupRangeDisplay('scenePositionY', 'scenePositionYLabel', v => 'Position Y: ' + v + '%');
    setupRangeDisplay('sceneRotationX', 'sceneRotationXLabel', v => 'Rotation X: ' + v + ' deg');
    setupRangeDisplay('sceneRotationY', 'sceneRotationYLabel', v => 'Rotation Y: ' + v + ' deg');
    setupRangeDisplay('sceneRotationZ', 'sceneRotationZLabel', v => 'Rotation Z: ' + v + ' deg');
    setupRangeDisplay('sceneAmplitude', 'sceneAmplitudeLabel', v => 'Amplitude: ' + v + '%');

    // Scene sliders: send real-time updates when connected
    const sceneSliders = ['sceneZoom', 'scenePositionX', 'scenePositionY',
        'sceneRotationX', 'sceneRotationY', 'sceneRotationZ', 'sceneAmplitude'];
    sceneSliders.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', throttledUpdateAvatarScene);
    });

    // Accordion behavior: only one settings group open at a time
    const settingsGroups = document.querySelectorAll('.sidebar .settings-group');
    settingsGroups.forEach(group => {
        group.addEventListener('toggle', () => {
            if (group.open) {
                settingsGroups.forEach(other => {
                    if (other !== group && other.open) {
                        other.removeAttribute('open');
                    }
                });
            }
        });
    });
}

function setupRangeDisplay(sliderId, displayId, formatter) {
    const slider = document.getElementById(sliderId);
    const display = document.getElementById(displayId);
    if (slider && display) {
        slider.addEventListener('input', () => {
            display.textContent = formatter(slider.value);
        });
    }
}

// ===== Photo Avatar Scene Update =====
let lastSceneUpdate = 0;
const SCENE_THROTTLE_MS = 50;

function throttledUpdateAvatarScene() {
    const now = Date.now();
    if (now - lastSceneUpdate < SCENE_THROTTLE_MS) return;
    lastSceneUpdate = now;
    updateAvatarScene();
}

function updateAvatarScene() {
    if (!isConnected || !ws || ws.readyState !== WebSocket.OPEN) return;
    if (!document.getElementById('isPhotoAvatar')?.checked) return;
    if (!document.getElementById('avatarEnabled')?.checked) return;

    const isCustom = document.getElementById('isCustomAvatar')?.checked || false;
    const customName = document.getElementById('customAvatarName')?.value || '';
    const avatarName = (isCustom && customName)
        ? customName
        : document.getElementById('photoAvatarName')?.value || 'Anika';
    const parts = avatarName.split('-');
    const character = parts[0].toLowerCase();
    const style = parts.slice(1).join('-') || undefined;

    const scene = {
        zoom: parseInt(document.getElementById('sceneZoom').value) / 100,
        position_x: parseInt(document.getElementById('scenePositionX').value) / 100,
        position_y: parseInt(document.getElementById('scenePositionY').value) / 100,
        rotation_x: parseInt(document.getElementById('sceneRotationX').value) * Math.PI / 180,
        rotation_y: parseInt(document.getElementById('sceneRotationY').value) * Math.PI / 180,
        rotation_z: parseInt(document.getElementById('sceneRotationZ').value) * Math.PI / 180,
        amplitude: parseInt(document.getElementById('sceneAmplitude').value) / 100,
    };

    const avatar = {
        type: 'photo-avatar',
        model: 'vasa-1',
        character: character,
        scene: scene,
    };
    if (isCustom) {
        avatar.customized = true;
    } else if (style) {
        avatar.style = style;
    }

    ws.send(JSON.stringify({
        type: 'update_scene',
        avatar: avatar,
    }));
}

// ===== Conditional Field Visibility =====
function updateConditionalFields() {
    const voiceType = document.getElementById('voiceType').value;
    const voiceName = document.getElementById('voiceName').value;
    const avatarEnabled = document.getElementById('avatarEnabled').checked;
    const isPhotoAvatar = document.getElementById('isPhotoAvatar').checked;
    const isCustomAvatar = document.getElementById('isCustomAvatar').checked;
    const turnDetectionType = document.getElementById('turnDetectionType').value;

    // Recognition language is always shown. MAI Transcribe may treat a single-locale
    // hint as advisory (it's fundamentally multilingual auto-detect), but the API
    // accepts the field and cascaded models honor it strictly. Hiding it would
    // silently strip an English-only lock when users want one.
    show('recognitionLanguageField', true);

    // Filler words (semantic VAD)
    show('fillerWordsField', turnDetectionType === 'azure_semantic_vad');

    // Voice type variants
    show('standardVoiceField', voiceType === 'standard');
    show('customVoiceFields', voiceType === 'custom');
    show('personalVoiceFields', voiceType === 'personal');

    // Voice temperature (DragonHD or personal voice)
    const isDragonHD = voiceName && voiceName.includes('DragonHD');
    const isPersonal = voiceType === 'personal';
    show('voiceTempField', isDragonHD || isPersonal);

    // Avatar settings
    show('avatarSettings', avatarEnabled);
    show('standardAvatarField', !isPhotoAvatar && !isCustomAvatar);
    show('photoAvatarField', isPhotoAvatar && !isCustomAvatar);
    show('customAvatarField', isCustomAvatar);
    show('photoAvatarSceneSettings', isPhotoAvatar);
}

function show(id, visible) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('hidden', !visible);
}

// ===== Sidebar Toggle (mobile) =====
function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('open');
}

// ===== Chat =====
function addMessage(role, text, isDev = false, suppressToast = false) {
    if (isDev && !isDeveloperMode) return;
    const messagesEl = document.getElementById('messages');
    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${isDev ? 'dev' : role}`;

    if (!isDev) {
        const roleSpan = document.createElement('div');
        roleSpan.className = 'message-role';
        roleSpan.textContent = role === 'user' ? 'You' : role === 'assistant' ? 'Assistant' : 'System';
        msgDiv.appendChild(roleSpan);
    }

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    contentDiv.textContent = text;
    msgDiv.appendChild(contentDiv);

    messagesEl.appendChild(msgDiv);
    scrollChatToBottom();
    updateClearChatButton();

    if (role === 'system' && text && !suppressToast) {
        let type = 'info';
        const lowerText = text.toLowerCase();
        if (lowerText.includes('error') || lowerText.includes('failed') || lowerText.includes('denied')) {
            type = 'error';
        } else if (lowerText.includes('dropped') || lowerText.includes('disconnected') || lowerText.includes('closed') || lowerText.includes('websocket error')) {
            type = 'warning';
        } else if (lowerText.includes('started') || lowerText.includes('success')) {
            type = 'success';
        }
        showToast(text, type);
    }

    return contentDiv;
}

function updateLastAssistantMessage(text) {
    const messages = document.querySelectorAll('.message.assistant .message-content');
    if (messages.length > 0) {
        messages[messages.length - 1].textContent = text;
        scrollChatToBottom();
    }
}

// Scroll-to-bottom is called once per streaming transcript token. Reading
// scrollHeight right after writing textContent forces a synchronous layout, so
// doing it per token stalls the main thread on every delta. Both the chat log
// and the caption band batch their scroll into a single rAF flush instead —
// visually identical (the user can't see sub-frame scroll states) but at most
// one forced layout per frame regardless of token rate.
let _scrollRafId = null;
let _scrollChatPending = false;
let _scrollCaptionPending = false;

function _flushScrolls() {
    _scrollRafId = null;
    if (_scrollChatPending) {
        _scrollChatPending = false;
        const chatArea = document.getElementById('chatArea');
        if (chatArea) chatArea.scrollTop = chatArea.scrollHeight;
    }
    if (_scrollCaptionPending) {
        _scrollCaptionPending = false;
        const caption = document.getElementById('avatarCaption');
        if (caption) caption.scrollTop = caption.scrollHeight;
    }
}

function _scheduleScrollFlush() {
    if (_scrollRafId === null) _scrollRafId = requestAnimationFrame(_flushScrolls);
}

function scrollChatToBottom() {
    _scrollChatPending = true;
    _scheduleScrollFlush();
}

function clearChat() {
    const messages = document.getElementById('messages');
    if (messages.children.length === 0) return;
    messages.innerHTML = '';
    updateClearChatButton();
}

function updateClearChatButton() {
    const btn = document.getElementById('clearChatBtn');
    const messages = document.getElementById('messages');
    if (!btn || !messages) return;
    const hasMessages = messages.children.length > 0;
    btn.disabled = !hasMessages;
    btn.style.opacity = hasMessages ? '' : '0.5';
}

// ===== Gather Config =====
function gatherConfig() {
    const voiceType = document.getElementById('voiceType').value;
    const isPhotoAvatar = document.getElementById('isPhotoAvatar').checked;
    const isCustomAvatar = document.getElementById('isCustomAvatar').checked;

    // Custom wins when named; an empty name falls through so it cannot blank the character.
    const customAvatarName = document.getElementById('customAvatarName').value;
    const selectedAvatarName = isCustomAvatar && customAvatarName
        ? customAvatarName
        : isPhotoAvatar
            ? document.getElementById('photoAvatarName').value
            : document.getElementById('avatarName').value;

    const voiceSpeed = parseFloat(document.getElementById('voiceSpeed').value) / 100;

    const config = {
        voiceType: voiceType,
        voiceName: document.getElementById('voiceName').value,
        voiceSpeed: voiceSpeed,
        voiceTemperature: parseFloat(document.getElementById('voiceTemperature').value),
        voiceDeploymentId: document.getElementById('voiceDeploymentId').value,
        customVoiceName: document.getElementById('customVoiceName').value,
        personalVoiceName: document.getElementById('personalVoiceName').value,
        personalVoiceModel: document.getElementById('personalVoiceModel').value,
        avatarEnabled: document.getElementById('avatarEnabled').checked,
        isPhotoAvatar: isPhotoAvatar,
        isCustomAvatar: isCustomAvatar,
        avatarName: selectedAvatarName,
        avatarOutputMode: document.getElementById('avatarOutputMode').value,
        avatarBackgroundImageUrl: document.getElementById('avatarBackgroundImageUrl').value,
        useNS: document.getElementById('useNS').checked,
        useEC: document.getElementById('useEC').checked,
        turnDetectionType: document.getElementById('turnDetectionType').value,
        turnDetectionSilenceMs: parseInt(document.getElementById('turnDetectionSilenceMs').value),
        enableBargeIn: document.getElementById('enableBargeIn').checked,
        removeFillerWords: document.getElementById('removeFillerWords').checked,
        srModel: document.getElementById('srModel').value,
        recognitionLanguage: document.getElementById('recognitionLanguage').value,
        eouDetectionType: document.getElementById('eouDetectionType').value,
        enableProactive: document.getElementById('enableProactive').checked,
    };

    // Voice Live binding override. Server-side this is honoured only in
    // DEVELOPER_MODE; sending it otherwise is harmless (the handler ignores
    // it and uses the deployment default). Each websocket opens its own Voice
    // Live connection, so two browser tabs can run different bindings at once
    // — which is the whole point: it makes agent-vs-model an A/B you can run
    // side by side instead of a redeploy.
    const bindingEl = document.getElementById('voiceBinding');
    if (bindingEl && bindingEl.value) {
        config.voiceBinding = bindingEl.value;
    }

    // Photo avatar scene settings
    if (isPhotoAvatar) {
        config.photoScene = {
            zoom: parseInt(document.getElementById('sceneZoom').value),
            positionX: parseInt(document.getElementById('scenePositionX').value),
            positionY: parseInt(document.getElementById('scenePositionY').value),
            rotationX: parseInt(document.getElementById('sceneRotationX').value),
            rotationY: parseInt(document.getElementById('sceneRotationY').value),
            rotationZ: parseInt(document.getElementById('sceneRotationZ').value),
            amplitude: parseInt(document.getElementById('sceneAmplitude').value),
        };
    }

    return config;
}

// ===== Connection =====
async function toggleConnection() {
    if (isConnecting) return;
    if (isConnected) {
        await disconnect();
    } else {
        await connectSession();
    }
}

async function connectSession() {
    setConnecting(true);
    // Fresh session: clear any prior status pill and reset the intentional-stop
    // guard so a later unexpected drop is correctly surfaced.
    intentionalDisconnect = false;
    setConnectionState(null);
    // Logged to the (dev-only) chat as a status line, but no toast — the avatar
    // spinner already signals "connecting", so the top-right toast was redundant.
    addMessage('system', 'Session started, click on the mic button to start conversation! debug id: connecting...', false, true);

    // Reveal the avatar frame + a branded loading placeholder right away (t=0)
    // so the final layout is in place immediately. Without this the screen stays
    // blank for ~3s (Voice Live handshake) and then the empty avatar box, name and
    // mic all pop in at once — the staggered reveal that feels awkward.
    const wantAvatar = document.getElementById('avatarEnabled')?.checked || false;
    avatarConnecting = wantAvatar;
    pendingAvatarEnabled = wantAvatar;
    if (wantAvatar) {
        // Pin the output mode now so the ice_servers handler (which arrives before
        // session_started) routes webrtc vs websocket correctly.
        avatarOutputMode = document.getElementById('avatarOutputMode')?.value || 'webrtc';
        const isPhotoAvatarSession = document.getElementById('isPhotoAvatar')?.checked || false;
        const avatarContainer = document.getElementById('avatarVideoContainer');
        if (avatarContainer) avatarContainer.classList.toggle('photo-avatar', isPhotoAvatarSession);
        showAvatarLoading('Connecting…');
        updateDeveloperModeLayout();
        armAvatarRevealWatchdog();
    }

    try {
        // Open WebSocket to Python backend
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${protocol}//${location.host}/ws/${clientId}`);
        ws.binaryType = 'arraybuffer';

        ws.onopen = () => {
            const config = gatherConfig();
            ws.send(JSON.stringify({ type: 'start_session', config }));
        };

        ws.onmessage = (event) => {
            // Binary frames = raw PCM16 audio chunks (server→client hot path).
            if (event.data instanceof ArrayBuffer) {
                handleAudioBinary(event.data);
                return;
            }
            const msg = JSON.parse(event.data);
            handleServerMessage(msg);
        };

        ws.onerror = (err) => {
            console.error('WebSocket error', err);
            notifySystem('Connection error', 'error');
            setConnecting(false);
        };

        ws.onclose = () => {
            console.log('WebSocket closed');
            // Distinguish a user-initiated stop from an unexpected drop. An
            // unexpected drop while we believed we were connected/connecting is
            // surfaced as a tap-to-restart state (the chat line is invisible in
            // production mode).
            const wasIntentional = intentionalDisconnect;
            const wasLive = isConnected || isConnecting;
            if (isConnected) {
                addMessage('system', 'Disconnected');
            }
            handleDisconnect();
            if (!wasIntentional && wasLive) {
                setConnectionState('ended');
            }
            intentionalDisconnect = false;
        };

        // Warm the WebRTC engine while the socket handshake and the Voice Live
        // session setup are in flight. Both are network-bound, so this ~1.2s of
        // one-time main-thread work is absorbed by a wait we were spending anyway
        // — and by the time ice_servers arrives the real peer connection builds
        // in ~1ms. See warmWebRTCEngine() for why earlier placements were worse.
        warmWebRTCEngine();

    } catch (err) {
        console.error('Connection error', err);
        notifySystem('Failed to connect: ' + err.message, 'error');
        setConnecting(false);
        setConnectionState('error');
    }
}

async function disconnect() {
    intentionalDisconnect = true;
    setConnectionState(null);
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'stop_session' }));
    }
    handleDisconnect();
}

function handleDisconnect() {
    isConnected = false;
    isConnecting = false;
    isRecording = false;
    audioChunksSent = 0;
    avatarEnabled = false;
    avatarConnecting = false;
    pendingAvatarEnabled = false;
    clearAvatarLoading();
    clearAvatarRevealWatchdog();

    stopAudioCapture();
    stopAudioPlayback();
    cleanupWebRTC();
    cleanupWebSocketVideo();
    updateSoundWaveAnimation();

    // Prepare next peer connection for faster reconnection
    if (cachedIceServers) {
        preparePeerConnection(cachedIceServers);
    }

    if (ws) {
        try { ws.close(); } catch (e) {}
        ws = null;
    }

    const lbl = document.getElementById('avatarNameLabel');
    if (lbl) lbl.textContent = '';
    clearTimeout(micRevealTimer);
    micRevealTimer = null;
    stopThinking();
    setAvatarSpeaking(false);
    clearAvatarCaption();
    document.getElementById('recordContainer')?.classList.add('hidden');

    updateConnectionUI();
    updateDeveloperModeLayout();
}

// ===== Handle Server Messages =====
function handleServerMessage(msg) {
    const type = msg.type;

    switch (type) {
        case 'session_started':
            onSessionStarted(msg);
            break;
        case 'session_error':
            notifySystem('Error: ' + (msg.error || 'Unknown error'), 'error');
            setConnecting(false);
            avatarConnecting = false;
            pendingAvatarEnabled = false;
            clearAvatarLoading();
            setConnectionState('error');
            updateDeveloperModeLayout();
            break;
        case 'ice_servers':
            // Only setup WebRTC when avatar output mode is webrtc
            if (avatarOutputMode === 'webrtc') {
                setupWebRTC(msg.iceServers);
            }
            break;
        case 'avatar_sdp_answer':
            handleAvatarSdpAnswer(msg.serverSdp);
            break;
        case 'audio_data':
            handleAudioDelta(msg.data);
            break;
        case 'transcript_done':
            if (msg.role === 'user') {
                // A real (non-empty) user turn permanently retires the onboarding hint.
                if ((msg.transcript || '').trim()) {
                    dismissOnboarding();
                    if (captionsShowUser) setAvatarCaption(msg.transcript, 'user');
                }
                // Update existing placeholder by itemId, or add new message
                const itemId = msg.itemId;
                if (itemId) {
                    const existing = document.querySelector(`.message.user[data-item-id="${itemId}"] .message-content`);
                    if (existing) {
                        existing.textContent = msg.transcript;
                        scrollChatToBottom();
                        break;
                    }
                }
                addMessage('user', msg.transcript);
            } else if (msg.role === 'assistant') {
                // Finalize the streaming assistant message (don't create a new one)
                if (msg.transcript) {
                    // Caption fallback: ensures the band is populated even if only
                    // a final transcript arrived (no streaming deltas).
                    setAvatarCaption(msg.transcript, 'assistant');
                    const assistantMsgs = document.querySelectorAll('.message.assistant .message-content');
                    if (assistantMsgs.length > 0) {
                        assistantMsgs[assistantMsgs.length - 1].textContent = msg.transcript;
                    }
                    pendingAssistantText = '';
                }
            }
            break;
        case 'transcript_delta':
            if (msg.role === 'assistant') {
                onAssistantDelta(msg.delta);
            }
            break;
        case 'text_delta':
            onAssistantDelta(msg.delta);
            break;
        case 'text_done':
            // Text response complete - already accumulated via deltas
            break;
        case 'speech_started':
            onSpeechStarted(msg.itemId);
            break;
        case 'speech_stopped':
            onSpeechStopped();
            break;
        case 'stop_playback':
            // Server truncated the output audio (manual Stop button). Clear any
            // local PCM playback and drop the speaking glow immediately.
            stopAudioPlayback();
            break;
        case 'transcript_empty':
            onTranscriptEmpty(msg.itemId);
            break;
        case 'response_created':
            pendingAssistantText = '';
            currentAssistantContentEl = null;
            addMessage('assistant', '');
            isSpeaking = true;
            startThinking();
            break;
        case 'response_done':
            isSpeaking = false;
            stopThinking();
            // NB: do NOT drop the speaking glow / caption here. response_done
            // marks the end of GENERATION, not playback — in WebRTC avatar mode
            // the avatar keeps talking over the media stream well after this
            // fires. The OFF transition is owned by the real-playback signals:
            // the data-channel IDLE event and the audio AnalyserNode (WebRTC),
            // the play-chunk drain (PCM/websocket), and the watchdog backstop.
            // Don't stop play-chunk animation here - the animation loop
            // will self-terminate when all buffered audio finishes playing
            break;
        case 'session_closed':
            addMessage('system', 'Session closed');
            handleDisconnect();
            break;
        case 'avatar_connecting':
            addMessage('system', 'Avatar connecting...');
            break;
        case 'video_data':
            handleVideoChunk(msg.delta);
            break;
        default:
            // Log unknown events in dev mode
            if (isDeveloperMode) {
                console.log('Unhandled:', type, msg);
            }
    }
}

let pendingAssistantText = '';
let currentAssistantContentEl = null;

function onAssistantDelta(text) {
    // First token of the answer has arrived — tear down the thinking indicator.
    if (thinkingActive || thinkingShowTimer) stopThinking();
    // Speaking glow is normally driven by real playback (data-channel events +
    // audio analyser). This transcript-based ON is only a fallback for before
    // the analyser has proven itself and when the service isn't sending speaking
    // events — so the glow still appears. Once real audio is detected, the
    // analyser / data-channel events own the ON/OFF timing (and clearing it
    // here would fight them).
    if (!avatarAnalyserProven && !avatarSpeechTurnActive) setAvatarSpeaking(true);
    pendingAssistantText += text;
    // Mirror the live assistant transcript into the on-avatar caption band.
    setAvatarCaption(pendingAssistantText, 'assistant');
    // Cache the DOM ref instead of running querySelectorAll on every delta token.
    if (!currentAssistantContentEl || !currentAssistantContentEl.isConnected) {
        const messages = document.querySelectorAll('.message.assistant .message-content');
        currentAssistantContentEl = messages.length > 0 ? messages[messages.length - 1] : null;
    }
    if (currentAssistantContentEl) {
        currentAssistantContentEl.textContent = pendingAssistantText;
        scrollChatToBottom();
    } else {
        addMessage('assistant', pendingAssistantText);
        const messages = document.querySelectorAll('.message.assistant .message-content');
        currentAssistantContentEl = messages.length > 0 ? messages[messages.length - 1] : null;
    }
}

async function onSessionStarted(msg) {
    isConnected = true;
    isConnecting = false;
    updateConnectionUI();

    // Update the "connecting..." status message with the real session ID
    const sessionId = msg.sessionId || '';
    const statusMessages = document.querySelectorAll('.message.system .message-content');
    for (const el of statusMessages) {
        if (el.textContent.includes('debug id: connecting...')) {
            el.textContent = `Session started, click on the mic button to start conversation! debug id: ${sessionId || 'unknown'}`;
            break;
        }
    }

    // Show appropriate content area
    avatarEnabled = msg.config?.avatarEnabled || false;
    avatarOutputMode = msg.config?.avatarOutputMode || 'webrtc';
    const isPhotoAvatarSession = document.getElementById('isPhotoAvatar')?.checked || false;
    const avatarContainer = document.getElementById('avatarVideoContainer');
    if (avatarContainer) {
        avatarContainer.classList.toggle('photo-avatar', isPhotoAvatarSession);
    }
    const labelEl = document.getElementById('avatarNameLabel');
    if (labelEl && !avatarEnabled) {
        // Avatar name is revealed together with the video (see revealAvatarVideo);
        // keep it empty here so it doesn't pop in during the loading spinner.
        labelEl.textContent = '';
    }
    updateDeveloperModeLayout();

    // If avatar is enabled with websocket mode, set up MediaSource video playback
    if (avatarEnabled && avatarOutputMode === 'websocket') {
        setupWebSocketVideoPlayback(isPhotoAvatarSession);
    }

    // Reveal the mic. With an avatar, wait until the avatar video is on screen so
    // the controls don't pop in before the face does. A fallback timer guarantees
    // the mic still appears even if the video frame never fires (so a stalled
    // avatar can never permanently block the conversation).
    if (avatarEnabled && avatarConnecting) {
        clearTimeout(micRevealTimer);
        micRevealTimer = setTimeout(showMicControls, 8000);
    } else {
        showMicControls();
    }

    // Start audio capture but leave mic off by default. The capture stream
    // stays live; the mic is muted at the track level (applyMicMuteState), so
    // silence keeps flowing and the server VAD always has a continuous stream.
    await startAudioCapture();
    isRecording = false;
    applyMicMuteState();
    stopRecordAnimation();
    resetVolumeCircle();
    updateMicUI();
}

function showMicControls() {
    clearTimeout(micRevealTimer);
    micRevealTimer = null;
    document.getElementById('recordContainer').classList.remove('hidden');
}

// ===== UI State =====
function setConnecting(connecting) {
    isConnecting = connecting;
    updateConnectionUI();
}

function updateConnectionUI() {
    const btn = document.getElementById('connectBtn');
    const text = document.getElementById('connectBtnText');

    btn.classList.remove('connected', 'connecting');
    if (isConnected) {
        btn.classList.add('connected');
        text.textContent = 'Disconnect';
    } else if (isConnecting) {
        btn.classList.add('connecting');
        text.textContent = 'Connecting...';
    } else {
        text.textContent = 'Connect';
    }

    // Disable connect button while connecting
    btn.disabled = isConnecting;

    // Scene Settings title: show "(Live Adjustable)" when connected
    const sceneTitle = document.getElementById('sceneSettingsTitle');
    if (sceneTitle) {
        sceneTitle.textContent = isConnected ? 'Scene Settings (Live Adjustable)' : 'Scene Settings';
    }

    // Update all control disabled states
    updateControlStates();

    // Mic buttons
    updateMicUI();
}

// ===== Control Enable/Disable States =====
// Controls that should be disabled when connected (locked during session)
const SETTINGS_CONTROLS = [
    // Conversation Settings
    'srModel', 'recognitionLanguage',
    'useNS', 'useEC', 'turnDetectionType', 'turnDetectionSilenceMs', 'enableBargeIn', 'removeFillerWords',
    'eouDetectionType', 'enableProactive',
    'voiceTemperature', 'voiceSpeed',
    // Voice Configuration
    'voiceType', 'voiceDeploymentId', 'customVoiceName',
    'personalVoiceName', 'personalVoiceModel', 'voiceName',
    // Avatar Configuration
    'avatarEnabled', 'isPhotoAvatar', 'avatarOutputMode',
    'isCustomAvatar', 'avatarName', 'photoAvatarName',
    'customAvatarName', 'avatarBackgroundImageUrl',
];

// Controls that should be disabled when NOT connected (chat interaction)
const CHAT_CONTROLS = [
    'textInput',
    'stageTextInput',
];

function updateControlStates() {
    // Disable all settings controls when connected
    for (const id of SETTINGS_CONTROLS) {
        const el = document.getElementById(id);
        if (el) el.disabled = isConnected;
    }

    // Disable chat controls when NOT connected
    for (const id of CHAT_CONTROLS) {
        const el = document.getElementById(id);
        if (el) el.disabled = !isConnected;
    }

    // Mic button (developer mode) - disabled when not connected
    const micBtn = document.getElementById('micBtn');
    if (micBtn) micBtn.disabled = !isConnected;

    // Send button - disabled when not connected
    const sendBtns = document.querySelectorAll('.send-btn');
    sendBtns.forEach(btn => btn.disabled = !isConnected);

    // Record button (non-developer mode footer) - disabled when not connected
    const recordBtn = document.getElementById('recordBtn');
    if (recordBtn) recordBtn.disabled = !isConnected;

    // Docked avatar mic - disabled until the session is connected so it can't
    // be clicked (no-op) while the avatar is still connecting.
    const avatarMicBtn = document.getElementById('avatarMicBtn');
    if (avatarMicBtn) avatarMicBtn.disabled = !isConnected;
}

// Whether the avatar panel (video frame + loading placeholder) should be shown.
// Once connected we trust the negotiated avatarEnabled; while still connecting we
// use the snapshot taken at connect time so the frame can appear at t=0.
function shouldShowAvatarPanel() {
    return isConnected ? avatarEnabled : (avatarConnecting && pendingAvatarEnabled);
}

// Set the avatar name label from the current config inputs (known client-side at
// connect time, so we don't have to wait for session_started to show it).
function setAvatarNameLabelFromConfig() {
    const labelEl = document.getElementById('avatarNameLabel');
    if (!labelEl) return;
    // AVATAR_DISPLAY_NAME (the single branding knob) wins when set, shown
    // verbatim. Otherwise derive a friendly name from the selected avatar model.
    if (avatarDisplayNameText) {
        labelEl.textContent = avatarDisplayNameText;
        return;
    }
    const isCustomA = document.getElementById('isCustomAvatar')?.checked;
    const isPhotoA = document.getElementById('isPhotoAvatar')?.checked;
    const rawName = (isCustomA
        ? (document.getElementById('customAvatarName')?.value || '')
        : isPhotoA
            ? (document.getElementById('photoAvatarName')?.value || '')
            : (document.getElementById('avatarName')?.value || '')).trim();
    // Strip suffixes like '-business', '-casual-sitting' for a friendlier label.
    // Deriving from the live inputs (rather than the server-resolved
    // assistantName) is what lets the label follow the dropdown in DEVELOPER_MODE;
    // assistantName is the fallback so a blank/misconfigured model never leaves
    // the stage nameless.
    labelEl.textContent = rawName ? rawName.split('-')[0] : assistantNameText;
}

// Safety net for an avatar that never appears. The reveal is driven solely by the
// video element's 'playing' event, so anything that stops playback — a refused
// autoplay, a stalled track, a failed ICE negotiation — used to leave the stage
// on "Connecting…" indefinitely with no way out but a manual page reload.
//
// This was previously armed only inside the Teams webview, so the standalone
// browser had no recovery path at all.
//
// It self-heals first: re-issuing play() costs nothing and silently fixes the
// autoplay case. Only if the video is still not running does it surface a
// visible, tappable restart state.
function armAvatarRevealWatchdog() {
    clearAvatarRevealWatchdog();
    if (isDeveloperMode) return;
    // Teams webviews fail faster and more often, so give them a shorter leash.
    // Standalone gets a generous budget: a cold Voice Live handshake plus ICE can
    // legitimately take well over ten seconds on a slow link, and a false positive
    // here would replace a working avatar with an error.
    const timeoutMs = isEmbeddedInTeams() ? 15000 : 25000;
    avatarRevealWatchdog = setTimeout(() => {
        avatarRevealWatchdog = null;
        // Only fire if the avatar genuinely never came up.
        if (!avatarConnecting) return;
        retryPausedAvatarMedia('reveal watchdog');
        // Give the retry a moment to take effect before declaring failure.
        avatarRevealWatchdog = setTimeout(() => {
            avatarRevealWatchdog = null;
            if (!avatarConnecting) return;
            console.warn('[Avatar] reveal watchdog fired — video never started');
            clearAvatarLoading();
            avatarConnecting = false;
            setConnectionState('error');
        }, 3000);
    }, timeoutMs);
}

function clearAvatarRevealWatchdog() {
    if (avatarRevealWatchdog) { clearTimeout(avatarRevealWatchdog); avatarRevealWatchdog = null; }
}

function showAvatarLoading(text) {
    if (avatarLoadingHideTimer) { clearTimeout(avatarLoadingHideTimer); avatarLoadingHideTimer = null; }
    const el = document.getElementById('avatarLoading');
    if (!el) return;
    const t = document.getElementById('avatarLoadingText');
    if (t && text) t.textContent = text;
    el.classList.remove('hidden', 'fade-out');
}

// Fade the placeholder out smoothly, then remove it from layout once the CSS
// transition has finished.
function hideAvatarLoading() {
    const el = document.getElementById('avatarLoading');
    if (!el) return;
    el.classList.add('fade-out');
    if (avatarLoadingHideTimer) clearTimeout(avatarLoadingHideTimer);
    avatarLoadingHideTimer = setTimeout(() => {
        el.classList.add('hidden');
        avatarLoadingHideTimer = null;
    }, 500);
}

// Hide the placeholder immediately (no fade) and reset it — used on disconnect/error
// so a later reconnect starts from a clean state.
function clearAvatarLoading() {
    if (avatarLoadingHideTimer) { clearTimeout(avatarLoadingHideTimer); avatarLoadingHideTimer = null; }
    const el = document.getElementById('avatarLoading');
    if (el) el.classList.add('hidden');
    if (el) el.classList.remove('fade-out');
}

// Fade the live avatar video in and the placeholder out. Called on the first
// 'playing' event from the WebRTC or websocket video element.
function revealAvatarVideo(mediaPlayer) {
    if (mediaPlayer) mediaPlayer.classList.add('avatar-video-ready');
    avatarConnecting = false;
    clearAvatarRevealWatchdog();
    setAvatarNameLabelFromConfig();
    hideAvatarLoading();
    showMicControls();
    // Avatar is now on screen — show the onboarding hint + suggested prompts
    // (unless the user has already interacted or the avatar is mid-greeting).
    showOnboarding();
    // Avatar is now on screen — refresh layout so the docked mic appears (and
    // the footer mic bar hides) exactly when the video reveals.
    updateDeveloperModeLayout();
}

// ===== Avatar "thinking" indicator =====
// Scheduled on response_created; only actually shown if the first answer token
// hasn't arrived within THINKING_SHOW_DELAY_MS, so fast turns never flash it.
function startThinking() {
    stopThinking();
    // Suppress while the avatar itself is still loading in (greeting turn) or
    // when there's no avatar on screen.
    if (!(isConnected && avatarEnabled) || avatarConnecting) return;
    const gen = ++thinkingGen;
    thinkingCaptionIndex = 0;
    thinkingShowTimer = setTimeout(() => showThinking(gen), THINKING_SHOW_DELAY_MS);
}

function showThinking(gen) {
    thinkingShowTimer = null;
    // A newer turn (or a teardown) superseded this scheduled show — abort.
    if (gen !== thinkingGen) return;
    if (!(isConnected && avatarEnabled) || avatarConnecting) return;
    const el = document.getElementById('avatarThinking');
    if (!el) return;
    thinkingActive = true;
    setThinkingCaption(THINKING_CAPTIONS[0]);
    el.classList.add('visible');
    thinkingRotateTimer = setInterval(() => {
        thinkingCaptionIndex = (thinkingCaptionIndex + 1) % THINKING_CAPTIONS.length;
        setThinkingCaption(THINKING_CAPTIONS[thinkingCaptionIndex]);
    }, THINKING_ROTATE_MS);
    thinkingSlowTimer = setTimeout(() => {
        if (thinkingRotateTimer) { clearInterval(thinkingRotateTimer); thinkingRotateTimer = null; }
        setThinkingCaption(THINKING_SLOW_CAPTION);
    }, THINKING_SLOW_MS);
    // Failsafe: force-clear if the answer/response_done never arrives.
    thinkingMaxTimer = setTimeout(stopThinking, THINKING_MAX_MS);
}

function setThinkingCaption(text) {
    const t = document.getElementById('avatarThinkingText');
    if (t) t.textContent = text;
}

function stopThinking() {
    // Invalidate any in-flight scheduled show so it can't fire after teardown.
    thinkingGen++;
    if (thinkingShowTimer) { clearTimeout(thinkingShowTimer); thinkingShowTimer = null; }
    if (thinkingRotateTimer) { clearInterval(thinkingRotateTimer); thinkingRotateTimer = null; }
    if (thinkingSlowTimer) { clearTimeout(thinkingSlowTimer); thinkingSlowTimer = null; }
    if (thinkingMaxTimer) { clearTimeout(thinkingMaxTimer); thinkingMaxTimer = null; }
    thinkingActive = false;
    const el = document.getElementById('avatarThinking');
    if (el) el.classList.remove('visible');
}

// ===== Live captions =====
// Reuses the existing transcript stream (no extra model calls). The caption
// element lives inside .avatar-stage, so it is naturally hidden whenever the
// avatar panel isn't shown.
function setAvatarCaption(text, role) {
    if (!captionsEnabled) return;
    if (captionClearTimer) { clearTimeout(captionClearTimer); captionClearTimer = null; }
    const el = document.getElementById('avatarCaption');
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('user', role === 'user');
    el.classList.toggle('visible', !!text);
    // Rolling subtitle: keep the newest line in view as the transcript streams,
    // so long answers scroll up instead of freezing on the first two lines.
    // Batched with the chat scroll so a fast token stream costs one forced
    // layout per frame, not one per token.
    _scrollCaptionPending = true;
    _scheduleScrollFlush();
}

function clearAvatarCaption(delayMs) {
    const el = document.getElementById('avatarCaption');
    if (!el) return;
    if (captionClearTimer) { clearTimeout(captionClearTimer); captionClearTimer = null; }
    const doClear = () => {
        el.classList.remove('visible');
        el.textContent = '';
        el.classList.remove('user');
    };
    if (delayMs && delayMs > 0) {
        captionClearTimer = setTimeout(doClear, delayMs);
    } else {
        doClear();
    }
}

// ===== Speaking-state glow (audio-driven) =====
// Driven by actual audio playback rather than response_done, because buffered
// audio keeps playing after the response is marked complete. A per-chunk
// watchdog guarantees the glow can never stick on if a stop path is missed.
function setAvatarSpeaking(on) {
    if (on) {
        armSpeakingWatchdog();
        if (avatarSpeaking) return;
        avatarSpeaking = true;
        const stage = document.querySelector('.avatar-stage');
        if (stage) stage.classList.add('speaking');
        // The avatar is talking — tuck the onboarding hint away (temporarily).
        hideOnboardingTemporarily();
        updateStopButton();
    } else {
        if (speakingWatchdogTimer) { clearTimeout(speakingWatchdogTimer); speakingWatchdogTimer = null; }
        if (!avatarSpeaking) return;
        avatarSpeaking = false;
        const stage = document.querySelector('.avatar-stage');
        if (stage) stage.classList.remove('speaking');
        // Let the last words linger briefly, then fade the caption out.
        clearAvatarCaption(900);
        // Restore onboarding if the user hasn't actually interacted yet.
        maybeRestoreOnboarding();
        updateStopButton();
    }
}

// The stop button stays docked next to the mic while the avatar is on screen
// (presence handled in updateDeveloperModeLayout); here we just toggle whether
// it's actionable — enabled only while the avatar is actually speaking.
function updateStopButton() {
    const btn = document.getElementById('avatarStopBtn');
    if (!btn) return;
    btn.disabled = !avatarSpeaking;
    btn.classList.toggle('active', avatarSpeaking);
}

// User pressed Stop: tell the server to cancel the in-flight response (reuses
// the existing barge-in interrupt path) and halt local audio immediately.
function stopAvatarSpeaking() {
    if (!avatarSpeaking) return;
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'interrupt' }));
    }
    stopAudioPlayback();
}

// Reset on every audio chunk; if audio stops arriving for SPEAKING_WATCHDOG_MS
// the glow force-clears. Resetting per chunk means long answers never get cut.
// The WebRTC analyser loop calls setAvatarSpeaking(true) every animation frame,
// so re-arming is throttled: churning a timer 60x/s bought nothing against a
// 4s deadline.
let lastWatchdogArmMs = 0;
const WATCHDOG_REARM_MIN_MS = 250;

function armSpeakingWatchdog() {
    const now = performance.now();
    if (speakingWatchdogTimer && (now - lastWatchdogArmMs) < WATCHDOG_REARM_MIN_MS) return;
    lastWatchdogArmMs = now;
    if (speakingWatchdogTimer) clearTimeout(speakingWatchdogTimer);
    speakingWatchdogTimer = setTimeout(() => setAvatarSpeaking(false), SPEAKING_WATCHDOG_MS);
}

// ===== WebRTC avatar speaking detection =====
// Tap the avatar's live remote audio track with an AnalyserNode and detect real
// audio energy. This (plus the data-channel events below) keeps the glow +
// caption alive for the avatar's entire spoken duration, instead of cutting out
// at response_done while buffered audio is still playing.
function attachAvatarAudioAnalyser(stream) {
    try {
        stopAvatarAudioAnalyser();
        if (!stream) return;
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        avatarAudioCtx = new Ctx();
        if (avatarAudioCtx.state === 'suspended' && avatarAudioCtx.resume) {
            avatarAudioCtx.resume().catch(() => {});
        }
        const src = avatarAudioCtx.createMediaStreamSource(stream);
        avatarAudioAnalyser = avatarAudioCtx.createAnalyser();
        avatarAudioAnalyser.fftSize = 512;
        avatarAudioAnalyser.smoothingTimeConstant = 0.3;
        avatarAudioData = new Uint8Array(avatarAudioAnalyser.fftSize);
        // Tap the track for level detection only — do NOT connect to the
        // destination. The <audio> element already plays this stream; routing it
        // here too would double the audio.
        src.connect(avatarAudioAnalyser);
        avatarAnalyserProven = false;
        avatarLastAudibleTs = 0;
        runAvatarSpeakingLoop();
    } catch (e) {
        console.warn('[avatar] audio analyser unavailable:', e);
    }
}

function runAvatarSpeakingLoop() {
    if (avatarAudioRafId) return;
    const tick = () => {
        if (!avatarAudioAnalyser || !avatarAudioData) { avatarAudioRafId = null; return; }
        avatarAudioAnalyser.getByteTimeDomainData(avatarAudioData);
        let sumSq = 0;
        for (let i = 0; i < avatarAudioData.length; i++) {
            const v = (avatarAudioData[i] - 128) / 128;
            sumSq += v * v;
        }
        const rms = Math.sqrt(sumSq / avatarAudioData.length);
        const now = performance.now();
        if (rms > AVATAR_SPEAK_RMS_ON) {
            avatarLastAudibleTs = now;
            avatarAnalyserProven = true;
        }
        if (avatarSpeechTurnActive) {
            // Service says this speaking turn is in progress — keep it lit even
            // through short between-sentence pauses.
            setAvatarSpeaking(true);
        } else if (avatarAnalyserProven) {
            // No active turn signal: follow real audio energy (with a hangover
            // so brief gaps don't blink the glow).
            setAvatarSpeaking((now - avatarLastAudibleTs) < AVATAR_SPEAK_HANGOVER_MS);
        }
        avatarAudioRafId = requestAnimationFrame(tick);
    };
    avatarAudioRafId = requestAnimationFrame(tick);
}

function stopAvatarAudioAnalyser() {
    if (avatarAudioRafId) { cancelAnimationFrame(avatarAudioRafId); avatarAudioRafId = null; }
    avatarAudioAnalyser = null;
    avatarAudioData = null;
    if (avatarAudioCtx) { try { avatarAudioCtx.close(); } catch (e) {} avatarAudioCtx = null; }
    avatarAnalyserProven = false;
    avatarSpeechTurnActive = false;
}

// The avatar service emits speaking-state events over the WebRTC data channel.
// These bracket the avatar's actual spoken turn precisely, so they are our
// primary glow/caption ON/OFF signal in WebRTC mode.
function handleAvatarDataChannelMessage(data) {
    if (typeof data !== 'string') return;
    if (data.indexOf('EVENT_TYPE_SWITCH_TO_SPEAKING') !== -1) {
        avatarSpeechTurnActive = true;
        avatarLastAudibleTs = performance.now();
        setAvatarSpeaking(true);
    } else if (data.indexOf('EVENT_TYPE_SWITCH_TO_IDLE') !== -1) {
        avatarSpeechTurnActive = false;
        // If the analyser is live it drops the glow once trailing audio drains;
        // otherwise drop it now.
        if (!avatarAnalyserProven) setAvatarSpeaking(false);
    }
}

// ===== Suggested prompts + onboarding hint =====
function buildOnboarding() {
    const wrap = document.getElementById('avatarOnboarding');
    if (!wrap) return;
    if (!suggestedPromptsEnabled) { wrap.hidden = true; return; }
    const hintEl = document.getElementById('avatarOnboardingHint');
    if (hintEl) hintEl.textContent = onboardingHintText || '';
    const chipsEl = document.getElementById('avatarOnboardingChips');
    if (chipsEl) {
        chipsEl.innerHTML = '';
        suggestedPrompts.forEach((q) => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'prompt-chip';
            btn.textContent = q;
            btn.addEventListener('click', () => {
                if (sendQuery(q)) dismissOnboarding();
            });
            chipsEl.appendChild(btn);
        });
    }
}

// Show on first avatar reveal, if enabled and not already dismissed.
function showOnboarding() {
    if (!suggestedPromptsEnabled || onboardingDismissed || avatarSpeaking) return;
    const wrap = document.getElementById('avatarOnboarding');
    if (!wrap) return;
    const hasHint = (onboardingHintText || '').length > 0;
    const hasChips = suggestedPrompts.length > 0;
    if (!hasHint && !hasChips) return;
    wrap.hidden = false;
    // Next frame so the fade-in transition runs.
    requestAnimationFrame(() => wrap.classList.add('visible'));
}

// Hide while the avatar is speaking (e.g. proactive greeting) without marking
// it permanently dismissed.
function hideOnboardingTemporarily() {
    const wrap = document.getElementById('avatarOnboarding');
    if (wrap) wrap.classList.remove('visible');
}

function maybeRestoreOnboarding() {
    if (onboardingDismissed) return;
    if (!(isConnected && avatarEnabled) || avatarConnecting) return;
    showOnboarding();
}

// Permanent dismissal after the first real user action.
function dismissOnboarding() {
    if (onboardingDismissed) return;
    onboardingDismissed = true;
    const wrap = document.getElementById('avatarOnboarding');
    if (!wrap) return;
    wrap.classList.remove('visible');
    setTimeout(() => { wrap.hidden = true; }, 300);
}

function updateDeveloperModeLayout() {
    const contentArea = document.getElementById('contentArea');
    const avatarVideoContainer = document.getElementById('avatarVideoContainer');
    const volumeAnimation = document.getElementById('volumeAnimation');
    const chatArea = document.getElementById('chatArea');
    const inputArea = document.getElementById('inputArea');
    const footerArea = document.getElementById('footerArea');

    const hide = (el, h) => el.classList.toggle('hidden', h);
    const showAvatar = shouldShowAvatarPanel();

    if (isDeveloperMode) {
        // Developer mode: show input area, hide footer
        hide(inputArea, false);
        hide(footerArea, true);

        if (showAvatar) {
            // Avatar (connecting or connected) + developer: side-by-side (avatar + chat)
            contentArea.classList.add('developer-layout');
            hide(avatarVideoContainer, false);
            hide(chatArea, false);
            hide(volumeAnimation, true);
        } else if (isConnected) {
            // No avatar + developer: side-by-side layout (robot + chat)
            contentArea.classList.add('developer-layout');
            hide(avatarVideoContainer, true);
            hide(chatArea, false);
            hide(volumeAnimation, false);
        } else {
            // Not connected: just show chat
            contentArea.classList.remove('developer-layout');
            hide(avatarVideoContainer, true);
            hide(chatArea, false);
            hide(volumeAnimation, true);
        }
    } else {
        // Normal mode: show footer, hide input area
        hide(inputArea, true);
        hide(footerArea, false);
        contentArea.classList.remove('developer-layout');

        if (showAvatar) {
            // Avatar (connecting or connected) + normal: only avatar video, no chat
            hide(avatarVideoContainer, false);
            hide(chatArea, true);
            hide(volumeAnimation, true);
        } else if (isConnected) {
            // No avatar + normal: only robot, no chat
            hide(avatarVideoContainer, true);
            hide(chatArea, true);
            hide(volumeAnimation, false);
        } else {
            // Not connected: show chat history
            hide(avatarVideoContainer, true);
            hide(chatArea, false);
            hide(volumeAnimation, true);
        }
    }

    // Docked avatar mic: visible only once the avatar video is actually on
    // screen (not during the connecting spinner). While the avatar is shown we
    // hide the footer mic bar so the avatar UI stays clean; the footer remains
    // the mic control for robot mode and the pre-connect screen.
    const avatarMicBtn = document.getElementById('avatarMicBtn');
    const avatarReady = showAvatar && !avatarConnecting;
    if (avatarMicBtn) hide(avatarMicBtn, !avatarReady);
    if (avatarReady) hide(footerArea, true);

    // Docked stop-speaking button: lives next to the mic with the same
    // readiness as the docked mic (env-gated by ENABLE_STOP_BUTTON). It stays
    // visible while the avatar is on screen but is only *enabled* while she's
    // actually speaking — see updateStopButton().
    const avatarStopBtn = document.getElementById('avatarStopBtn');
    if (avatarStopBtn) hide(avatarStopBtn, !(avatarReady && stopButtonEnabled));
    updateStopButton();

    // Bottom-center stage composer: normal (production) mode only, env-gated,
    // and only once the avatar is on screen (same readiness as the docked mic).
    // Never shown in developer mode so the dev #inputArea layout is untouched.
    const stageComposer = document.getElementById('stageComposer');
    if (stageComposer) {
        const showComposer = !isDeveloperMode && textInputEnabled && avatarReady;
        hide(stageComposer, !showComposer);
    }

    // Visibility may have changed while recording; sync the listening ring loop.
    updateAvatarMicRing();
}

let soundWaveRafId = null;
// Bar elements are cached at creation time. The previous implementation ran an
// 80ms setInterval that did 20 document.getElementById() lookups plus 20
// `style.transition` + 20 `style.height` writes on EVERY tick (~250 id lookups
// and ~500 style writes per second) — and, being a timer, it kept doing all of
// that while the tab was hidden. Now: ids resolved once, transition set once at
// creation, unchanged heights skipped, and the loop is rAF-driven so the
// browser suspends it in background tabs.
let soundWaveBars = [];
let soundWaveHeights = [];
const SOUND_WAVE_INTERVAL_MS = 80;

function buildSoundWaveBars(container, startIndex) {
    if (!container || container.children.length > 0) return;
    for (let i = startIndex; i < startIndex + 10; i++) {
        const bar = document.createElement('div');
        bar.className = 'bar';
        bar.id = `item-${i}`;
        bar.style.height = '2px';
        // Set once here instead of re-assigning the same value every tick.
        bar.style.transition = 'height 0.08s ease';
        container.appendChild(bar);
    }
}

function updateSoundWaveAnimation() {
    const leftWave = document.getElementById('soundWaveLeft');
    const rightWave = document.getElementById('soundWaveRight');

    if (isConnected && avatarEnabled && isRecording && !isDeveloperMode) {
        // Create sound wave bars if not already present
        buildSoundWaveBars(leftWave, 0);
        buildSoundWaveBars(rightWave, 10);

        // (Re)build the element cache if the bars were just created or replaced.
        if (soundWaveBars.length !== 20 || !soundWaveBars[0] || !soundWaveBars[0].isConnected) {
            soundWaveBars = [];
            for (let i = 0; i < 20; i++) soundWaveBars.push(document.getElementById(`item-${i}`));
            soundWaveHeights = new Array(20).fill(-1);
        }

        // Start animation
        if (!soundWaveRafId) {
            let lastTick = 0;
            const tick = (ts) => {
                soundWaveRafId = requestAnimationFrame(tick);
                // Keep the original ~12.5Hz cadence so the 0.08s CSS transition
                // still reads as a smooth spectral sweep.
                if (ts - lastTick < SOUND_WAVE_INTERVAL_MS) return;
                lastTick = ts;
                const hasData = !!(micAnalyserNode && micAnalyserDataArray);
                if (hasData) micAnalyserNode.getByteFrequencyData(micAnalyserDataArray);
                const binCount = hasData ? micAnalyserDataArray.length : 0;
                for (let i = 0; i < 20; i++) {
                    const ele = soundWaveBars[i];
                    if (!ele) continue;
                    let height = 2;
                    if (hasData) {
                        // Map the 20 bars to different frequency bins for a real
                        // spectral visualizer (focus on speech frequencies).
                        const binIndex = Math.floor((binCount / 40) * (i + 2));
                        const binValue = micAnalyserDataArray[binIndex] || 0;
                        // Scale value (0-255) to height (2px - 50px)
                        height = Math.round(2 + (binValue / 255) * 48);
                    }
                    if (height === soundWaveHeights[i]) continue;
                    soundWaveHeights[i] = height;
                    ele.style.height = `${height}px`;
                }
            };
            soundWaveRafId = requestAnimationFrame(tick);
        }
        if (leftWave) leftWave.style.display = '';
        if (rightWave) rightWave.style.display = '';
    } else {
        // Stop animation, hide waves
        if (soundWaveRafId) {
            cancelAnimationFrame(soundWaveRafId);
            soundWaveRafId = null;
        }
        if (leftWave) leftWave.style.display = 'none';
        if (rightWave) rightWave.style.display = 'none';
    }
}
function updateMicUI() {
    const micBtn = document.getElementById('micBtn');
    const recordBtn = document.getElementById('recordBtn');

    // Toggle recording class
    if (micBtn) micBtn.classList.toggle('recording', isRecording);
    if (recordBtn) recordBtn.classList.toggle('recording', isRecording);
    const avatarMicBtn = document.getElementById('avatarMicBtn');
    if (avatarMicBtn) avatarMicBtn.classList.toggle('recording', isRecording);

    // Toggle icon visibility: show off-icon when not recording, on-icon when recording
    document.querySelectorAll('.mic-off-icon').forEach(el => {
        el.classList.toggle('hidden', isRecording);
    });
    document.querySelectorAll('.mic-on-icon').forEach(el => {
        el.classList.toggle('hidden', !isRecording);
    });

    // Update label text
    const label = document.querySelector('.microphone-label');
    if (label) {
        label.textContent = isRecording ? 'Turn off microphone' : 'Turn on microphone';
    }

    // Update sound wave visibility
    updateSoundWaveAnimation();

    // Drive the docked mic's volume-reactive listening ring
    updateAvatarMicRing();
}

// Volume-reactive ring on the docked avatar mic. While recording, sample the
// mic analyser each frame and expose a smoothed 0..1 level as the --mic-level
// CSS var, which scales/brightens the ring (see .avatar-mic-ring in style.css).
let avatarMicRingRaf = null;

function updateAvatarMicRing() {
    const btn = document.getElementById('avatarMicBtn');
    if (!btn) return;

    // Only animate when the docked mic is actually visible (avatar mode). In
    // robot/footer mode the button is hidden, so skip the per-frame analyser work.
    if (isRecording && !btn.classList.contains('hidden')) {
        if (avatarMicRingRaf) return;
        let smoothed = 0;
        const tick = () => {
            if (!isRecording || btn.classList.contains('hidden')) {
                avatarMicRingRaf = null;
                btn.style.setProperty('--mic-level', '0');
                return;
            }
            let level = 0;
            if (micAnalyserNode && micAnalyserDataArray) {
                micAnalyserNode.getByteFrequencyData(micAnalyserDataArray);
                const len = micAnalyserDataArray.length;
                // Sample the low speech band (~25Hz–750Hz). The higher bins are
                // near-silent for voice, so averaging the full range washes the
                // level out to ~0; this keeps the ring responsive to speech.
                const start = 2;
                const end = Math.max(start + 1, Math.floor(len / 16));
                let sum = 0;
                for (let i = start; i < end; i++) sum += micAnalyserDataArray[i];
                const avg = sum / (end - start);
                level = Math.min(1, avg / 70); // ~70/255 reads as a clear voice
            }
            // Ease toward the target so the ring breathes instead of flickering.
            smoothed += (level - smoothed) * 0.3;
            btn.style.setProperty('--mic-level', smoothed.toFixed(3));
            avatarMicRingRaf = requestAnimationFrame(tick);
        };
        avatarMicRingRaf = requestAnimationFrame(tick);
    } else {
        if (avatarMicRingRaf) {
            cancelAnimationFrame(avatarMicRingRaf);
            avatarMicRingRaf = null;
        }
        btn.style.setProperty('--mic-level', '0');
    }
}

// ===== Audio Capture (24kHz PCM16 via AudioWorklet) =====
async function startAudioCapture() {
    try {
        mediaStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                sampleRate: 24000,
                echoCancellation: true,
                noiseSuppression: true,
            }
        });
        // Honor the current mute state immediately, at the track level, so we
        // never stream real microphone audio before the user enables the mic.
        // A disabled track stays live and delivers silence (not nothing) to the
        // worklet — see applyMicMuteState for why that matters.
        applyMicMuteState();
        audioContext = new AudioContext({ sampleRate: 24000 });
        console.log('[Audio] AudioContext created, actual sampleRate:', audioContext.sampleRate);

        // Register AudioWorklet processor inline via Blob
        const processorCode = `
class PCM16Processor extends AudioWorkletProcessor {
    constructor() {
        super();
        // 40ms at 24kHz = 960 samples. Smaller buffers give tighter
        // barge-in/interruption latency than the previous 100ms.
        this.bufferSize = 960;
        this.buffer = new Float32Array(this.bufferSize);
        this.offset = 0;
    }
    process(inputs) {
        const input = inputs[0];
        if (!input || !input[0]) return true;
        const data = input[0];
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
        const blob = new Blob([processorCode], { type: 'application/javascript' });
        const url = URL.createObjectURL(blob);
        await audioContext.audioWorklet.addModule(url);
        URL.revokeObjectURL(url);

        const source = audioContext.createMediaStreamSource(mediaStream);
        workletNode = new AudioWorkletNode(audioContext, 'pcm16-processor');

        // Create analyser for mic volume visualization
        const micAnalyser = audioContext.createAnalyser();
        micAnalyser.fftSize = 2048;
        micAnalyser.smoothingTimeConstant = 0.85;
        const micDataArray = new Uint8Array(micAnalyser.frequencyBinCount);

        workletNode.port.onmessage = (e) => {
            if (!isConnected || !ws || ws.readyState !== WebSocket.OPEN) return;

            // Stream audio continuously while connected — including while muted.
            // Muting is done at the track level (mediaStream track.enabled =
            // false in applyMicMuteState), so a muted mic keeps delivering
            // *silence* frames here rather than nothing at all. This is
            // deliberate: cutting the stream on mute (the old behavior) orphaned
            // the server-side VAD when the user toggled the mic mid-utterance —
            // it had fired speech_started but, with no further audio (not even
            // silence) arriving, never fired speech_stopped, so the turn hung
            // forever ("voice won't go down"). Continuous silence lets the
            // server always observe end-of-speech and close the turn. The user's
            // real audio is never sent while muted (the track zeroes it).
            // We also intentionally do NOT gate on playback state: that is
            // fragile (a single missed response_done would stick the gate and
            // drop the mic permanently). Echo-driven false turns are prevented
            // by keeping client barge-in (stopAudioPlayback on speech_started)
            // and the server's interrupt_response in lock-step, plus the
            // browser's echo cancellation on the mic capture.
            audioChunksSent++;
            if (audioChunksSent <= 3 || audioChunksSent % 100 === 0) {
                console.log(`[Audio] Sending chunk #${audioChunksSent}, bytes=${e.data.byteLength}`);
            }
            // Send raw PCM16 bytes as a binary WebSocket frame — avoids
            // base64 + JSON wrap overhead (~45%) on every audio chunk.
            ws.send(e.data);
        };

        source.connect(workletNode);
        source.connect(micAnalyser);
        workletNode.connect(audioContext.destination);

        // Store mic analyser so volume animation can use it
        micAnalyserNode = micAnalyser;
        micAnalyserDataArray = micDataArray;
        analyserNode = micAnalyser;
        analyserDataArray = micDataArray;
        startVolumeAnimation('record');

        console.log('[Audio] Capture started (24kHz PCM16)');
    } catch (err) {
        console.error('Audio capture error', err);
        // Surface an actionable, normal-mode-visible state instead of a chat
        // line the user can't see in production.
        const name = err && err.name;
        if (name === 'NotAllowedError' || name === 'SecurityError') {
            notifySystem('Microphone access was blocked. Allow mic access in your browser, then retry.', 'error');
            setConnectionState('mic-blocked');
        } else if (name === 'NotFoundError' || name === 'OverconstrainedError' || name === 'DevicesNotFoundError') {
            notifySystem('No microphone was found. Connect a mic, then retry.', 'error');
            setConnectionState('mic-missing');
        } else {
            notifySystem('Microphone could not be started. Please retry.', 'error');
            setConnectionState('error');
        }
        return false;
    }
    return true;
}

function stopAudioCapture() {
    stopRecordAnimation();
    micAnalyserNode = null;
    micAnalyserDataArray = null;
    if (workletNode) { try { workletNode.disconnect(); } catch (e) {} workletNode = null; }
    if (audioContext) { try { audioContext.close(); } catch (e) {} audioContext = null; }
    if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
    resetVolumeCircle();
}

// ===== Audio Playback (24kHz PCM16) =====
function handleAudioBinary(arrayBuffer) {
    // Fast path for raw binary audio frames (no base64 decode needed).
    _playAudioPCM16(arrayBuffer);
}

function handleAudioDelta(base64Data) {
    // Legacy base64-in-JSON path (kept for backward compat).
    if (!base64Data) return;
    _playAudioPCM16(base64ToArrayBuffer(base64Data));
}

function _playAudioPCM16(arrayBuffer) {
    if (!playbackContext) {
        playbackContext = new AudioContext({ sampleRate: 24000 });
        // Create analyser for volume visualization
        analyserNode = playbackContext.createAnalyser();
        analyserNode.fftSize = 2048;
        analyserNode.smoothingTimeConstant = 0.85;
        analyserDataArray = new Uint8Array(analyserNode.frequencyBinCount);
        analyserNode.connect(playbackContext.destination);
        nextPlaybackTime = 0;
    }
    const int16 = new Int16Array(arrayBuffer);
    if (int16.length === 0) return;
    // Convert straight into the AudioBuffer's channel data. The previous code
    // allocated an intermediate Float32Array and then copied it in, doubling
    // both the allocation and the memcpy for every 40ms chunk (~25/s per turn).
    const buffer = playbackContext.createBuffer(1, int16.length, 24000);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < int16.length; i++) {
        channel[i] = int16[i] / 32768;
    }
    const source = playbackContext.createBufferSource();
    source.buffer = buffer;
    source.connect(analyserNode);

    const now = playbackContext.currentTime;
    if (nextPlaybackTime < now) nextPlaybackTime = now;
    source.start(nextPlaybackTime);
    nextPlaybackTime += buffer.duration;

    // Audio is playing → the avatar is speaking. Reset the watchdog on every
    // chunk so long answers stay lit; it force-clears only on real silence.
    setAvatarSpeaking(true);

    // Start playback volume animation (only if not already running)
    if (!playChunkAnimationFrameId) {
        startVolumeAnimation('play-chunk');
    }
}

function stopAudioPlayback() {
    stopPlayChunkAnimation();
    // Barge-in / teardown — the avatar is no longer speaking.
    setAvatarSpeaking(false);
    if (playbackContext) { try { playbackContext.close(); } catch (e) {} playbackContext = null; }
    playbackBufferQueue = [];
    nextPlaybackTime = 0;
    // Switch back to mic analyser if mic is on
    if (isRecording && micAnalyserNode) {
        analyserNode = micAnalyserNode;
        analyserDataArray = micAnalyserDataArray;
        startVolumeAnimation('record');
    } else {
        analyserNode = null;
        analyserDataArray = null;
        resetVolumeCircle();
    }
}

// ===== Volume Animation =====
function startVolumeAnimation(animationType) {
    if (animationType === 'record') {
        stopPlayChunkAnimation();
    } else {
        stopPlayChunkAnimation();
        stopRecordAnimation();
    }
    const isRecord = animationType === 'record';
    const calculateVolume = () => {
        if (analyserNode && analyserDataArray) {
            analyserNode.getByteFrequencyData(analyserDataArray);
            // Sum in place over the typed array. The previous
            // `Array.from(...).reduce(...)` allocated a boxed 1024-element JS
            // Array plus an iterator and ran 1024 closure calls EVERY frame
            // (~60x/s for the whole session), which is pure GC churn on the
            // animation hot path.
            let sum = 0;
            const len = analyserDataArray.length;
            for (let i = 0; i < len; i++) sum += analyserDataArray[i];
            updateVolumeCircle(sum / len, animationType);
        }

        if (isRecord) {
            // Stop record animation if mic was turned off
            if (!isRecording) {
                recordAnimationFrameId = null;
                resetVolumeCircle();
                return;
            }
            recordAnimationFrameId = requestAnimationFrame(calculateVolume);
        } else {
            // For playback: self-terminate when response is done AND audio finished
            if (!isSpeaking && (!playbackContext || playbackContext.currentTime >= nextPlaybackTime + 0.3)) {
                playChunkAnimationFrameId = null;
                // Audio has fully drained — drop the speaking glow.
                setAvatarSpeaking(false);
                // Switch back to mic animation or reset
                if (isRecording && micAnalyserNode) {
                    analyserNode = micAnalyserNode;
                    analyserDataArray = micAnalyserDataArray;
                    startVolumeAnimation('record');
                } else {
                    analyserNode = null;
                    analyserDataArray = null;
                    resetVolumeCircle();
                }
                return;
            }
            playChunkAnimationFrameId = requestAnimationFrame(calculateVolume);
        }
    };
    calculateVolume();
}

function stopRecordAnimation() {
    if (recordAnimationFrameId) {
        cancelAnimationFrame(recordAnimationFrameId);
        recordAnimationFrameId = null;
    }
}

function stopPlayChunkAnimation() {
    if (playChunkAnimationFrameId) {
        cancelAnimationFrame(playChunkAnimationFrameId);
        playChunkAnimationFrameId = null;
    }
}

function stopVolumeAnimation() {
    stopRecordAnimation();
    stopPlayChunkAnimation();
}

// Last written circle size/type, so identical frames skip the style write
// entirely (each write invalidates layout even when the value is unchanged).
let lastVolumeSize = -1;
let lastVolumeType = '';

function updateVolumeCircle(volume, animationType) {
    // Sub-pixel changes are invisible but each write invalidates layout, so
    // quantise and skip no-op frames.
    const size = Math.round(160 + volume);
    if (size === lastVolumeSize && animationType === lastVolumeType) return;
    const circle = document.getElementById('volumeCircle');
    if (!circle) return;
    if (animationType !== lastVolumeType) {
        circle.style.backgroundColor = animationType === 'record' ? 'lightgray' : 'lightblue';
        lastVolumeType = animationType;
    }
    lastVolumeSize = size;
    circle.style.width = size + 'px';
    circle.style.height = size + 'px';
}

function resetVolumeCircle() {
    lastVolumeSize = -1;
    lastVolumeType = '';
    const circle = document.getElementById('volumeCircle');
    if (!circle) return;
    circle.style.width = '';
    circle.style.height = '';
    circle.style.backgroundColor = '';
}

// ===== WebSocket Video Playback (MediaSource Extensions) =====
function setupWebSocketVideoPlayback(isPhotoAvatar) {
    // Clean any existing video
    cleanupWebSocketVideo();
    const container = document.getElementById('avatarVideo');
    if (container) container.innerHTML = '';

    // Create video element
    const videoElement = document.createElement('video');
    videoElement.id = 'ws-video';
    videoElement.autoplay = true;
    videoElement.playsInline = true;

    if (isPhotoAvatar) {
        videoElement.style.borderRadius = '10%';
    }
    videoElement.style.width = 'auto';
    videoElement.style.height = isDeveloperMode ? 'auto' : '';
    videoElement.style.objectFit = 'cover';
    videoElement.style.display = 'block';

    videoElement.addEventListener('canplay', () => {
        // Same autoplay exposure as the WebRTC path, except this element genuinely
        // needs its audio (fMP4 carries H.264 + AAC), so it cannot be muted.
        // A refusal therefore arms the gesture retry rather than only logging.
        playAvatarMedia(videoElement, 'video');
    });
    videoElement.addEventListener('playing', () => revealAvatarVideo(videoElement));
    // Tracked so the reveal watchdog and the gesture retry can reach this element.
    avatarVideoElement = videoElement;

    // fMP4 codec: H.264 video + AAC audio
    const FMP4_MIME_CODEC = 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"';

    if (!MediaSource.isTypeSupported(FMP4_MIME_CODEC)) {
        console.error('MediaSource fMP4 codec not supported');
        addMessage('system', 'WebSocket video playback not supported in this browser. Please use WebRTC mode.');
        return;
    }

    mediaSource = new MediaSource();
    videoElement.src = URL.createObjectURL(mediaSource);

    mediaSource.addEventListener('sourceopen', () => {
        try {
            if (mediaSource.readyState === 'open') {
                sourceBuffer = mediaSource.addSourceBuffer(FMP4_MIME_CODEC);
                sourceBuffer.addEventListener('updateend', () => {
                    processVideoChunkQueue();
                });
            }
        } catch (e) {
            console.error('Error creating SourceBuffer:', e);
        }
    });

    // Append to container
    if (container) {
        container.appendChild(videoElement);
    } else {
        pendingWsVideoElement = videoElement;
    }
}

let videoChunkCount = 0;

function handleVideoChunk(base64Data) {
    if (!base64Data) return;
    videoChunkCount++;
    if (videoChunkCount <= 5 || videoChunkCount % 100 === 0) {
        console.log(`[VIDEO] chunk #${videoChunkCount}, length=${base64Data.length}, mediaSource=${mediaSource?.readyState}, sourceBuffer=${!!sourceBuffer}`);
    }
    try {
        const binaryString = atob(base64Data);
        const arrayBuffer = new ArrayBuffer(binaryString.length);
        const bytes = new Uint8Array(arrayBuffer);
        for (let i = 0; i < binaryString.length; i++) {
            bytes[i] = binaryString.charCodeAt(i);
        }
        videoChunksQueue.push(arrayBuffer);
        processVideoChunkQueue();
    } catch (e) {
        console.error('Error handling video chunk:', e);
    }
}

function processVideoChunkQueue() {
    if (!sourceBuffer || sourceBuffer.updating || !mediaSource || mediaSource.readyState !== 'open') {
        return;
    }
    const next = videoChunksQueue.shift();
    if (!next) return;
    try {
        sourceBuffer.appendBuffer(next);
    } catch (e) {
        console.error('Error appending video chunk:', e);
    }
}

function cleanupWebSocketVideo() {
    videoChunksQueue = [];
    if (sourceBuffer && mediaSource) {
        try {
            if (mediaSource.readyState === 'open' && !sourceBuffer.updating) {
                mediaSource.endOfStream();
            }
        } catch (e) {
            console.error('Error ending MediaSource stream:', e);
        }
    }
    sourceBuffer = null;
    mediaSource = null;
    pendingWsVideoElement = null;
    // Assigned in setupWebSocketVideoPlayback so the watchdog/gesture retry can
    // reach it; drop it here or they would poke a torn-down element.
    if (avatarVideoElement) {
        avatarVideoElement = null;
    }
    disarmAvatarGestureRetry();
}

// ===== WebRTC for Avatar =====

// Attach one inbound track to its own media element.
//
// Both peer-connection paths (prewarmed and from-scratch) funnel through here so
// they cannot drift apart.
//
// Each element gets a MediaStream containing ONLY its own track. Previously both
// elements were handed `event.streams[0]`, which carries audio *and* video — so
// the <video> element counted as "autoplay with sound", which browsers refuse
// until the user has interacted with the origin. That is a first-visit-only
// failure by construction: play() is rejected, 'playing' never fires,
// revealAvatarVideo() is never called, and the stage sits on "Connecting…"
// forever. Reloading "fixed" it only because by then the origin had been
// interacted with.
//
// A video-only, muted element satisfies every browser's autoplay policy
// unconditionally, so the face always appears. The voice rides the separate
// <audio> element, which is retried on the next user gesture if it is refused.
function attachAvatarTrack(event) {
    const container = document.getElementById('avatarVideo');
    const isVideo = event.track.kind === 'video';
    const mediaPlayer = document.createElement(event.track.kind);
    mediaPlayer.id = event.track.kind;
    mediaPlayer.srcObject = new MediaStream([event.track]);
    // autoplay=true starts the stream as soon as the first frame decodes (no
    // loadeddata round-trip). playsInline keeps it inline on iOS Safari instead
    // of forcing fullscreen.
    mediaPlayer.autoplay = true;
    mediaPlayer.playsInline = true;
    // The video element carries no audio track, but say so explicitly: it is the
    // documented condition for unconditional autoplay, and it keeps the avatar
    // from ever being double-audible if a future change adds audio to it.
    if (isVideo) mediaPlayer.muted = true;
    if (container) container.appendChild(mediaPlayer);

    if (isVideo) {
        avatarVideoElement = mediaPlayer;
        mediaPlayer.onplaying = () => revealAvatarVideo(mediaPlayer);
    } else {
        avatarAudioElement = mediaPlayer;
        // The analyser drives the "speaking" glow and needs the audio track.
        attachAvatarAudioAnalyser(mediaPlayer.srcObject);
    }
    playAvatarMedia(mediaPlayer, event.track.kind);
}

// Start playback and, if the autoplay policy refuses, arm a one-shot retry on the
// next user gesture instead of failing silently. `autoplay` alone gives us no
// error to react to, which is why the original stall was invisible.
function playAvatarMedia(el, kind) {
    const p = el.play();
    if (!p || typeof p.catch !== 'function') return;
    p.catch((err) => {
        console.warn(`[Avatar] ${kind} playback refused (${err && err.name}); retrying on next user gesture`);
        armAvatarGestureRetry();
    });
}

// One-shot: the first real user interaction satisfies the autoplay policy, so
// replay whatever is still paused. Listeners are capture-phase and passive so
// they never interfere with the click that triggered them.
function armAvatarGestureRetry() {
    if (avatarGestureRetry) return;
    const retry = () => {
        disarmAvatarGestureRetry();
        retryPausedAvatarMedia('user gesture');
    };
    avatarGestureRetry = retry;
    for (const evt of AVATAR_GESTURE_EVENTS) {
        document.addEventListener(evt, retry, { capture: true, passive: true });
    }
}

// Detach the listeners, whether they fired or the session was torn down first.
// Keeping the handler reference (rather than a boolean) is what makes this
// possible — otherwise a teardown mid-arm would leave them attached forever and
// each new session would stack another set.
function disarmAvatarGestureRetry() {
    if (!avatarGestureRetry) return;
    for (const evt of AVATAR_GESTURE_EVENTS) {
        document.removeEventListener(evt, avatarGestureRetry, true);
    }
    avatarGestureRetry = null;
}

// Re-issue play() on any avatar element that is still paused. Safe to call at any
// time — play() on an already-playing element resolves immediately.
function retryPausedAvatarMedia(reason) {
    for (const [el, kind] of [[avatarVideoElement, 'video'], [avatarAudioElement, 'audio']]) {
        if (el && el.paused) {
            console.log(`[Avatar] retrying ${kind} playback (${reason})`);
            const p = el.play();
            if (p && typeof p.catch === 'function') {
                p.catch((err) => console.warn(`[Avatar] ${kind} retry failed: ${err && err.name}`));
            }
        }
    }
}

// Read an ICE candidate's type ("host" / "srflx" / "relay"). Chromium exposes
// `.type` directly; parse the SDP candidate line as a fallback so this works on
// any browser. The line is:
//   candidate:<foundation> <component> <transport> <priority> <ip> <port> typ <type> ...
function iceCandidateType(candidate) {
    if (!candidate) return null;
    if (candidate.type) return candidate.type;
    const parts = String(candidate.candidate || '').split(' ');
    const i = parts.indexOf('typ');
    return i >= 0 ? (parts[i + 1] || null) : null;
}

// A "host" candidate is a private LAN address (192.168.x.x); it can never reach
// the Azure avatar service. Only srflx (NAT-reflexive) or relay (TURN) candidates
// can. Measured live on a loaded machine: an offer sent carrying only a host
// candidate failed exactly like an empty one — iceConnectionState=disconnected ->
// connectionState=failed, avatar never appeared. So "do we have a candidate?" is
// the wrong question; "do we have one that can reach the server?" is the right one.
function isConnectableCandidate(candidate) {
    const t = iceCandidateType(candidate);
    return t === 'srflx' || t === 'relay';
}

// How long to keep gathering after the first TURN relay candidate arrives, so
// sibling candidates (the other media section, additional TURN transports) make
// it into the offer too. A relay candidate alone is enough to CONNECT; the extra
// ones only improve path selection, so this is a short settle rather than a wait.
const ICE_RELAY_SETTLE_MS = 250;
// Backstop for networks where TURN is unreachable and no relay candidate ever
// arrives. Sending whatever we have is better than stalling — if 1.5s wasn't
// enough to reach a TURN server, longer usually isn't either.
const ICE_GATHER_TIMEOUT_MS = 1500;
// Hard ceiling for the case where the backstop fires before any *connectable*
// candidate has been gathered. This app does not trickle ICE — `onicecandidate`
// only ever triggers sending the whole localDescription, individual candidates
// are never sent — so the offer must carry every candidate it will ever have.
// Shipping one with nothing reachable in it produces a session that can never
// connect, so when we have nothing usable, waiting is strictly better than
// sending. This ceiling stops that wait from being unbounded: at this point we
// send whatever exists and let the reveal watchdog surface the failure, which is
// better than hanging on the spinner forever.
const ICE_GATHER_MAX_WAIT_MS = 8000;

// Prepare a peer connection ahead of time so ICE candidates are pre-gathered.
// This avoids the ICE gathering delay when the user starts a new session.
function preparePeerConnection(iceServers) {
    const iceConfig = iceServers.map(s => ({
        urls: s.urls,
        username: s.username || undefined,
        credential: s.credential || undefined,
    }));

    // iceCandidatePoolSize pre-allocates ICE candidates against each STUN/TURN server
    // at PC construction time, so by the time we createOffer() the candidates are
    // mostly gathered. Cuts cold-start gathering latency.
    const pc = new RTCPeerConnection({ iceServers: iceConfig, iceCandidatePoolSize: 4 });
    attachAvatarConnectionMonitor(pc);
    let iceGatheringDone = false;
    let relaySettleTimer = null;
    let connectableCount = 0;
    let awaitingUsableCandidate = false;

    // Queue the connection once it can actually connect, instead of waiting for
    // gathering to fully complete (which routinely takes longer than the backstop
    // and so effectively never won the race).
    const markPrepared = (reason) => {
        if (iceGatheringDone) return;
        iceGatheringDone = true;
        clearTimeout(relaySettleTimer);
        peerConnectionQueue.push(pc);
        console.log('[' + new Date().toISOString() + '] Peer connection prepared (' + reason + ').');
        // Keep only the latest prepared connection
        if (peerConnectionQueue.length > 1) {
            const old = peerConnectionQueue.shift();
            try { old.close(); } catch (e) {}
        }
    };

    // Handle incoming tracks (video and audio)
    pc.ontrack = (event) => attachAvatarTrack(event);

    pc.onicegatheringstatechange = () => {
        if (pc.iceGatheringState === 'complete') {
            // ICE gathering complete
        }
    };

    pc.onicecandidate = (event) => {
        if (!event.candidate) {
            markPrepared('gathering complete');
            return;
        }
        if (isConnectableCandidate(event.candidate)) connectableCount++;
        if (awaitingUsableCandidate) {
            if (connectableCount > 0 && !relaySettleTimer) {
                relaySettleTimer = setTimeout(
                    () => markPrepared('usable candidate after timeout'), ICE_RELAY_SETTLE_MS);
            }
            return;
        }
        if (iceCandidateType(event.candidate) === 'relay' && !relaySettleTimer) {
            relaySettleTimer = setTimeout(
                () => markPrepared('relay candidate ready'), ICE_RELAY_SETTLE_MS);
        }
    };

    // Add transceivers for video and audio
    pc.addTransceiver('video', { direction: 'sendrecv' });
    pc.addTransceiver('audio', { direction: 'sendrecv' });

    // Listen for data channel events
    pc.addEventListener('datachannel', (event) => {
        const dataChannel = event.channel;
        dataChannel.onmessage = (e) => {
            console.log('[' + new Date().toISOString() + '] WebRTC event received: ' + e.data);
            handleAvatarDataChannelMessage(e.data);
        };
        dataChannel.onclose = () => {
            console.log('Data channel closed');
        };
    });
    pc.createDataChannel('eventChannel');

    pc.createOffer().then(offer => {
        return pc.setLocalDescription(offer);
    }).then(() => {
        // Backstop: queue the connection anyway if no relay candidate ever shows
        // up. This is the disconnect-time prewarm; same logic as the first-connect
        // path — see setupWebRTC() for rationale.
        setTimeout(() => {
            if (iceGatheringDone) return;
            if (connectableCount > 0) {
                markPrepared('gathering timed out');
                return;
            }
            // Queuing a connection with nothing reachable just moves the failure
            // to whoever reuses it. Wait for a usable candidate, up to the ceiling.
            awaitingUsableCandidate = true;
            setTimeout(() => markPrepared('no usable candidates'),
                ICE_GATHER_MAX_WAIT_MS - ICE_GATHER_TIMEOUT_MS);
        }, ICE_GATHER_TIMEOUT_MS);
    }).catch(err => {
        console.error('preparePeerConnection offer error', err);
    });
}

function setupWebRTC(iceServers) {
    if (peerConnection) cleanupWebRTC();

    // Cache ICE servers for future peer connection preparation
    cachedIceServers = iceServers;

    // Clear existing video container
    const container = document.getElementById('avatarVideo');
    if (container) container.innerHTML = '';

    if (peerConnectionQueue.length > 0) {
        // Use cached peer connection with pre-gathered ICE candidates
        peerConnection = peerConnectionQueue.shift();
        console.log('[' + new Date().toISOString() + '] Using cached peer connection with pre-gathered ICE candidates.');

        // Send SDP offer immediately (no need to wait for ICE gathering)
        const sdpJson = JSON.stringify(peerConnection.localDescription);
        const sdpBase64 = btoa(sdpJson);
        console.log('[SDP] Sending cached base64 SDP, starts with:', sdpBase64.substring(0, 40));
        ws.send(JSON.stringify({ type: 'avatar_sdp_offer', clientSdp: sdpBase64 }));
        console.log('[WebRTC] Cached SDP offer sent (base64)');

        // Prepare next peer connection for future use
        preparePeerConnection(iceServers);
        return;
    }

    // No cached peer connection available (first connection), create one from scratch
    const iceConfig = iceServers.map(s => ({
        urls: s.urls,
        username: s.username || undefined,
        credential: s.credential || undefined,
    }));

    // See preparePeerConnection() for why iceCandidatePoolSize is set.
    peerConnection = new RTCPeerConnection({ iceServers: iceConfig, iceCandidatePoolSize: 4 });
    attachAvatarConnectionMonitor(peerConnection);

    // Handle incoming tracks (video and audio)
    peerConnection.ontrack = (event) => attachAvatarTrack(event);

    peerConnection.onicegatheringstatechange = () => {
        if (peerConnection.iceGatheringState === 'complete') {
            // ICE gathering complete
        }
    };

    let iceGatheringDone = false;
    let relaySettleTimer = null;
    let connectableCount = 0;
    let awaitingUsableCandidate = false;

    // Send the offer as soon as we hold a candidate that can actually traverse
    // NAT, rather than after a flat wait. Measured on the live app: the first TURN
    // relay candidate arrives ~300ms after gathering starts, while gathering only
    // reports "complete" at ~2.1s — so the old backstop timer fired on EVERY
    // session and cost ~1.2s of visible startup for nothing. The timer is kept as
    // a backstop for networks where TURN is unreachable, which makes this strictly
    // no worse than the previous behaviour.
    const sendOfferOnce = (reason) => {
        if (iceGatheringDone) return;
        iceGatheringDone = true;
        clearTimeout(relaySettleTimer);
        const sdpJson = JSON.stringify(peerConnection.localDescription);
        const sdpBase64 = btoa(sdpJson);
        ws.send(JSON.stringify({ type: 'avatar_sdp_offer', clientSdp: sdpBase64 }));
        console.log('[WebRTC] SDP offer sent (' + reason + ')');
    };

    peerConnection.onicecandidate = (event) => {
        if (!event.candidate) {
            sendOfferOnce('gathering complete');
            return;
        }
        if (isConnectableCandidate(event.candidate)) connectableCount++;
        // Degraded path: the backstop already passed with nothing reachable, so
        // take the first connectable candidate of either kind rather than holding
        // out for a relay. Still settle briefly so a relay arriving just behind an
        // srflx makes it into the same offer.
        if (awaitingUsableCandidate) {
            if (connectableCount > 0 && !relaySettleTimer) {
                relaySettleTimer = setTimeout(
                    () => sendOfferOnce('usable candidate after timeout'), ICE_RELAY_SETTLE_MS);
            }
            return;
        }
        if (iceCandidateType(event.candidate) === 'relay' && !relaySettleTimer) {
            relaySettleTimer = setTimeout(
                () => sendOfferOnce('relay candidate ready'), ICE_RELAY_SETTLE_MS);
        }
    };

    // Add transceivers for video and audio
    peerConnection.addTransceiver('video', { direction: 'sendrecv' });
    peerConnection.addTransceiver('audio', { direction: 'sendrecv' });

    // Listen for data channel events
    peerConnection.addEventListener('datachannel', (event) => {
        const dataChannel = event.channel;
        dataChannel.onmessage = (e) => {
            console.log('[' + new Date().toISOString() + '] WebRTC event received: ' + e.data);
            handleAvatarDataChannelMessage(e.data);
        };
        dataChannel.onclose = () => {
            console.log('Data channel closed');
        };
    });
    peerConnection.createDataChannel('eventChannel');

    peerConnection.createOffer().then(offer => {
        return peerConnection.setLocalDescription(offer);
    }).then(() => {
        // Backstop only — see sendOfferOnce() above for why this should no longer
        // be the path that normally fires.
        setTimeout(() => {
            if (iceGatheringDone) return;
            if (connectableCount > 0) {
                sendOfferOnce('gathering timed out');
                return;
            }
            // Nothing reachable gathered yet. An offer now could never connect
            // (no trickle — see ICE_GATHER_MAX_WAIT_MS), so wait for a usable
            // candidate instead of shipping a guaranteed-dead session.
            awaitingUsableCandidate = true;
            setTimeout(() => sendOfferOnce('no usable candidates'),
                ICE_GATHER_MAX_WAIT_MS - ICE_GATHER_TIMEOUT_MS);
        }, ICE_GATHER_TIMEOUT_MS);
    }).catch(err => {
        console.error('WebRTC offer error', err);
        addMessage('system', 'WebRTC setup failed');
    });

    // Prepare a peer connection for future use
    preparePeerConnection(iceServers);
}

function handleAvatarSdpAnswer(serverSdpBase64) {
    if (!peerConnection || !serverSdpBase64) return;
    try {
        // Server SDP is base64-encoded JSON: {"type":"answer","sdp":"..."}
        const serverSdpJson = atob(serverSdpBase64);
        const serverSdpObj = JSON.parse(serverSdpJson);
        peerConnection.setRemoteDescription(new RTCSessionDescription(serverSdpObj)).then(() => {
            console.log('[WebRTC] Remote SDP set');
        }).catch(err => {
            // A failed setRemoteDescription leaves the avatar permanently frozen
            // with no video. Surface it instead of swallowing it silently.
            console.error('SDP answer error', err);
            notifySystem('The avatar could not start (connection negotiation failed). You can keep talking with audio, or reconnect to retry the avatar.', 'warning');
        });
    } catch (e) {
        console.error('Failed to parse server SDP', e);
        notifySystem('The avatar could not start (invalid connection answer). You can keep talking with audio, or reconnect to retry the avatar.', 'warning');
    }
}

// Watch the peer connection and surface a terminal WebRTC failure to the user.
// Without this, a dropped/failed ICE negotiation just leaves the avatar frozen
// with no feedback ("sometimes unresponsive"). Attached to every PC we build.
function attachAvatarConnectionMonitor(pc) {
    if (!pc || pc._avatarMonitorAttached) return;
    pc._avatarMonitorAttached = true;
    let notified = false;
    const onFail = (label) => {
        if (notified) return;
        notified = true;
        console.error('[WebRTC] avatar connection ' + label);
        notifySystem('The avatar connection dropped. You can keep talking with audio, or reconnect to retry the avatar.', 'warning');
    };
    pc.addEventListener('iceconnectionstatechange', () => {
        if (pc.iceConnectionState === 'failed') onFail('iceConnectionState=failed');
    });
    pc.addEventListener('connectionstatechange', () => {
        if (pc.connectionState === 'failed') onFail('connectionState=failed');
    });
}

function cleanupWebRTC() {
    stopAvatarAudioAnalyser();
    if (peerConnection) {
        try { peerConnection.close(); } catch (e) {}
        peerConnection = null;
    }
    if (avatarVideoElement) {
        avatarVideoElement.srcObject = null;
        avatarVideoElement = null;
    }
    if (avatarAudioElement) {
        avatarAudioElement.srcObject = null;
        avatarAudioElement = null;
    }
    disarmAvatarGestureRetry();
    const container = document.getElementById('avatarVideo');
    if (container) container.innerHTML = '';
}

// ===== Mic Toggle =====
// Mute/unmute at the MediaStreamTrack level rather than by starting/stopping
// the capture stream. A disabled track stays live and keeps feeding *silence*
// to the worklet, so the server VAD always sees a continuous stream and can
// close any in-flight turn. Cutting the stream instead (the old approach)
// orphaned the VAD when the mic was toggled mid-utterance and hung the turn
// ("voice won't go down"). Privacy is preserved: a disabled track zeroes the
// samples, so the user's real audio never leaves the browser while muted.
function applyMicMuteState() {
    if (!mediaStream) return;
    mediaStream.getAudioTracks().forEach(t => { t.enabled = isRecording; });
}

function toggleMicrophone() {
    if (!isConnected) return;
    isRecording = !isRecording;
    applyMicMuteState();
    updateMicUI();
    // Turning the mic on counts as a real interaction — retire the onboarding hint.
    if (isRecording) dismissOnboarding();
    // Start/stop volume animation based on mic state
    if (isRecording && micAnalyserNode) {
        analyserNode = micAnalyserNode;
        analyserDataArray = micAnalyserDataArray;
        startVolumeAnimation('record');
    } else if (!isRecording) {
        stopRecordAnimation();
        resetVolumeCircle();
    }
}

// ===== Connection / permission status (normal mode) =====
// Single source of truth for the normal-mode status pill (#stageStatus). The
// avatar spinner already covers "connecting", so this surfaces the states the
// user would otherwise never see in production (chat is hidden): reconnecting,
// session ended, and microphone permission problems. In the 'ended' state the
// pill is clickable to restart the session.
function setConnectionState(state) {
    connectionState = state || null;
    const el = document.getElementById('stageStatus');
    const live = document.getElementById('stageStatusLive');
    if (!el) return;

    const map = {
        reconnecting: { text: 'Reconnecting…', cls: 'warning', clickable: false },
        ended: { text: 'Session ended — tap to restart', cls: '', clickable: true },
        'mic-blocked': { text: 'Microphone blocked — allow mic access in your browser, then tap to retry', cls: 'error', clickable: true },
        'mic-missing': { text: 'No microphone found — connect one, then tap to retry', cls: 'error', clickable: true },
        error: { text: 'Something went wrong — tap to restart', cls: 'error', clickable: true },
    };
    const cfg = map[connectionState];

    // Developer mode keeps its own chat-based feedback; don't overlay the pill.
    if (!cfg || isDeveloperMode) {
        el.classList.add('hidden');
        el.classList.remove('clickable', 'warning', 'error');
        if (live) live.textContent = '';
        el.onclick = null;
        return;
    }

    el.textContent = cfg.text;
    if (live) live.textContent = cfg.text;
    el.classList.remove('warning', 'error', 'clickable');
    if (cfg.cls) el.classList.add(cfg.cls);
    el.classList.toggle('clickable', cfg.clickable);
    el.classList.remove('hidden');

    el.onclick = null;
    if (cfg.clickable) {
        const action = connectionState;
        el.onclick = () => {
            setConnectionState(null);
            if (action === 'mic-blocked' || action === 'mic-missing') {
                // Session is still up; just retry capturing the mic.
                if (isConnected) { startAudioCapture(); return; }
            }
            // 'ended' / 'error' (or mic retry with no session): restart.
            if (!isConnected && !isConnecting) {
                connectSession().catch(err => console.error('Restart failed', err));
            }
        };
    }
}

// Surface a system message so it's visible in BOTH modes: the chat line (dev)
// plus a toast in normal mode where the chat area is hidden.
function notifySystem(message, type = 'error') {
    addMessage('system', message);
    if (!isDeveloperMode) showToast(message, type, 6000);
}

// ===== Send Text =====
// Shared text path used by the dev text input, the stage composer, and the
// suggested-prompt chips. Returns true if the message was actually sent.
function sendQuery(text) {
    const q = (text || '').trim();
    if (!q || !isConnected || !ws || ws.readyState !== WebSocket.OPEN) return false;
    addMessage('user', q);
    ws.send(JSON.stringify({ type: 'send_text', text: q }));
    return true;
}

function sendTextMessage() {
    const input = document.getElementById('textInput');
    if (sendQuery(input.value)) {
        input.value = '';
        dismissOnboarding();
    }
}

// Bottom-center stage composer (normal/production mode). Reuses the shared
// sendQuery() path so there is no duplicate send logic.
function sendStageMessage() {
    const input = document.getElementById('stageTextInput');
    if (!input) return;
    if (sendQuery(input.value)) {
        input.value = '';
        dismissOnboarding();
    }
}

// ===== Speech Events (sound wave animation) =====
function onSpeechStarted(itemId) {
    isSpeaking = true;
    console.log(`[Turn] speech_started item=${itemId} | mic chunks sent so far=${audioChunksSent} | isRecording=${isRecording}`);
    // The user appears to be talking — tuck the onboarding hint away. This is
    // only a temporary hide (a stray VAD start may yield an empty transcript);
    // permanent dismissal happens on a non-empty user transcript_done.
    hideOnboardingTemporarily();
    // NOTE: deliberately do NOT tear down the thinking pill here. A stray VAD
    // speech_started during the grounding gap would otherwise cancel a
    // legitimate pill. Real barge-in is handled by response_done (status
    // CANCELLED), which calls stopThinking().
    // Stop assistant audio playback (barge-in) in speech-only mode if barge-in is enabled
    const bargeInEnabled = document.getElementById('enableBargeIn')?.checked || false;
    if (bargeInEnabled) {
        stopAudioPlayback();
    }
    // Add user placeholder message (will be updated when transcription completes)
    if (itemId) {
        const contentDiv = addMessage('user', '...');
        if (contentDiv) {
            contentDiv.closest('.message').setAttribute('data-item-id', itemId);
        }
    }
}

function onSpeechStopped() {
    console.log(`[Turn] speech_stopped | mic chunks sent so far=${audioChunksSent}`);
    pendingAssistantText = '';
    isSpeaking = false;
}

// A speech segment produced no recognized words (empty transcript). Remove the
// dangling "..." placeholder so the user doesn't perceive it as the avatar
// going silent. Surface a brief toast so the failure is visible, not mysterious.
function onTranscriptEmpty(itemId) {
    console.warn(`[Turn] transcript EMPTY item=${itemId} | mic chunks sent so far=${audioChunksSent}`);
    if (itemId) {
        const msg = document.querySelector(`.message.user[data-item-id="${itemId}"]`);
        if (msg) msg.remove();
    }
    // The speech segment produced nothing — if the user hasn't really
    // interacted yet, bring the onboarding hint back.
    maybeRestoreOnboarding();
    showToast("Didn't catch that — please try again.", 'warning', 2500);
}

// ===== Utilities =====
function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let i = 0; i < bytes.byteLength; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
}

function base64ToArrayBuffer(base64) {
    const binary = atob(base64);
    const len = binary.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    return bytes.buffer;
}

// ===== Toast Notification System =====
function showToast(message, type = 'info', duration = 5000) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    // Icon selection
    let icon = '';
    if (type === 'success') icon = '✅';
    else if (type === 'error') icon = '❌';
    else if (type === 'warning') icon = '⚠️';
    else icon = 'ℹ️';

    toast.innerHTML = `<span style="flex-shrink: 0;">${icon}</span> <span style="line-height: 1.4;">${message}</span>`;
    container.appendChild(toast);

    // Force reflow and add visible class
    setTimeout(() => toast.classList.add('visible'), 10);

    // Auto remove
    setTimeout(() => {
        toast.classList.remove('visible');
        setTimeout(() => toast.remove(), 400);
    }, duration);
}

// Note: we used to pre-warm an RTCPeerConnection against Google's public STUN
// at page-load time, on the theory that having ICE candidates pre-gathered
// would speed up the first Connect. In practice that hurt as often as it helped:
// the cached PC's local candidates are gathered against `stun.l.google.com`,
// but the real ICE servers returned by Voice Live usually include Azure TURN
// credentials the prewarm PC never saw — on restrictive NATs the cached PC
// then fails to find a usable path and we fall back to a longer ICE timeout.
//
// The reconnect path (cleanupWebRTC -> preparePeerConnection(cachedIceServers))
// still pre-warms a PC against the REAL Azure ICE servers right after disconnect,
// which is a real win and is kept.
