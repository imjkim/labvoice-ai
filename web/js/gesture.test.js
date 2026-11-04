// Run from web/: node --test    (Node 18+, no dependencies)
import assert from "node:assert/strict";
import { test } from "node:test";

import { GestureDebouncer, classifyHand, detectGesture, isIndexUp, isStopGesture, thumbSignal } from "./gesture.js";

// A synthetic right hand in normalized image coordinates (x right, y down), wrist at the bottom.
// Each finger: [mcp, pip, dip, tip]. "up" fingers are straight and vertical; "folded" fingers
// curl back toward the palm, so the tip ends up closer to the wrist than the middle joint.
const WRIST = { x: 0.5, y: 0.9 };
const FINGER_X = { index: 0.44, middle: 0.5, ring: 0.56, pinky: 0.61 };

function finger(x, pose) {
  if (pose === "up") return [{ x, y: 0.7 }, { x, y: 0.6 }, { x, y: 0.53 }, { x, y: 0.47 }];
  if (pose === "folded") return [{ x, y: 0.7 }, { x, y: 0.62 }, { x, y: 0.68 }, { x, y: 0.74 }];
  if (pose === "down") return [{ x, y: 0.7 }, { x, y: 0.8 }, { x, y: 0.87 }, { x, y: 0.93 }];
  throw new Error(pose);
}

/** Index finger pointing straight out from its knuckle at `deg` from vertical (+ = right). */
function tiltedIndex(deg) {
  const base = { x: FINGER_X.index, y: 0.7 };
  const r = (deg * Math.PI) / 180;
  return [0.1, 0.17, 0.23].reduce(
    (pts, len) => [...pts, { x: base.x + Math.sin(r) * len, y: base.y - Math.cos(r) * len }],
    [base],
  );
}

// Thumb poses: [cmc, mcp, ip, tip]. "tucked" curls across the palm; "up" sticks straight up past
// the folded fingers (a thumbs-up fist); "side" points straight out to the left.
const THUMBS = {
  rest: [{ x: 0.42, y: 0.85 }, { x: 0.38, y: 0.8 }, { x: 0.36, y: 0.76 }, { x: 0.35, y: 0.72 }],
  tucked: [{ x: 0.44, y: 0.85 }, { x: 0.41, y: 0.79 }, { x: 0.44, y: 0.74 }, { x: 0.48, y: 0.72 }],
  up: [{ x: 0.43, y: 0.84 }, { x: 0.41, y: 0.72 }, { x: 0.405, y: 0.6 }, { x: 0.4, y: 0.5 }],
  side: [{ x: 0.43, y: 0.84 }, { x: 0.36, y: 0.8 }, { x: 0.27, y: 0.8 }, { x: 0.19, y: 0.8 }],
};

function hand({ index = "up", middle = "folded", ring = "folded", pinky = "folded", indexPts, thumb: t = "rest" } = {}) {
  const thumb = THUMBS[t];
  return [
    WRIST,
    ...thumb,
    ...(indexPts || finger(FINGER_X.index, index)),
    ...finger(FINGER_X.middle, middle),
    ...finger(FINGER_X.ring, ring),
    ...finger(FINGER_X.pinky, pinky),
  ];
}

test("a raised index finger with the others folded counts", () => {
  assert.equal(hand().length, 21);
  assert.equal(isIndexUp(hand()), true);
});

test("open palm, fist, and two fingers up do not count", () => {
  assert.equal(isIndexUp(hand({ middle: "up", ring: "up", pinky: "up" })), false, "open palm");
  assert.equal(isIndexUp(hand({ index: "folded" })), false, "fist");
  assert.equal(isIndexUp(hand({ middle: "up" })), false, "peace sign");
});

test("the finger has to point up, not sideways or down", () => {
  assert.equal(isIndexUp(hand({ indexPts: tiltedIndex(20) })), true, "a natural lean is fine");
  assert.equal(isIndexUp(hand({ indexPts: tiltedIndex(-30) })), true, "either direction");
  assert.equal(isIndexUp(hand({ indexPts: tiltedIndex(60) })), false, "pointing across the room");
  assert.equal(isIndexUp(hand({ indexPts: tiltedIndex(90) })), false, "pointing sideways");
  assert.equal(isIndexUp(hand({ index: "down" })), false, "pointing down");
});

test("angles are measured in real proportions, not stretched image coordinates", () => {
  // 30 degrees in normalized coordinates is about 38 degrees on a 4:3 frame: over the 35 limit.
  const lean = hand({ indexPts: tiltedIndex(30) });
  assert.equal(isIndexUp(lean, { aspect: 1 }), true);
  assert.equal(isIndexUp(lean, { aspect: 4 / 3 }), false);
});

test("missing or partial hands are never a gesture", () => {
  assert.equal(isIndexUp(undefined), false);
  assert.equal(isIndexUp([]), false);
  assert.equal(isIndexUp(hand().slice(0, 20)), false);
});

