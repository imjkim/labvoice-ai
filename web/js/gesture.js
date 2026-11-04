// Raise an index finger to stop the assistant mid-answer.
//
// The camera runs MediaPipe's hand Gesture Recognizer in the browser. Video frames never leave
// this page: only the decision "a finger is up" reaches the app. Two signals count as a raised
// finger, and it must be held briefly before it fires:
//   - MediaPipe's own "Pointing_Up" gesture, when it's confident;
//   - our geometric check on the 21 hand landmarks (isIndexUp), which also covers hands the
//     classifier is unsure about.
// The pure functions here (isIndexUp, isStopGesture, GestureDebouncer) are unit-tested in Node;
// GestureWatcher is the browser glue around them.

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

export const GESTURE_DEFAULTS = {
  extendedRatio: 1.15, // tip at least this much farther from the wrist than the PIP joint
  foldedRatio: 1.0, //    tip no farther from the wrist than the PIP joint
  maxTiltDeg: 35, //      how far from vertical the index finger may lean
  minScore: 0.6, //       MediaPipe "Pointing_Up" confidence that counts on its own
  holdMs: 200, //         how long the finger must stay up before it fires
  cooldownMs: 1200, //    minimum time between two stops
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

/** Does any hand in a GestureRecognizer result show a raised index finger? */
export function isStopGesture(result, { aspect = 1, ...opts } = {}) {
  const o = { ...GESTURE_DEFAULTS, ...opts };
  const hands = Math.max(result?.landmarks?.length ?? 0, result?.gestures?.length ?? 0);
  for (let i = 0; i < hands; i++) {
    const top = result.gestures?.[i]?.[0];
    if (top?.categoryName === "Pointing_Up" && top.score >= o.minScore) return true;
    if (isIndexUp(result.landmarks?.[i], { aspect, ...opts })) return true;
  }
  return false;
}

/**
 * Turns a noisy per-frame yes/no into one event per raise: the finger must stay up for
 * `holdMs`, it fires once per raise (lower your hand to re-arm), and never twice within
 * `cooldownMs`. Time-based rather than frame-based, because the frame rate varies.
 */
export class GestureDebouncer {
  constructor({ holdMs = GESTURE_DEFAULTS.holdMs, cooldownMs = GESTURE_DEFAULTS.cooldownMs } = {}) {
    this.holdMs = holdMs;
    this.cooldownMs = cooldownMs;
    this.since = null;
    this.fired = false;
    this.lastFiredAt = -Infinity;
  }

  /** Feed one frame's decision; returns true on the frame the gesture fires. */
  push(active, nowMs) {
    if (!active) {
      this.since = null;
      this.fired = false;
      return false;
    }
    if (this.since === null) this.since = nowMs;
    if (this.fired || nowMs - this.since < this.holdMs || nowMs - this.lastFiredAt < this.cooldownMs) return false;
    this.fired = true;
    this.lastFiredAt = nowMs;
    return true;
  }

  reset() {
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
   *   onGesture()               a raised index finger was held long enough
   *   onState({ hand, up })     whether a hand is in view and whether it shows the gesture
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
    this.last = { hand: false, up: false };
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
        video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: "user" },
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
    this._report({ hand: false, up: false });
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
    const up = isStopGesture(result, { aspect });
    this._report({ hand: (result.landmarks?.length ?? 0) > 0, up });
    if (this.debounce.push(up, now)) this.cb.onGesture?.();
  };

  _report(next) {
    if (next.hand === this.last.hand && next.up === this.last.up) return;
    this.last = next;
    this.cb.onState?.(next);
  }
}
