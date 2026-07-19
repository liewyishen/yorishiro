import type { HerState, Presence, SourceHandlers, StateSource } from "./types";

/**
 * The daemon, live: /state polled for her vitals, /events held open for
 * her journal and her voice. Journal lines arrive as default SSE messages
 * and are read for what they record — a blocked tick, a passed gate, a
 * message landing. Reply lifecycle arrives as named `reply` events.
 */

/** Where the daemon lives. Override with VITE_HER_API when it moves. */
const API = (import.meta.env.VITE_HER_API as string | undefined) ?? "http://127.0.0.1:8765";

const POLL_MS = 2000;

/** /state as the daemon sends it — its own fields plus the derived interval. */
interface RawState {
  presence: string;
  last_interaction: number; // epoch seconds
  in_conversation: boolean;
  activity_running: boolean;
  tick_interval_seconds: number;
}

/**
 * The daemon carries no affect model yet, so valence/arousal are projected
 * from what IS real: how recently she was spoken to. Conversation runs
 * warm and awake; the feeling cools as the room stays quiet. When the
 * daemon grows real vitals, these lines are exactly what they replace.
 */
function project(raw: RawState): HerState {
  const idleMin = Math.max(0, Date.now() / 1000 - raw.last_interaction) / 60;
  const presence: Presence =
    raw.presence === "sleeping" ? "sleeping" : raw.activity_running ? "activity" : "active";
  const arousal =
    presence === "sleeping"
      ? 0.04
      : raw.in_conversation
        ? 0.5 + 0.35 * Math.exp(-idleMin / 8)
        : 0.08 + 0.45 * Math.exp(-idleMin / 40);
  const valence = raw.in_conversation
    ? 0.3 + 0.25 * Math.exp(-idleMin / 10)
    : 0.05 + 0.3 * Math.exp(-idleMin / 45);
  return {
    presence,
    tick_interval_seconds: raw.tick_interval_seconds,
    valence,
    arousal,
    activity_detail: raw.in_conversation ? "talking with you" : "",
  };
}

/** Before the first successful poll there is only this: a cool resting form. */
const UNSEEN: HerState = {
  presence: "active",
  tick_interval_seconds: 1800,
  valence: 0.05,
  arousal: 0.08,
  activity_detail: "",
};

export function createRealSource(): StateSource {
  return {
    subscribe(h: SourceHandlers) {
      let closed = false;
      let everSeen = false;

      const poll = async () => {
        try {
          const res = await fetch(`${API}/state`, { signal: AbortSignal.timeout(1500) });
          if (!res.ok) throw new Error(String(res.status));
          const raw = (await res.json()) as RawState;
          if (closed) return;
          everSeen = true;
          h.onState(project(raw));
          h.onLink?.(true);
        } catch {
          if (closed) return;
          // the room still needs paper when she can't be reached — the
          // resting form plus the unreachable label, never a blank page
          if (!everSeen) h.onState(UNSEEN);
          h.onLink?.(false);
        }
      };
      void poll();
      const timer = setInterval(() => void poll(), POLL_MS);

      const es = new EventSource(`${API}/events`);
      es.onmessage = (e) => {
        // the journal — plain lines, read for the events they record
        const line = String(e.data);
        if (line.includes("tick blocked")) h.onTick({ at: Date.now(), tag: "intercepted" });
        else if (line.includes("tick passed gate")) h.onTick({ at: Date.now(), tag: "silent" });
        else if (line.includes("message in")) h.onArrival({ at: Date.now() });
      };
      es.addEventListener("reply", (e) => {
        try {
          const p = JSON.parse((e as MessageEvent).data as string);
          const at = Date.now();
          if (p.type === "reply_start") h.onReply?.({ kind: "start", id: p.id, at, self: p.initiated === true });
          else if (p.type === "reply_delta") h.onReply?.({ kind: "delta", id: p.id, at, text: String(p.text ?? "") });
          else if (p.type === "reply_end") h.onReply?.({ kind: "end", id: p.id, at });
        } catch {
          /* a malformed event — the journal already logged whatever happened */
        }
      });
      // EventSource retries on its own; the poll is the up/down authority
      es.onerror = () => {
        if (!closed) h.onLink?.(false);
      };

      return () => {
        closed = true;
        clearInterval(timer);
        es.close();
      };
    },

    async send(text: string) {
      try {
        const res = await fetch(`${API}/message`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ text }),
          signal: AbortSignal.timeout(3000),
        });
        return res.ok;
      } catch {
        return false;
      }
    },
  };
}
