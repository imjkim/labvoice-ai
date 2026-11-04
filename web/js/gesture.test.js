// Run from web/: node --test    (Node 18+, no dependencies)
import assert from "node:assert/strict";
import { test } from "node:test";

import { GestureDebouncer, isIndexUp, isStopGesture } from "./gesture.js";

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

function hand({ index = "up", middle = "folded", ring = "folded", pinky = "folded", indexPts } = {}) {
  const thumb = [{ x: 0.42, y: 0.85 }, { x: 0.38, y: 0.8 }, { x: 0.36, y: 0.76 }, { x: 0.35, y: 0.72 }];
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

test("the debouncer needs the finger held, then fires once per raise", () => {
  const d = new GestureDebouncer({ holdMs: 200, cooldownMs: 1000 });
  assert.equal(d.push(true, 0), false);
  assert.equal(d.push(true, 150), false, "not held long enough yet");
  assert.equal(d.push(true, 210), true, "fires once held");
  assert.equal(d.push(true, 400), false, "does not repeat while still held");
  assert.equal(d.push(false, 500), false);
  assert.equal(d.push(true, 600), false);
  assert.equal(d.push(true, 900), false, "re-raised, but inside the cooldown");
  assert.equal(d.push(false, 1000), false);
  assert.equal(d.push(true, 1300), false);
  assert.equal(d.push(true, 1520), true, "re-raised after the cooldown");
});

test("a single flickering frame resets the hold", () => {
  const d = new GestureDebouncer({ holdMs: 200, cooldownMs: 0 });
  d.push(true, 0);
  d.push(true, 150);
  d.push(false, 180); // detection dropped for a frame
  assert.equal(d.push(true, 210), false, "the hold starts over");
  assert.equal(d.push(true, 420), true);
});
