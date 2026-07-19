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

/**
 * The seam. Components only ever see this interface, so replacing the mock
 * with an EventSource on the daemon's /events is a one-module swap. The
 * real source will also feed streaming beats through onTick as spokes —
 * one per beat — so speech stays the only thing that ripples.
 */
export interface StateSource {
  subscribe(
    onState: (s: HerState) => void,
    onTick: (t: TickEvent) => void,
    onArrival: (a: ArrivalEvent) => void,
  ): () => void;
}
