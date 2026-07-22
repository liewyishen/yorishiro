import { useCallback, useEffect, useRef, useState } from "react";
import { createSource } from "./source";
import type {
  ArrivalEvent,
  ExchangeEntry,
  HerState,
  SequencedReply,
  StateSource,
  TickEvent,
  Utterance,
} from "./types";

const TICK_MEMORY = 96; // enough for the strip and the instrument log
const ARRIVAL_MEMORY = 8; // arrivals only matter for the moment they land
const REPLY_MEMORY = 512; // one long utterance streams many deltas
const HISTORY_MEMORY = 100; // the session ledger — deep enough to look back, bounded

export function useHerSource(): {
  state: HerState | null;
  ticks: TickEvent[];
  arrivals: ArrivalEvent[];
  replies: SequencedReply[];
  /** her current words, assembled from the reply stream — never a log */
  utterance: Utterance | null;
  /** the log the utterance is not: closed lines, this session only */
  history: ExchangeEntry[];
  /** her current words are being held open — the linger is suspended */
  pinned: boolean;
  /** hold the words that are up now, or let them go */
  togglePin: () => void;
  /** her self-initiated messages that have faded from the centre unseen */
  unseen: number;
  /** the user opened the ledger — her reach-outs have been read, clear the trace */
  markHistorySeen: () => void;
  connected: boolean;
  send: (text: string) => Promise<boolean>;
} {
  const [state, setState] = useState<HerState | null>(null);
  const [ticks, setTicks] = useState<TickEvent[]>([]);
  const [arrivals, setArrivals] = useState<ArrivalEvent[]>([]);
  const [replies, setReplies] = useState<SequencedReply[]>([]);
  const [utterance, setUtterance] = useState<Utterance | null>(null);
  const [history, setHistory] = useState<ExchangeEntry[]>([]);
  // the pin holds against TIME, never against a voice — so it lives here,
  // beside the utterance, and is released by the very events that replace
  // one. Anywhere else and the two could drift out of step.
  const [pinned, setPinned] = useState(false);
  // her reach-outs that faded from the centre before anyone looked. Session
  // only, in memory — a reload forgets, like the ledger it points at. Only
  // her FIRST-spoken lines land here; answers to you never do.
  const [unseen, setUnseen] = useState(0);
  const [connected, setConnected] = useState(true);
  const source = useRef<StateSource | null>(null);
  const seq = useRef(0);
  // her half-said lines, keyed by reply id, until an end closes them
  const open = useRef(new Map<string, { text: string; self: boolean; at: number }>());

  const remember = useCallback((entry: ExchangeEntry) => {
    setHistory((prev) => [...prev.slice(-(HISTORY_MEMORY - 1)), entry]);
  }, []);

  useEffect(() => {
    const src = createSource();
    source.current = src;
    return src.subscribe({
      onState: setState,
      onTick: (t) => setTicks((prev) => [...prev.slice(-(TICK_MEMORY - 1)), t]),
      onArrival: (a) => setArrivals((prev) => [...prev.slice(-(ARRIVAL_MEMORY - 1)), a]),
      onReply: (r) => {
        seq.current += 1;
        setReplies((prev) => [...prev.slice(-(REPLY_MEMORY - 1)), { ...r, seq: seq.current }]);
        // one stream, two readers: the same deltas raise the live Voice at
        // the centre and settle into the ledger below. Nothing is rendered
        // twice — the Voice shows the line being said, the ledger the said.
        if (r.kind === "start") {
          // she has begun something new — whatever was being held goes,
          // hers to take back whether you asked for it or she didn't
          setPinned(false);
          open.current.set(r.id, { text: "", self: r.self, at: r.at });
        } else if (r.kind === "delta") {
          const line = open.current.get(r.id);
          if (line) line.text += r.text;
        } else {
          // the worker always closes a stream, even one the model killed
          // mid-sentence, so nothing is left half-open here
          const line = open.current.get(r.id);
          open.current.delete(r.id);
          // stamped when she began, not when she finished — that is the
          // moment that belongs beside the message she was answering
          if (line?.text) {
            remember({ role: "her", text: line.text, at: line.at, self: line.self });
            // a completed reach-out counts once, on its end — she came by,
            // said her piece, and it will recede from the centre whether or
            // not you were looking. The dot is what's left of it. Replies to
            // you (self=false) don't count: you were here to receive them.
            if (line.self) setUnseen((n) => n + 1);
          }
        }
        setUtterance((prev) => {
          if (r.kind === "start") return { id: r.id, text: "", live: true, self: r.self, endedAt: null };
          if (!prev || prev.id !== r.id) return prev; // a stray delta from a dead stream
          if (r.kind === "delta") return { ...prev, text: prev.text + r.text };
          return { ...prev, live: false, endedAt: r.at };
        });
      },
      onLink: setConnected,
    });
  }, [remember]);

  // no guard on there being anything to hold: the Voice is the only thing
  // that knows whether her words are still on the water, and a pin set over
  // an empty centre shows nothing and is cleared by the next start anyway
  const togglePin = useCallback(() => setPinned((v) => !v), []);

  // opening the ledger is where her reach-outs are actually read, so it is
  // where "unseen" ends. One clearer, called by every path that opens it.
  const markHistorySeen = useCallback(() => setUnseen(0), []);

  const send = useCallback(
    async (text: string) => {
      // what you said is remembered whether or not it lands — the room
      // already says "unreachable" when her daemon is gone, and a message
      // that vanished from your own ledger too would be the worse lie
      remember({ role: "you", text, at: Date.now() });
      // speaking is the other thing that supersedes a held line. Released
      // here rather than on her reply, so the centre is already hers again
      // in the beat before she answers — and stays released if she doesn't.
      setPinned(false);
      return source.current ? source.current.send(text) : false;
    },
    [remember],
  );

  return {
    state,
    ticks,
    arrivals,
    replies,
    utterance,
    history,
    pinned,
    togglePin,
    unseen,
    markHistorySeen,
    connected,
    send,
  };
}
