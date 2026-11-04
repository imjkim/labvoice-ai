// Hand signals, read from a camera in the browser:
//   index_up     index finger raised, other fingers folded   -> stop talking, listen
//   thumb_down   fist with the thumb pointing down           -> stop listening
//   thumb_up     fist with the thumb pointing up             -> listen again
//
// The camera runs MediaPipe's hand Gesture Recognizer. Video frames never leave this page: only
// the name of a signal reaches the app. A frame shows a signal if MediaPipe's own classifier is
// confident ("Pointing_Up", "Thumb_Up", "Thumb_Down") or if our geometric check on the 21 hand
// landmarks agrees, and a signal must be held briefly before it fires.
// The pure functions here (classifyHand, detectGesture, GestureDebouncer) are unit-tested in
// Node; GestureWatcher is the browser glue around them.

const MP_VERSION = "0.10.14";
const MP_BASE = `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${MP_VERSION}`;
const MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task";

// Landmark indices (MediaPipe hand model): 0 wrist; each finger is MCP, PIP, DIP, tip.
const WRIST = 0;
const INDEX = { mcp: 5, pip: 6, tip: 8 };
const OTHERS = [
  { pip: 10, tip: 12 }, // middle
  { pip: 14, tip: 16 }, // ring
  { pip: 18, tip: 20 }, // pinky
];
const THUMB = { mcp: 2, ip: 3, tip: 4 };

export const SIGNALS = ["index_up", "thumb_up", "thumb_down"];
const CANNED = { Pointing_Up: "index_up", Thumb_Up: "thumb_up", Thumb_Down: "thumb_down" };

export const GESTURE_DEFAULTS = {
  extendedRatio: 1.15, // tip at least this much farther from the wrist than the PIP joint
  foldedRatio: 1.0, //    tip no farther from the wrist than the PIP joint
  maxTiltDeg: 35, //      how far from vertical the index finger may lean
  maxThumbTiltDeg: 45, // how far from vertical a thumbs-up or thumbs-down may lean
  thumbRatio: 1.1, //     thumb tip at least this much farther from the wrist than its middle joint
  minScore: 0.6, //       MediaPipe confidence that counts on its own
  // How long each signal must be held. Thumbs take a little longer: making a fist on the way to
  // another gesture shouldn't mute the microphone.
  holdMs: { index_up: 200, thumb_up: 350, thumb_down: 350 },
  cooldownMs: 1200, //    minimum time between two firings of the same signal
};

/**
 * True when the hand shows one raised index finger: index extended and pointing up, middle,
 * ring and pinky folded. The thumb is ignored (people hold it every which way).
 * Landmarks are MediaPipe's normalized image coordinates (x right, y down, 0..1). `aspect` is
 * the video's width / height, so angles and distances are measured in real proportions.
 */
export function isIndexUp(landmarks, { aspect = 1, ...opts } = {}) {
  const o = { ...GESTURE_DEFAULTS, ...opts };
  if (!Array.isArray(landmarks) || landmarks.length < 21) return false;
  const p = (i) => ({ x: landmarks[i].x * aspect, y: landmarks[i].y });
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const wrist = p(WRIST);
  const reach = (f) => dist(wrist, p(f.tip)) / (dist(wrist, p(f.pip)) || 1e-6);

  if (reach(INDEX) < o.extendedRatio) return false;
  if (!OTHERS.every((f) => reach(f) <= o.foldedRatio)) return false;

  const base = p(INDEX.mcp);
  const tip = p(INDEX.tip);
  const up = base.y - tip.y; // positive when the tip is above the knuckle
  if (up <= 0) return false;
  const tiltDeg = (Math.atan2(Math.abs(tip.x - base.x), up) * 180) / Math.PI;
  return tiltDeg <= o.maxTiltDeg;
}

/**
 * "thumb_up" or "thumb_down" for a fist with the thumb out, else null: all four fingers folded,
 * the thumb straight, within 45 degrees of vertical, and its tip the highest (or lowest) point of
 * the hand. Rotation-invariant ratios, so a sideways fist still counts as a fist.
 */
