/**
 * Her ring-signature: the dash pattern each ring is drawn with. Uneven
 * arcs, uneven gaps — never gear teeth. Generated once from a fixed seed,
 * so the fingerprint is the same every load: a body should be the same
 * body every morning.
 */

export interface Arc {
  from: number; // radians
  to: number;
}

function mulberry32(seed: number): () => number {
  let a = seed;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const TAU = Math.PI * 2;

function makeSignature(rand: () => number, coverage: number, nFixed?: number): Arc[] {
  const n = nFixed ?? 3 + Math.floor(rand() * 3); // 3–5 arcs per ring
  const arcW = Array.from({ length: n }, () => 0.5 + rand());
  const gapW = Array.from({ length: n }, () => 0.5 + rand());
  const arcTotal = arcW.reduce((s, w) => s + w, 0);
  const gapTotal = gapW.reduce((s, w) => s + w, 0);
  const arcs: Arc[] = [];
  let a = rand() * TAU;
  for (let i = 0; i < n; i++) {
    const span = (arcW[i] / arcTotal) * coverage * TAU;
    arcs.push({ from: a, to: a + span });
    a += span + (gapW[i] / gapTotal) * (1 - coverage) * TAU;
  }
  return arcs;
}

const rand = mulberry32(0x59a5); // the same seed the old body wore

/**
 * 0: the body ring — four arcs of unequal length, gaps of unequal width.
 *    Even spacing would be gear teeth; uneven is what a body looks like.
 * 1–2: spoken ripples, alternated so back-to-back speech never stamps
 *      the same ring twice.
 * 3: the self-initiated ripple — near-full: she arrives with her whole voice.
 * 4: the retracting ring — near-full coverage, so even at its smallest
 *    the shape still reads as a ring and the pull-back stays legible.
 */
export const SIGNATURES: Arc[][] = [
  makeSignature(rand, 0.76, 4),
  makeSignature(rand, 0.78),
  makeSignature(rand, 0.84),
  makeSignature(rand, 0.9),
  makeSignature(rand, 0.93),
];