test("either MediaPipe's confident Pointing_Up or the landmark check triggers a stop", () => {
  const pointing = (score) => ({ gestures: [[{ categoryName: "Pointing_Up", score }]], landmarks: [] });
  assert.equal(isStopGesture(pointing(0.8)), true);
  assert.equal(isStopGesture(pointing(0.3)), false, "low confidence alone is not enough");
  assert.equal(isStopGesture({ gestures: [[{ categoryName: "None", score: 0.9 }]], landmarks: [hand()] }), true);
  assert.equal(
    isStopGesture({ gestures: [[{ categoryName: "Open_Palm", score: 0.9 }]], landmarks: [hand({ middle: "up", ring: "up", pinky: "up" })] }),
    false,
  );
  assert.equal(isStopGesture({ gestures: [], landmarks: [] }), false, "no hands");
  assert.equal(isStopGesture(undefined), false);
  const twoHands = { gestures: [[{ categoryName: "Closed_Fist", score: 0.9 }], [{ categoryName: "None", score: 0.5 }]], landmarks: [hand({ index: "folded" }), hand()] };
  assert.equal(isStopGesture(twoHands), true, "the second hand counts too");
});

/** Mirror a hand top to bottom: a thumbs-up fist becomes a thumbs-down fist. */
const flip = (landmarks) => landmarks.map((l) => ({ x: l.x, y: 1.5 - l.y }));

test("a fist with the thumb up or down is a thumbs signal", () => {
  const up = hand({ index: "folded", thumb: "up" });
  assert.equal(thumbSignal(up), "thumb_up");
  assert.equal(thumbSignal(flip(up)), "thumb_down");
  assert.equal(classifyHand(up), "thumb_up");
  assert.equal(classifyHand(flip(up)), "thumb_down");
});

test("a tucked or sideways thumb, an open hand, or a raised index finger is not a thumbs signal", () => {
  assert.equal(thumbSignal(hand({ index: "folded", thumb: "tucked" })), null, "plain fist");
  assert.equal(thumbSignal(hand({ index: "folded", thumb: "side" })), null, "thumb pointing sideways");
  assert.equal(thumbSignal(hand({ thumb: "up", middle: "up", ring: "up", pinky: "up" })), null, "open hand");
  assert.equal(thumbSignal(hand({ thumb: "up" })), null, "index up too: that's not a fist");
  assert.equal(classifyHand(hand()), "index_up");
  assert.equal(classifyHand(hand({ index: "folded", thumb: "tucked" })), null);
});

test("MediaPipe's confident labels map to the three signals; otherwise the landmarks decide", () => {
  const label = (categoryName, score, landmarks = []) => ({ gestures: [[{ categoryName, score }]], landmarks });
  assert.equal(detectGesture(label("Thumb_Down", 0.8)), "thumb_down");
  assert.equal(detectGesture(label("Thumb_Up", 0.8)), "thumb_up");
  assert.equal(detectGesture(label("Pointing_Up", 0.8)), "index_up");
  assert.equal(detectGesture(label("Thumb_Up", 0.3)), null, "unsure and no landmarks");
  assert.equal(detectGesture(label("None", 0.9, [flip(hand({ index: "folded", thumb: "up" }))])), "thumb_down");
  assert.equal(detectGesture(label("Victory", 0.9, [hand({ middle: "up" })])), null);
  assert.equal(isStopGesture(label("Thumb_Up", 0.9)), false, "a thumbs up doesn't stop speech");
});

test("each signal must be held, then fires once per gesture", () => {
  const d = new GestureDebouncer({ holdMs: { index_up: 200, thumb_up: 350, thumb_down: 350 }, cooldownMs: 1000 });
  assert.equal(d.push("index_up", 0), null);
  assert.equal(d.push("index_up", 150), null, "not held long enough yet");
  assert.equal(d.push("index_up", 210), "index_up", "fires once held");
  assert.equal(d.push("index_up", 400), null, "does not repeat while still held");
  assert.equal(d.push(null, 500), null);
  assert.equal(d.push("index_up", 600), null);
  assert.equal(d.push("index_up", 900), null, "raised again, but inside the cooldown");
  assert.equal(d.push(null, 1000), null);
  assert.equal(d.push("index_up", 1300), null);
  assert.equal(d.push("index_up", 1520), "index_up", "raised again after the cooldown");
});

test("thumbs need a longer hold, and switching signals restarts it", () => {
  const d = new GestureDebouncer({ holdMs: { index_up: 200, thumb_up: 350, thumb_down: 350 }, cooldownMs: 0 });
  d.push("thumb_down", 0);
  assert.equal(d.push("thumb_down", 250), null, "a fist on its way somewhere else doesn't mute");
  assert.equal(d.push("thumb_down", 360), "thumb_down");
  assert.equal(d.push("thumb_up", 400), null, "switched: the hold starts over");
  assert.equal(d.push("thumb_up", 760), "thumb_up");
});

test("a single flickering frame resets the hold", () => {
  const d = new GestureDebouncer({ holdMs: 200, cooldownMs: 0 });
  d.push("index_up", 0);
  d.push("index_up", 150);
  d.push(null, 180); // detection dropped for a frame
  assert.equal(d.push("index_up", 210), null, "the hold starts over");
  assert.equal(d.push("index_up", 420), "index_up");
});
