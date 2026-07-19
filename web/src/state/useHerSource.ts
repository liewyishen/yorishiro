import { useCallback, useEffect, useRef, useState } from "react";
import { createSource } from "./source";
import type { ArrivalEvent, HerState, SequencedReply, StateSource, TickEvent, Utterance } from "./types";

const TICK_MEMORY = 96; // enough for the strip and the instrument log
const ARRIVAL_MEMORY = 8; // arrivals only matter for the moment they land
const REPLY_MEMORY = 512; // one long utterance streams many deltas

export function useHerSource(): {
  state: HerState | null;
  ticks: TickEvent[];
  arrivals: ArrivalEvent[];
  replies: SequencedReply[];
  /** her current words, assembled from the reply stream — never a log */
  utterance: Utterance | null;
  connected: boolean;
  send: (text: string) => Promise<boolean>;
} {
  const [state, setState] = useState<HerState | null>(null);
  const [ticks, setTicks] = useState<TickEvent[]>([]);
  const [arrivals, setArrivals] = useState<ArrivalEvent[]>([]);
  const [replies, setReplies] = useState<SequencedReply[]>([]);
  const [utterance, setUtterance] = useState<Utterance | null>(null);
  const [connected, setConnected] = useState(true);
  const source = useRef<StateSource | null>(null);
  const seq = useRef(0);

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
        setUtterance((prev) => {
          if (r.kind === "start") return { id: r.id, text: "", live: true, self: r.self, endedAt: null };
          if (!prev || prev.id !== r.id) return prev; // a stray delta from a dead stream
          if (r.kind === "delta") return { ...prev, text: prev.text + r.text };
          return { ...prev, live: false, endedAt: r.at };
        });
      },
      onLink: setConnected,
    });
  }, []);

  const send = useCallback(async (text: string) => (source.current ? source.current.send(text) : false), []);

  return { state, ticks, arrivals, replies, utterance, connected, send };
}
