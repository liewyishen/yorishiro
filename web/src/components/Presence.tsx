import { useEffect, useRef } from "react";
import {
  arrivalGlow,
  BODY_R,
  breathAmpl,
  breathPeriodS,
  clamp,
  CORE_R,
  daylight,
  hsl,
  lerp,
  poolHsl,
  poolLightHsl,
  REPLY_RIPPLE_EVERY_S,
  retractDurationS,
  retractReachPx,
  RING_WIDTH,
  ringAlpha,
  ringHsl,
  rippleDurationS,
  rippleStrength,
  rotationPeriodS,
  SELF_RIPPLE,
  TRAVEL_R,
} from "../lib/projection";
import { SIGNATURES } from "../lib/rings";
import type { ArrivalEvent, HerState, SequencedReply, TickEvent } from "../state/types";

/**
 * Three layers, three grammars. The water core is her interior: an
 * impression of water — offset pools of light adrift on slow
 * incommensurate sines, a broad wandering shadow, a softly breathing
 * edge — never a simulation, never a solid ball. The body ring is her
 * form: one uneven broken ring hugging the pool, turning at the pace of
 * minutes. Ripples are her speech, born only from real events: a spoken
 * tick travels out and dies like the tail of a sound; an intercepted
 * tick rises a little way, hangs, and is taken back in; a message
 * landing from outside brightens the water for half a beat first.
 */

const W = 440;
const H = 440;
const C = 220;
const TAU = Math.PI * 2;
const GOLDEN = 2.399963; // successive ripples never share a starting angle

/**
 * Her currents: where the lighter water wanders and how it dims. Sine
 * pairs with incommensurate periods so the drift never visibly loops —
 * far away, moving water; up close, no technique to see.
 */
const CURRENTS = [
  { ax: 0.2, tx: 13, px: 0.4, ax2: 0.14, tx2: 29, px2: 2.6, ay: 0.17, ty: 17, py: 1.9, ay2: 0.13, ty2: 37, py2: 4.4, r: 0.85, a0: 0.32, a1: 0.14, ta: 21, pa: 0.9 },
  { ax: 0.16, tx: 19, px: 3.7, ax2: 0.12, tx2: 41, px2: 1.1, ay: 0.21, ty: 11, py: 5.2, ay2: 0.1, ty2: 31, py2: 2.8, r: 0.7, a0: 0.26, a1: 0.12, ta: 27, pa: 3.3 },
] as const;

interface Ripple {
  kind: "ripple" | "retract";
  born: number;
  life: number;
  sig: number; // index into SIGNATURES
  dir: 1 | -1; // successive ripples counter-rotate
  a0: number;
  strength: number; // ripple ink
  widthX: number; // the self ripple is drawn heavier
  apex: number; // retract only
}

interface Eased {
  valence: number;
  arousal: number;
  rotP: number;
  breathP: number;
  breathA: number;
  day: number;
}

const easeOutQuad = (t: number) => 1 - (1 - t) ** 2;
const easeOutCubic = (t: number) => 1 - (1 - t) ** 3;
const easeInCubic = (t: number) => t ** 3;

/** radius / opacity / weight of one ripple at progress p — null when gone */
function shape(rp: Ripple, p: number): { r: number; alpha: number; width: number } | null {
  if (p >= 1) return null;
  if (rp.kind === "ripple") {
    // leaves her form quickly, slows, and dies away long — a sound's tail
    return {
      r: BODY_R + (TRAVEL_R - BODY_R) * easeOutCubic(p),
      alpha: rp.strength * (1 - p) ** 2.1,
      width: RING_WIDTH * rp.widthX * (1.15 - 0.4 * p),
    };
  }
  // the hesitation, in three movements:
  // rise — decelerating the whole way, never reaching full stretch
  if (p < 0.4) {
    const q = easeOutCubic(p / 0.4);
    return { r: BODY_R + (rp.apex * 0.96 - BODY_R) * q, alpha: 0.35 + 0.55 * q, width: RING_WIDTH * 1.25 };
  }
  // the held beat — still leaning outward, a reach that doesn't commit
  if (p < 0.62) {
    const q = easeOutQuad((p - 0.4) / 0.22);
    return { r: rp.apex * (0.96 + 0.04 * q), alpha: 0.9, width: RING_WIDTH * 1.25 };
  }
  // taken back — slow to let go, then swallowed past the ring into the water
  const q = easeInCubic((p - 0.62) / 0.38);
  const r = rp.apex - (rp.apex - CORE_R) * q;
  if (r <= CORE_R + 1) return null;
  return { r, alpha: 0.9 - 0.45 * q, width: RING_WIDTH * 1.25 };
}

