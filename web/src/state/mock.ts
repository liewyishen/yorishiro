import type { ArrivalEvent, HerState, SourceHandlers, StateSource, TickEvent, TickTag } from "./types";

/**
 * Stands in for the daemon's SSE stream. Time is compressed: a phase that
 * would hold for half an hour of real heartbeat lasts ~20s here, so every
 * visual mode is reachable within one cup of tea.
 */

// the tags the mock actually rolls. "skipped" is emitted on its own path
// below (a before-L1 short-circuit isn't a weighted L0/L1 outcome), so it
// stays out of the weights — which also keeps every existing weight literal
// valid without adding a key to it.
type RolledTag = Exclude<TickTag, "skipped">;

interface Phase {
  state: HerState;
  durationS: number;
  // weights for the tick tags — spoke is rare by design, the daily cap is 4
  tagWeights: Record<RolledTag, number>;
  // messages from outside land at this cadence (conversation only)
  arrivalEveryS?: number;
  // a spoke in this phase is hers alone — L2 woke, nobody prompted it
  selfSpoke?: boolean;
  // like real in-conversation / afterglow beats, this phase mostly
  // short-circuits before L1 — emit some "skipped" ticks so the strip's
  // quiet mark shows in mock mode. Off unless set; weights are untouched.
  skips?: boolean;
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
    skips: true, // talking with you — most beats never reach L1
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
    skips: true, // afterglow — the warm tail of a conversation, still not asking
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
// mirrors config.yaml db.history_turns — the mock has no DB, so it fakes a
// window that fills as the conversation grows, purely to show the readout live
const HISTORY_CAP = 60;

function pickTag(weights: Record<RolledTag, number>): RolledTag {
  let r = Math.random();
  for (const tag of ["spoke", "silent", "intercepted"] as const) {
    r -= weights[tag];
    if (r <= 0) return tag;
  }
  return "intercepted";
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

// what the mock answers with when spoken to — hers to replace, obviously
const CANNED = [
  "mm. I was in the middle of a thought — say that again, slower?",
  "I heard you. give me a second to put this down.",
  "…that's a better question than you think it is.",
];

export function createMockSource(): StateSource {
  let handlers: SourceHandlers | null = null;
  let closed = false;
  let sends = 0;
  let stored = 0; // messages "in the DB" — grows with each turn, fakes the window

  return {
    subscribe(h) {
      handlers = h;
      closed = false;
      stored = 0; // a fresh subscription is a fresh conversation window
      const { onState, onTick, onArrival } = h;
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
          conversation: { len: Math.min(stored, HISTORY_CAP), cap: HISTORY_CAP, total: stored },
        });
      };

      emit(); // synchronous first emit — the room is never blank
      h.onLink?.(true); // the mock is always reachable — it lives here

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
          stored += 1; // his line lands in the window
          const a: ArrivalEvent = { at: Date.now() };
          onArrival(a);
        }
        emit();
      }, STATE_EVERY_MS);

      const tickTimer = setInterval(() => {
        const p = SCRIPT[phaseIdx];
        // a conversation/afterglow phase short-circuits before L1 more often
        // than not — emit those as "skipped" so the quiet mark is on the strip
        if (p.skips && Math.random() < 0.55) {
          onTick({ at: Date.now(), tag: "skipped" });
          return;
        }
        const tag = pickTag(p.tagWeights);
        const ev: TickEvent =
          tag === "spoke" && p.selfSpoke ? { at: Date.now(), tag, self: true } : { at: Date.now(), tag };
        onTick(ev);
      }, TICK_EVERY_MS);

      return () => {
        closed = true;
        handlers = null;
        clearInterval(stateTimer);
        clearInterval(tickTimer);
      };
    },

    // the same shape as the real wire: arrival lands, then a beat later
    // her reply streams in word by word
    async send(_text: string) {
      const h = handlers;
      if (!h) return false;
      h.onArrival({ at: Date.now() });
      stored += 2; // his line, then hers — a full exchange fills two slots
      const words = CANNED[sends++ % CANNED.length].split(" ");
      const id = `mock-${Date.now()}`;
      const guard = (fn: () => void) => () => {
        if (!closed) fn();
      };
      setTimeout(
        guard(() => h.onReply?.({ kind: "start", id, at: Date.now(), self: false })),
        700,
      );
      words.forEach((w, i) =>
        setTimeout(
          guard(() => h.onReply?.({ kind: "delta", id, at: Date.now(), text: (i ? " " : "") + w })),
          950 + i * 140,
        ),
      );
      setTimeout(
        guard(() => h.onReply?.({ kind: "end", id, at: Date.now() })),
        1150 + words.length * 140,
      );
      return true;
    },
  };
}
