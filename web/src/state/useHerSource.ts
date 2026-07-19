import { useEffect, useState } from "react";
import { createSource } from "./source";
import type { ArrivalEvent, HerState, TickEvent } from "./types";

const TICK_MEMORY = 96; // enough for the strip and the instrument log
const ARRIVAL_MEMORY = 8; // arrivals only matter for the moment they land

export function useHerSource(): {
  state: HerState | null;
  ticks: TickEvent[];
  arrivals: ArrivalEvent[];
} {
  const [state, setState] = useState<HerState | null>(null);
  const [ticks, setTicks] = useState<TickEvent[]>([]);
  const [arrivals, setArrivals] = useState<ArrivalEvent[]>([]);

  useEffect(() => {
    return createSource().subscribe(
      setState,
      (t) => setTicks((prev) => [...prev.slice(-(TICK_MEMORY - 1)), t]),
      (a) => setArrivals((prev) => [...prev.slice(-(ARRIVAL_MEMORY - 1)), a]),
    );
  }, []);

  return { state, ticks, arrivals };
}
