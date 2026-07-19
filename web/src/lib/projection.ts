import type { HerState } from "../state/types";

/**
 * The locked ontology, three layers, three grammars:
 *   WATER CORE — her interior. A pool of moving light; it flows.
 *   BODY RING  — her form. Exactly one broken ring, hugging the pool,
 *                turning so slowly you must stare to be sure.
 *   RIPPLES    — her speech. Born only from real events, travelling
 *                outward from her form, dying like the tail of a sound.
 * No layer borrows another's grammar: the interior never radiates, the
 * form never multiplies, speech never decorates. The interface does
 * not lie.
 *
 * Every mapping from vitals to pixels lives in this file; the
 * instrument overlay reads these same functions back, so what the eye
 * sees and what the panel says can never disagree.
 */

export interface Hsl {
  h: number;
  s: number;
  l: number;
}

const MAUVE: Hsl = { h: 335, s: 9, l: 65 }; // valence -1 — the warmth gone
const CLAY: Hsl = { h: 20, s: 32, l: 66 }; // valence  0 — resting, dusty
const TERRA: Hsl = { h: 16, s: 58, l: 63 }; // valence +1 — saturated warm

/** 0 = faded grey-mauve, 1 = full terracotta. The instrument shows this. */
export function warmth(valence: number): number {
  return (clamp(valence, -1, 1) + 1) / 2;
}

export function bodyHsl(valence: number): Hsl {
  const v = clamp(valence, -1, 1);
  if (v < 0) {
    // hue travels 335 → 380(=20): it passes through red, never through blue
    const u = v + 1;
    return { h: lerp(MAUVE.h, CLAY.h + 360, u) % 360, s: lerp(MAUVE.s, CLAY.s, u), l: lerp(MAUVE.l, CLAY.l, u) };
  }
  return { h: lerp(CLAY.h, TERRA.h, v), s: lerp(CLAY.s, TERRA.s, v), l: lerp(CLAY.l, TERRA.l, v) };
}

export function hsl(c: Hsl, a = 1): string {
  const base = `${c.h.toFixed(1)} ${c.s.toFixed(1)}% ${c.l.toFixed(1)}%`;
  return a >= 1 ? `hsl(${base})` : `hsl(${base} / ${a.toFixed(3)})`;
}

/** The water's base tone — a pool of light, not a solid; night pales it. */
export function poolHsl(valence: number, day: number): Hsl {
  const b = bodyHsl(valence);
  return { h: b.h, s: lerp(b.s * 0.35, b.s, day), l: lerp(48, b.l + 2, day) };
}

/** The brighter water that drifts inside the pool — where the light pools. */
export function poolLightHsl(valence: number, day: number): Hsl {
  const p = poolHsl(valence, day);
  return { h: p.h, s: p.s, l: Math.min(p.l + 11, 93) };
}

/**
 * The ink her speech is drawn with. A stroke needs more pigment than a
 * fill; ripples inherit the current warmth the moment they are thrown.
 */
export function ringHsl(valence: number, day: number): Hsl {
  const b = bodyHsl(valence);
  return {
    h: b.h,
    s: lerp(b.s * 0.3, Math.min(b.s + 8, 100), day),
    l: lerp(52, b.l - 10, day),
  };
}

/** On light paper a 1px hairline evaporates. */
export const RING_WIDTH = 1.5;

/** Global opacity: core and rings dim with the room but never vanish. */
export function ringAlpha(day: number): number {
  return lerp(0.6, 1, day);
}

/** Her size at rest. Small on purpose: presence, not spectacle. */
export const CORE_R = 26;

/**
 * The body ring hugs the pool — close enough to read as one being.
 * Exactly one ring, ever: a second concentric line revives sonar.
 */
export const BODY_R = 34;

/** Where a full ripple crossing ends — the far bank of the pond. */
export const TRAVEL_R = 170;

/**
 * BREATH, signal one. Its period is bound to the real heartbeat: quick
 * company breathes visibly, deep idle is near-still — but never frozen,
 * so quiet reads as "here, just quiet", not as a dead render.
 */
export function breathPeriodS(state: HerState): number {
  return clamp(1.9 * Math.log(Math.max(state.tick_interval_seconds, 1)), 4.5, 20);
}

