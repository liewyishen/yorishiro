import type { ArrivalEvent, HerState, StateSource, TickEvent, TickTag } from "./types";

/**
 * Stands in for the daemon's SSE stream. Time is compressed: a phase that
 * would hold for half an hour of real heartbeat lasts ~20s here, so every
 * visual mode is reachable within one cup of tea.
 */

interface Phase {
  state: HerState;
  durationS: number;
  // weights for the tick tags — spoke is rare by design, the daily cap is 4
  tagWeights: Record<TickTag, number>;
  // messages from outside land at this cadence (conversation only)
  arrivalEveryS?: number;
  // a spoke in this phase is hers alone — L2 woke, nobody prompted it
  selfSpoke?: boolean;
}

const SCRIPT: Phase[] = [
  {
    state: {
      presence: "active",
      tick_interval_seconds: 15,
      valence: 0.55,
      arousal: 0.75,
      activity_detail: "talking with you",
    },
    durationS: 20,
    tagWeights: { spoke: 0.06, silent: 0.64, intercepted: 0.3 },
    arrivalEveryS: 7,
  },
  {
    state: {
      presence: "active",
      tick_interval_seconds: 60,
      valence: 0.45,
      arousal: 0.4,
      activity_detail: "rereading what you said",
    },
    durationS: 16,
    tagWeights: { spoke: 0.03, silent: 0.57, intercepted: 0.4 },
  },
  {
    state: {
      presence: "activity",
      tick_interval_seconds: 600,
      valence: 0.2,
      arousal: 0.3,
      activity_detail: "organizing paper notes",
    },
    durationS: 22,
    tagWeights: { spoke: 0, silent: 0.3, intercepted: 0.7 },
  },
  {
    state: {
      presence: "active",
      tick_interval_seconds: 1800,
      valence: 0.05,
      arousal: 0.12,
      activity_detail: "nothing in particular",
    },
    durationS: 20,
    tagWeights: { spoke: 0.02, silent: 0.48, intercepted: 0.5 },
    selfSpoke: true,
  },
  {
    // the cool end of the temperature scale has to be visible too
    state: {
      presence: "active",
      tick_interval_seconds: 1800,
      valence: -0.55,
      arousal: 0.3,
      activity_detail: "stuck on a proof from tuesday",
    },
    durationS: 18,
    tagWeights: { spoke: 0, silent: 0.4, intercepted: 0.6 },
  },
  {
    state: {
      presence: "sleeping",
      tick_interval_seconds: 60,
      valence: 0.0,
      arousal: 0.05,
      activity_detail: "",
    },
    durationS: 16,
    // asleep, every wake is refused at the gate
    tagWeights: { spoke: 0, silent: 0, intercepted: 1 },
  },
];

const STATE_EVERY_MS = 1000;
const TICK_EVERY_MS = 3000;

function pickTag(weights: Record<TickTag, number>): TickTag {
  let r = Math.random();
  for (const tag of ["spoke", "silent", "intercepted"] as const) {
    r -= weights[tag];
    if (r <= 0) return tag;
  }
  return "intercepted";
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export function createMockSource(): StateSource {
  return {
    subscribe(onState, onTick, onArrival) {
      let phaseIdx = 0;
      let phaseElapsedS = 0;
      let sinceArrivalS = 0;
      let t = 0;

      const emit = () => {
        const p = SCRIPT[phaseIdx];
        // small drift so the projection is visibly live, not stepped
        onState({
          ...p.state,
          valence: clamp(p.state.valence + 0.08 * Math.sin(t / 7) + 0.03 * Math.sin(t / 2.3), -1, 1),
          arousal: clamp(p.state.arousal + 0.05 * Math.sin(t / 5 + 1), 0, 1),
        });
      };

      emit(); // synchronous first emit — the room is never blank

      const stateTimer = setInterval(() => {
        t += STATE_EVERY_MS / 1000;
        phaseElapsedS += STATE_EVERY_MS / 1000;
        sinceArrivalS += STATE_EVERY_MS / 1000;
        if (phaseElapsedS >= SCRIPT[phaseIdx].durationS) {
          phaseIdx = (phaseIdx + 1) % SCRIPT.length;
          phaseElapsedS = 0;
          sinceArrivalS = 0;
        }
        const p = SCRIPT[phaseIdx];
        if (p.arrivalEveryS && sinceArrivalS >= p.arrivalEveryS) {
          sinceArrivalS = 0;
          const a: ArrivalEvent = { at: Date.now() };
          onArrival(a);
        }
        emit();
      }, STATE_EVERY_MS);

      const tickTimer = setInterval(() => {
        const p = SCRIPT[phaseIdx];
        const tag = pickTag(p.tagWeights);
        const ev: TickEvent =
          tag === "spoke" && p.selfSpoke ? { at: Date.now(), tag, self: true } : { at: Date.now(), tag };
        onTick(ev);
      }, TICK_EVERY_MS);

      return () => {
        clearInterval(stateTimer);
        clearInterval(tickTimer);
      };
    },
  };
}
