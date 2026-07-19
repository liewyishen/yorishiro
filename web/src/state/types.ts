export type Presence = "active" | "sleeping" | "activity";

/** spoke: she initiated. intercepted: L0 refused. silent: allowed, chose nothing. */
export type TickTag = "spoke" | "intercepted" | "silent";

export interface HerState {
  presence: Presence;
  tick_interval_seconds: number;
  valence: number; // -1..1
  arousal: number; // 0..1
  activity_detail: string;
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