export function thumbSignal(landmarks, { aspect = 1, ...opts } = {}) {
  const o = { ...GESTURE_DEFAULTS, ...opts };
  if (!Array.isArray(landmarks) || landmarks.length < 21) return null;
  const p = (i) => ({ x: landmarks[i].x * aspect, y: landmarks[i].y });
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const wrist = p(WRIST);
  const reach = (f) => dist(wrist, p(f.tip)) / (dist(wrist, p(f.pip)) || 1e-6);

  if (![INDEX, ...OTHERS].every((f) => reach(f) <= o.foldedRatio)) return null;
  if (dist(wrist, p(THUMB.tip)) < dist(wrist, p(THUMB.ip)) * o.thumbRatio) return null; // tucked thumb

  const base = p(THUMB.mcp);
  const tip = p(THUMB.tip);
  const dy = tip.y - base.y; // + when the thumb points down the image
  const tiltDeg = (Math.atan2(Math.abs(tip.x - base.x), Math.abs(dy)) * 180) / Math.PI;
  if (tiltDeg > o.maxThumbTiltDeg) return null;

  const ys = landmarks.map((l) => l.y);
  if (dy < 0 && tip.y <= Math.min(...ys)) return "thumb_up";
  if (dy > 0 && tip.y >= Math.max(...ys)) return "thumb_down";
  return null;
}

/** The signal one hand shows by its landmarks alone: "index_up", "thumb_up", "thumb_down" or null. */
export function classifyHand(landmarks, opts = {}) {
  if (isIndexUp(landmarks, opts)) return "index_up";
  return thumbSignal(landmarks, opts);
}

/**
 * The signal in a GestureRecognizer result, or null. Per hand, MediaPipe's own label wins when it
 * is confident; otherwise our landmark check decides. The first hand showing a signal counts.
 */
export function detectGesture(result, { aspect = 1, ...opts } = {}) {
  const o = { ...GESTURE_DEFAULTS, ...opts };
  const hands = Math.max(result?.landmarks?.length ?? 0, result?.gestures?.length ?? 0);
  for (let i = 0; i < hands; i++) {
    const top = result.gestures?.[i]?.[0];
    if (top && CANNED[top.categoryName] && top.score >= o.minScore) return CANNED[top.categoryName];
    const mine = classifyHand(result.landmarks?.[i], { aspect, ...opts });
    if (mine) return mine;
  }
  return null;
}

/** Does any hand show a raised index finger? */
export function isStopGesture(result, opts = {}) {
  return detectGesture(result, opts) === "index_up";
}

/**
 * Turns a noisy per-frame signal into one event per gesture: a signal must be held for its
 * `holdMs`, fires once per gesture (drop your hand or change signal to re-arm), and the same
 * signal never fires twice within `cooldownMs`. Switching signals restarts the hold. Time-based
 * rather than frame-based, because the frame rate varies.
 */
export class GestureDebouncer {
  constructor({ holdMs = GESTURE_DEFAULTS.holdMs, cooldownMs = GESTURE_DEFAULTS.cooldownMs } = {}) {
    this.holdMs = typeof holdMs === "number" ? Object.fromEntries(SIGNALS.map((s) => [s, holdMs])) : holdMs;
    this.cooldownMs = cooldownMs;
    this.lastFiredAt = {};
    this.reset();
  }

  /** Feed one frame's signal (or null); returns the signal on the frame it fires, else null. */
  push(signal, nowMs) {
    if (signal !== this.current) {
      this.current = signal || null;
      this.since = signal ? nowMs : null;
      this.fired = false;
    }
    if (!signal || this.fired) return null;
    if (nowMs - this.since < (this.holdMs[signal] ?? 200)) return null;
    if (nowMs - (this.lastFiredAt[signal] ?? -Infinity) < this.cooldownMs) return null;
    this.fired = true;
    this.lastFiredAt[signal] = nowMs;
    return signal;
  }

  reset() {
    this.current = null;
    this.since = null;
    this.fired = false;
  }
}

let recognizerPromise = null;