export function Presence({
  state,
  ticks,
  arrivals,
  replies,
}: {
  state: HerState;
  ticks: TickEvent[];
  arrivals: ArrivalEvent[];
  replies: SequencedReply[];
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const target = useRef(state);
  const tickQ = useRef<TickEvent[]>([]);
  const arriveQ = useRef<ArrivalEvent[]>([]);
  const replyQ = useRef<SequencedReply[]>([]);
  // events that predate mount are history, not speech — never replay them
  const seenTick = useRef(ticks.length ? ticks[ticks.length - 1].at : 0);
  const seenArrive = useRef(arrivals.length ? arrivals[arrivals.length - 1].at : 0);
  const seenReply = useRef(replies.length ? replies[replies.length - 1].seq : 0);

  useEffect(() => {
    target.current = state;
  }, [state]);

  useEffect(() => {
    for (const t of ticks) {
      if (t.at > seenTick.current) {
        // a hidden tab hears no live speech — mark seen, never replay on return
        if (!document.hidden) tickQ.current.push(t);
        seenTick.current = t.at;
      }
    }
  }, [ticks]);

  useEffect(() => {
    for (const a of arrivals) {
      if (a.at > seenArrive.current) {
        if (!document.hidden) arriveQ.current.push(a);
        seenArrive.current = a.at;
      }
    }
  }, [arrivals]);

  useEffect(() => {
    for (const r of replies) {
      if (r.seq > seenReply.current) {
        if (!document.hidden) replyQ.current.push(r);
        seenReply.current = r.seq;
      }
    }
  }, [replies]);

  useEffect(() => {
    const cv = canvas.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    cv.width = W * dpr;
    cv.height = H * dpr;
    ctx.scale(dpr, dpr);
    ctx.lineCap = "round";

    const still = window.matchMedia("(prefers-reduced-motion: reduce)");
    const s0 = target.current;
    const cur: Eased = {
      valence: s0.valence,
      arousal: s0.arousal,
      rotP: rotationPeriodS(s0),
      breathP: breathPeriodS(s0),
      breathA: breathAmpl(s0),
      day: daylight(s0),
    };

    let ripples: Ripple[] = [];
    let births = 0;
    let clock = 0;
    let rotAcc = 0;
    let breathAcc = 0;
    let pulseAt = -10; // the water swells when speech leaves or returns
    let pulseAmp = 0;
    let arrivalAt = -10; // the struck note
    let lastSpeechAt = -10; // paces the ripple rhythm while her words stream
    let replyHeavy = false; // a self-initiated utterance opens at full weight, once
    let last = performance.now();
    let raf = 0;

    const throwRipple = (
      kind: Ripple["kind"],
      life: number,
      sig: number,
      strength: number,
      widthX: number,
      apex = 0,
    ) => {
      ripples.push({ kind, born: clock, life, sig, dir: births % 2 ? -1 : 1, a0: births * GOLDEN, strength, widthX, apex });
      births += 1;
    };

    // LAYER 1 — the water core. An impression, not a simulation: a soft
    // heart of light, two drifting brighter pools, one wandering shadow,
    // everything fading to nothing before the edge is ever a line.
    const drawWater = (R: number, glow: number, dayA: number) => {
      const base = poolHsl(cur.valence, cur.day);
      const light = poolLightHsl(cur.valence, cur.day);
      // the edge itself breathes, a hair beyond the vitals breath
      const edge = R * (1 + 0.01 * Math.sin(clock / 7.3 + 0.6) + 0.007 * Math.sin(clock / 17.7 + 2.2));
      ctx.save();
      ctx.beginPath();
      ctx.arc(C, C, edge, 0, TAU);
      ctx.clip();
      const heart = ctx.createRadialGradient(C, C, 0, C, C, edge);
      heart.addColorStop(0, hsl({ ...base, l: Math.min(base.l + 4 + glow * 10, 95) }, 0.9 * dayA));
      heart.addColorStop(0.62, hsl(base, 0.55 * dayA));
      heart.addColorStop(1, hsl(base, 0));
      ctx.fillStyle = heart;
      ctx.fillRect(C - edge, C - edge, edge * 2, edge * 2);
      for (const cu of CURRENTS) {
        const x = C + edge * (cu.ax * Math.sin(clock / cu.tx + cu.px) + cu.ax2 * Math.sin(clock / cu.tx2 + cu.px2));
        const y = C + edge * (cu.ay * Math.sin(clock / cu.ty + cu.py) + cu.ay2 * Math.sin(clock / cu.ty2 + cu.py2));
        const a = (cu.a0 + cu.a1 * Math.sin(clock / cu.ta + cu.pa)) * (1 + glow * 0.4) * dayA;
        const g = ctx.createRadialGradient(x, y, 0, x, y, edge * cu.r);
        g.addColorStop(0, hsl(light, a));
        g.addColorStop(1, hsl(light, 0));
        ctx.fillStyle = g;
        ctx.fillRect(C - edge, C - edge, edge * 2, edge * 2);
      }
      // water is never evenly lit: one broad dimness, adrift
      const sx = C + edge * 0.5 * Math.sin(clock / 31 + 2.1);
      const sy = C + edge * 0.5 * Math.sin(clock / 19 + 0.7);
      const shade = ctx.createRadialGradient(sx, sy, 0, sx, sy, edge * 1.1);
      shade.addColorStop(0, hsl({ ...base, l: Math.max(base.l - 13, 0) }, (0.1 + 0.08 * Math.sin(clock / 23 + 1.9)) * dayA));
      shade.addColorStop(1, hsl({ ...base, l: Math.max(base.l - 13, 0) }, 0));
      ctx.fillStyle = shade;
      ctx.fillRect(C - edge, C - edge, edge * 2, edge * 2);
      ctx.restore();
    };

    // LAYER 2 — the body ring: one, hugging, uneven, lazily turning
    const drawBody = (scale: number, ang: number, dayA: number) => {
      const ink = ringHsl(cur.valence, cur.day);
      ctx.lineWidth = RING_WIDTH;
      ctx.strokeStyle = hsl(ink, 0.85 * dayA);
      for (const arc of SIGNATURES[0]) {
        ctx.beginPath();
        ctx.arc(C, C, BODY_R * scale, ang + arc.from, ang + arc.to);
        ctx.stroke();
      }
    };

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const dt = clamp((now - last) / 1000, 0, 0.05); // ~2 frames — a stalled tab can't lunge
      last = now;

      // ease the vitals, then project per frame — the same mapping the
      // instrument prints, just smoothed in time
      const t = target.current;
      const k = 1 - Math.exp(-dt * 2.2);
      cur.valence = lerp(cur.valence, t.valence, k);
      cur.arousal = lerp(cur.arousal, t.arousal, k);
      cur.rotP = lerp(cur.rotP, rotationPeriodS(t), k);
      cur.breathP = lerp(cur.breathP, breathPeriodS(t), k);
      cur.breathA = lerp(cur.breathA, breathAmpl(t), k);
      // day/night matches the room's slower ease, so ink and page dim together
      cur.day = lerp(cur.day, daylight(t), 1 - Math.exp(-dt / 1.8));

      const dayA = ringAlpha(cur.day);
      ctx.clearRect(0, 0, W, H);

      if (still.matches) {
        // her still form: water and ring held where they are, colors live
        tickQ.current.length = 0;
        arriveQ.current.length = 0;
        replyQ.current.length = 0;
        drawWater(CORE_R, 0, dayA);
        drawBody(1, -0.9, dayA);
        return;
      }

      clock += dt;
      rotAcc += (dt * TAU) / cur.rotP;
      breathAcc += (dt * TAU) / cur.breathP;

      // LAYER 3 — events are the only place a ripple can be born
      if (arriveQ.current.length) arrivalAt = clock;
      arriveQ.current.length = 0;
      for (const ev of tickQ.current) {
        if (ev.tag === "spoke") {
          const heavy = ev.self === true;
          throwRipple(
            "ripple",
            rippleDurationS(t) * (heavy ? SELF_RIPPLE.durationX : 1),
            heavy ? 3 : 1 + (births % 2),
            heavy ? SELF_RIPPLE.strength : rippleStrength(t),
            heavy ? SELF_RIPPLE.widthX : 1,
          );
          pulseAt = clock;
          pulseAmp = heavy ? 3 : 1.8;
        } else if (ev.tag === "intercepted") {
          throwRipple("retract", retractDurationS(t), 4, 0, 1, retractReachPx(t));
        }
        // silent: her stillness needs no drawing
      }
      tickQ.current.length = 0;

      // her voice, live: the water brightens as she begins, then ripples
      // leave the ring on a gentle rhythm for as long as the words flow
      for (const ev of replyQ.current) {
        if (ev.kind === "start") {
          arrivalAt = clock; // the struck note — her voice beginning
          replyHeavy = ev.self;
          lastSpeechAt = -10; // the first delta ripples at once
        } else if (ev.kind === "delta") {
          if (clock - lastSpeechAt >= REPLY_RIPPLE_EVERY_S) {
            const heavy = replyHeavy;
            throwRipple(
              "ripple",
              rippleDurationS(t) * (heavy ? SELF_RIPPLE.durationX : 1),
              heavy ? 3 : 1 + (births % 2),
              heavy ? SELF_RIPPLE.strength : rippleStrength(t),
              heavy ? SELF_RIPPLE.widthX : 1,
            );
            pulseAt = clock;
            pulseAmp = heavy ? 3 : 1.4;
            lastSpeechAt = clock;
            replyHeavy = false; // the heaviest ripple is a single ripple
          }
        }
        // end: nothing forced — the last ring's long tail is the finish
      }
      replyQ.current.length = 0;

      // breath ≤3%, plus the brief swell when speech leaves or returns
      const breathe = 1 + cur.breathA * Math.sin(breathAcc);
      const swell = pulseAmp * Math.exp(-3 * (clock - pulseAt));
      drawWater(CORE_R * breathe + swell, arrivalGlow(clock - arrivalAt), dayA);

      const ink = ringHsl(cur.valence, cur.day);
      ripples = ripples.filter((rp) => {
        const p = (clock - rp.born) / rp.life;
        const sh = shape(rp, p);
        if (!sh) {
          if (rp.kind === "retract") {
            pulseAt = clock; // swallowed — the water takes it back in
            pulseAmp = 1.6;
          }
          return false;
        }
        const ang = rp.a0 + rp.dir * rotAcc;
        ctx.lineWidth = sh.width;
        ctx.strokeStyle = hsl(ink, sh.alpha * dayA);
        for (const arc of SIGNATURES[rp.sig]) {
          ctx.beginPath();
          ctx.arc(C, C, sh.r, ang + arc.from, ang + arc.to);
          ctx.stroke();
        }
        return true;
      });

      // her form rides over her speech, breathing with the water
      drawBody(breathe, rotAcc, dayA);
    };

    // coming back to the tab resumes from now, with no catch-up: anything
    // that slipped into the queues around the transition is already history
    const onVisibility = () => {
      if (document.hidden) return;
      last = performance.now();
      tickQ.current.length = 0;
      arriveQ.current.length = 0;
      replyQ.current.length = 0;
    };
    document.addEventListener("visibilitychange", onVisibility);

    raf = requestAnimationFrame(frame);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      cancelAnimationFrame(raf);
    };
  }, []);

  return (
    <canvas
      ref={canvas}
      aria-hidden
      className="block"
      style={{ width: W, height: H, maxWidth: "90vw" }}
    />
  );
}
