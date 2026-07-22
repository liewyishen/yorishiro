import type { HerState, Presence, SourceHandlers, StateSource, TickTag } from "./types";

/**
 * The daemon, live: /state polled for her vitals, /events held open for
 * her journal and her voice. Her voice and her heartbeat arrive as named
 * SSE events — `reply` for the reply lifecycle, `tick` for each heartbeat
 * outcome, `arrival` for a message landing — each a small JSON payload.
 * The default journal messages are still streamed for humans, but nothing
 * here reads their wording: the events, not the log text, are the interface.
 */

/** the tags the daemon emits; anything else is ignored rather than trusted */
const TICK_TAGS: readonly TickTag[] = ["spoke", "intercepted", "silent", "skipped"];
const isTickTag = (v: unknown): v is TickTag => TICK_TAGS.includes(v as TickTag);

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
  // conversation-window usage — integer counts only, never content. len/total
  // are null when the daemon's DB is down; cap is config, always present.
  history_cap?: number;
  history_len?: number | null;
  history_total?: number | null;
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
    // pass the window counts straight through when the daemon sends them; a
    // daemon too old to carry a cap simply leaves the readout absent
    conversation:
      typeof raw.history_cap === "number"
        ? { len: raw.history_len ?? null, cap: raw.history_cap, total: raw.history_total ?? null }
        : undefined,
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
      // default messages are the human journal now — left unparsed on purpose.
      // Every machine signal below is a named, structured event instead.
      es.addEventListener("tick", (e) => {
        try {
          const p = JSON.parse((e as MessageEvent).data as string);
          // one heartbeat, one tick — its tag maps straight onto TickEvent,
          // carrying the daemon's own timestamp so the strip stays ordered
          if (isTickTag(p.tag)) h.onTick({ at: Number(p.at) || Date.now(), tag: p.tag });
        } catch {
          /* a malformed tick is a dropped mark, never a thrown beat */
        }
      });
      es.addEventListener("arrival", (e) => {
        try {
          const p = JSON.parse((e as MessageEvent).data as string);
          h.onArrival({ at: Number(p.at) || Date.now() });
        } catch {
          h.onArrival({ at: Date.now() });
        }
      });
      es.addEventListener("reply", (e) => {
        try {
          const p = JSON.parse((e as MessageEvent).data as string);
          const at = Date.now();
          if (p.type === "reply_start")
            h.onReply?.({ kind: "start", id: p.id, at, self: p.initiated === true });
          else if (p.type === "reply_delta")
            h.onReply?.({ kind: "delta", id: p.id, at, text: String(p.text ?? "") });
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
