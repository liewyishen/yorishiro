import { useEffect, useRef, useState } from "react";
import { Composer } from "./components/Composer";
import { Presence } from "./components/Presence";
import { Instrument } from "./components/Instrument";
import { TickStrip } from "./components/TickStrip";
import { Voice } from "./components/Voice";
import { daylight, pageColors, presenceWords } from "./lib/projection";
import { useHerSource } from "./state/useHerSource";
import type { HerState } from "./state/types";

/**
 * The room follows her: bone paper while she's awake, easing down to a
 * dark room when she sleeps. Lives on CSS variables so every surface —
 * text, hairlines, pencil marks — dims together.
 */
function usePaper(state: HerState | null) {
  const target = useRef<number | null>(null);

  useEffect(() => {
    if (state) target.current = daylight(state);
  }, [state]);

  useEffect(() => {
    const still = window.matchMedia("(prefers-reduced-motion: reduce)");
    let cur: number | null = null;
    let drift = 0;
    let last = performance.now();
    let raf = 0;

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const dt = Math.min((now - last) / 1000, 0.1);
      last = now;
      if (target.current === null) return;
      // the first reading snaps: opening on her asleep must open dark,
      // not fade into it — afterwards day/night is a slow slide
      cur = cur === null ? target.current : cur + (target.current - cur) * (1 - Math.exp(-dt / 1.8));
      if (!still.matches) drift += dt * 0.1;
      const c = pageColors(cur, drift);
      const root = document.documentElement.style;
      root.setProperty("--paper", c.paper);
      root.setProperty("--ink", c.ink);
      root.setProperty("--ink-dim", c.inkDim);
      root.setProperty("--hairline", c.hairline);
      root.setProperty("--pencil", c.pencil);
    };

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, []);
}

export function App() {
  const { state, ticks, arrivals, replies, utterance, connected, send } = useHerSource();
  const [instrument, setInstrument] = useState(false);
  usePaper(state);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // typing in the composer is speech, not a keyboard shortcut
      if ((e.target as HTMLElement | null)?.tagName === "INPUT") return;
      // "`" too — on most layouts ~ lives behind shift, and the split
      // between presence and instrument shouldn't depend on remembering that
      if (e.key === "~" || e.key === "`") {
        e.preventDefault();
        setInstrument((v) => !v);
      } else if (e.key === "Escape") {
        setInstrument(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!state) return null; // mock emits synchronously; real SSE may not

  return (
    <main className="flex h-full flex-col">
      <header className="px-6 py-5">
        <h1 className="text-[11px] font-medium tracking-[0.15em] text-(--ink)">
          YORISHIRO <span className="text-(--ink-dim)">依り代</span>
        </h1>
      </header>

      <section className="flex flex-1 flex-col items-center justify-center">
        <Presence state={state} ticks={ticks} arrivals={arrivals} replies={replies} />
        {/* her words, live — enough space held open that surfacing text
            doesn't shove the core; only a very long reply may still stretch it */}
        <div className="flex min-h-28 items-start justify-center pt-1">
          <Voice utterance={utterance} />
        </div>
        {/* her voice — a human face, apart from the machine's mono */}
        <div className="flex flex-col items-center gap-1.5 text-center font-serif">
          <p className="min-h-6 text-[17px] text-(--ink)">{state.activity_detail}</p>
          <p className="text-[14px] text-(--ink-dim) italic">{presenceWords(state)}</p>
        </div>
      </section>

      <footer className="px-6 pb-5">
        <Composer send={send} />
        {/* the edge where the life-stream lives */}
        <TickStrip ticks={ticks} />
        {/* breathing room below the tallies — the hairline is chrome, not an axis */}
        <div className="mt-5 flex items-center justify-between border-t border-(--hairline) pt-3">
          <span className="text-[11px] font-medium tracking-[0.1em] text-(--ink-dim) uppercase">
            presence
          </span>
          {!connected && (
            <span className="text-[11px] tracking-[0.1em] text-(--ink-dim)">
              her daemon is unreachable
            </span>
          )}
          <button
            onClick={() => setInstrument((v) => !v)}
            className="cursor-pointer text-[11px] font-medium tracking-[0.1em] text-(--ink-dim) uppercase outline-offset-4 outline-(--ink-dim) hover:text-(--ink) focus-visible:outline"
          >
            ~ instrument
          </button>
        </div>
      </footer>

      {instrument && <Instrument state={state} ticks={ticks} />}
    </main>
  );
}