/** Breath amplitude as a fraction of the core radius — capped at 3%. */
export function breathAmpl(state: HerState): number {
  return lerp(0.01, 0.03, clamp(state.arousal, 0, 1));
}

/**
 * The body ring's lazy self-rotation: a full turn on the order of
 * minutes — slow enough that you must stare to confirm it moves at all.
 * Faster would be a loading spinner, and a spinner is a machine.
 */
export function rotationPeriodS(state: HerState): number {
  return lerp(200, 80, clamp(state.arousal, 0, 1));
}

/** A spoken ripple's whole life — the fading tail is most of it. */
export function rippleDurationS(state: HerState): number {
  return lerp(3.8, 2.6, clamp(state.arousal, 0, 1));
}

/** How much ink a spoken ripple carries. */
export function rippleStrength(state: HerState): number {
  return 0.5 + 0.5 * clamp(state.arousal, 0, 1);
}

/**
 * A self-initiated message is L2 waking on its own — the rarest, most
 * expensive act in the architecture. Its ripple is the heaviest thing
 * this surface ever draws, and nothing else may borrow these numbers.
 */
export const SELF_RIPPLE = { strength: 1, widthX: 2.1, durationX: 1.3 } as const;

/** How far an intercepted ring reaches: about a quarter of a crossing. */
export function retractReachPx(state: HerState): number {
  return BODY_R + lerp(0.22, 0.3, clamp(state.arousal, 0, 1)) * (TRAVEL_R - BODY_R);
}

/** The whole hesitation — the rise, the held beat, the pull back in. */
export function retractDurationS(state: HerState): number {
  return lerp(3.1, 2.4, clamp(state.arousal, 0, 1));
}

/**
 * The struck-note envelope when a message from outside lands: a fast
 * attack, half a beat of glow, gone — before any ripple answers it.
 */
export function arrivalGlow(sinceS: number): number {
  if (sinceS < 0) return 0;
  const attack = 0.09;
  if (sinceS < attack) return sinceS / attack;
  return Math.exp(-(sinceS - attack) / 0.35);
}

/** 1 = bone paper, 0 = the dark room. Her day/night lives in the page. */
export function daylight(state: HerState): number {
  return state.presence === "sleeping" ? 0 : 1;
}

const DAY = {
  paper: [247, 244, 238],
  ink: [58, 53, 46],
  inkDim: [104, 96, 84],
  hairline: [214, 208, 196],
  pencil: [63, 58, 50],
} as const;

const NIGHT = {
  paper: [12, 11, 10],
  ink: [181, 175, 166],
  inkDim: [134, 128, 118],
  hairline: [40, 38, 35],
  pencil: [196, 190, 180],
} as const;

/**
 * The room's surfaces at a given daylight, with a barely-there warm drift
 * on the paper — felt more than seen, so the page never sits still.
 */
export function pageColors(day: number, driftPhase: number): Record<"paper" | "ink" | "inkDim" | "hairline" | "pencil", string> {
  const drift = Math.sin(driftPhase) * 1.5 * day;
  const mix = (d: readonly number[], n: readonly number[], drifts: boolean) => {
    const c = d.map((x, i) => lerp(n[i], x, day));
    if (drifts) {
      c[0] += drift;
      c[1] += drift * 0.4;
      c[2] -= drift;
    }
    return `rgb(${c.map((x) => Math.round(clamp(x, 0, 255))).join(" ")})`;
  };
  return {
    paper: mix(DAY.paper, NIGHT.paper, true),
    ink: mix(DAY.ink, NIGHT.ink, false),
    inkDim: mix(DAY.inkDim, NIGHT.inkDim, false),
    hairline: mix(DAY.hairline, NIGHT.hairline, false),
    pencil: mix(DAY.pencil, NIGHT.pencil, false),
  };
}

/**
 * The next-heartbeat indicator, in words only. A number would be a
 * countdown, a countdown makes you wait, and waiting reads as neediness.
 */
export function presenceWords(state: HerState): string {
  if (state.presence === "sleeping") return "asleep";
  if (state.presence === "activity") return "absorbed elsewhere";
  const s = state.tick_interval_seconds;
  if (s <= 20) return "close by";
  if (s <= 120) return "lingering near";
  return "awake, quiet";
}

export const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
export const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
