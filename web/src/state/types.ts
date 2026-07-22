export type Presence = "active" | "sleeping" | "activity";

/**
 * One heartbeat's outcome, straight from the daemon's structured `tick`
 * event — no longer inferred from log wording.
 *   spoke: she reached out (a proactive L2 fired).
 *   intercepted: L0 refused — asked-and-blocked (cooldown / cap / dnd / sleep).
 *   silent: passed the gate, L1 consulted, chose nothing.
 *   skipped: short-circuited before L1 (in conversation / afterglow) — she
 *            wasn't even asked, so it renders quieter than an intercepted beat.
 */
export type TickTag = "spoke" | "intercepted" | "silent" | "skipped";

export interface HerState {
  presence: Presence;
  tick_interval_seconds: number;
  valence: number; // -1..1
  arousal: number; // 0..1
  activity_detail: string;
  /**
   * How full her short-term conversation window is — operator info for the
   * instrument only, never the presence screen. Counts, never content.
   *   len:   messages within the window she actually sees = min(total, cap),
   *          or null when the DB is offline.
   *   cap:   the window size (db.history_turns); known even with the DB down.
   *   total: every message stored behind the window, or null when offline.
   * Absent until the first reading that carries it (real source only).
   */
  conversation?: { len: number | null; cap: number; total: number | null };
}

export interface TickEvent {
  at: number; // epoch ms
  tag: TickTag;
  /** a spoke SHE started — L2 woke on its own, not in reply to anyone */
  self?: boolean;
}

/** A message from outside landing in the room — distinct from her speaking. */
export interface ArrivalEvent {
  at: number; // epoch ms
}

/** Her words leaving, as they leave: start → deltas → end, one id per utterance. */
export type ReplyEvent =
  | { kind: "start"; id: string; at: number; self: boolean }
  | { kind: "delta"; id: string; at: number; text: string }
  | { kind: "end"; id: string; at: number };

/** Deltas can share a millisecond; the seq is the watermark that can't. */
export type SequencedReply = ReplyEvent & { seq: number };

/** The utterance the reply events assemble into — her current words, not a log. */
export interface Utterance {
  id: string;
  text: string;
  live: boolean; // still streaming
  self: boolean;
  endedAt: number | null; // epoch ms once ended
}

/**
 * One line of the session ledger. The Utterance above is her voice as it
 * happens; this is what's left after it recedes — held in memory only, so
 * a reload genuinely forgets. `self` marks a line she started on her own
 * (L1). Always false today; carried through so that the day she speaks
 * first, the ledger can still tell the difference.
 */
export type ExchangeEntry =
  { role: "you"; text: string; at: number } | { role: "her"; text: string; at: number; self: boolean };

export interface SourceHandlers {
  onState: (s: HerState) => void;
  onTick: (t: TickEvent) => void;
  onArrival: (a: ArrivalEvent) => void;
  onReply?: (r: ReplyEvent) => void;
  /** the link to the daemon itself — false means the room can't see her */
  onLink?: (up: boolean) => void;
}

/**
 * The seam. Components only ever see this interface; the mock and the
 * EventSource-backed real source are interchangeable behind it. Ticks are
 * the heartbeat's record, replies are her voice — separate grammars, so
 * speech streaming never masquerades as a beat.
 */
export interface StateSource {
  subscribe(h: SourceHandlers): () => void;
  /** push a message toward her — resolves false if it could not be delivered */
  send(text: string): Promise<boolean>;
}