/** Load MediaPipe and the gesture model once per page (about 8 MB, cached by the browser). */
function loadRecognizer() {
  recognizerPromise ??= (async () => {
    const { FilesetResolver, GestureRecognizer } = await import(`${MP_BASE}/vision_bundle.mjs`);
    const files = await FilesetResolver.forVisionTasks(`${MP_BASE}/wasm`);
    const create = (delegate) =>
      GestureRecognizer.createFromOptions(files, {
        baseOptions: { modelAssetPath: MODEL_URL, delegate },
        runningMode: "VIDEO",
        numHands: 2,
      });
    try {
      return await create("GPU");
    } catch {
      return await create("CPU"); // no WebGL2 / GPU blocked: slower, still fine at 15 fps
    }
  })().catch((err) => {
    recognizerPromise = null; // let the next attempt retry (e.g. after the network comes back)
    throw err;
  });
  return recognizerPromise;
}

export class GestureWatcher {
  /**
   * callbacks:
   *   onGesture(signal)           "index_up" | "thumb_up" | "thumb_down", held long enough
   *   onState({ hand, signal })   whether a hand is in view, and the signal it shows right now
   */
  constructor(callbacks = {}, { fps = 15 } = {}) {
    this.cb = callbacks;
    this.frameMs = 1000 / fps;
    this.debounce = new GestureDebouncer();
    this.stream = null;
    this.video = null;
    this.recognizer = null;
    this.raf = 0;
    this.lastRun = 0;
    this.lastVideoTime = -1;
    this.last = { hand: false, signal: null };
  }

  get supported() {
    return Boolean(navigator.mediaDevices?.getUserMedia);
  }

  get active() {
    return Boolean(this.stream);
  }

  /** Start the camera and watch for the gesture. Shows the feed in `video` (muted, inline). */
  async start(video) {
    if (this.stream) return;
    // Open the camera and load the model in parallel; if either fails, don't leave the camera on.
    const [cam, model] = await Promise.allSettled([
      navigator.mediaDevices.getUserMedia({
        // 720p keeps the enlarged preview sharp; the recognizer scales frames down itself.
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" },
        audio: false,
      }),
      loadRecognizer(),
    ]);
    if (cam.status === "rejected" || model.status === "rejected") {
      if (cam.status === "fulfilled") cam.value.getTracks().forEach((t) => t.stop());
      if (cam.status === "rejected") throw cam.reason;
      throw Object.assign(new Error(model.reason?.message || String(model.reason)), { name: "ModelLoadError" });
    }
    const [stream, recognizer] = [cam.value, model.value];
    this.stream = stream;
    this.recognizer = recognizer;
    this.video = video;
    video.srcObject = stream;
    video.muted = true;
    video.playsInline = true;
    await video.play();
    this.debounce.reset();
    this.lastVideoTime = -1;
    this._tick();
  }

  stop() {
    cancelAnimationFrame(this.raf);
    this.stream?.getTracks().forEach((t) => t.stop()); // turns off the camera light
    if (this.video) this.video.srcObject = null;
    this.stream = this.video = null;
    this._report({ hand: false, signal: null });
  }

  _tick = () => {
    if (!this.stream) return;
    this.raf = requestAnimationFrame(this._tick);
    const now = performance.now();
    const v = this.video;
    if (now - this.lastRun < this.frameMs || v.readyState < 2 || v.currentTime === this.lastVideoTime) return;
    this.lastRun = now;
    this.lastVideoTime = v.currentTime;

    let result;
    try {
      result = this.recognizer.recognizeForVideo(v, now);
    } catch {
      return; // a dropped frame is harmless; the next one will do
    }
    const aspect = v.videoWidth && v.videoHeight ? v.videoWidth / v.videoHeight : 1;
    const signal = detectGesture(result, { aspect });
    this._report({ hand: (result.landmarks?.length ?? 0) > 0, signal });
    const fired = this.debounce.push(signal, now);
    if (fired) this.cb.onGesture?.(fired);
  };

  _report(next) {
    if (next.hand === this.last.hand && next.signal === this.last.signal) return;
    this.last = next;
    this.cb.onState?.(next);
  }
}
