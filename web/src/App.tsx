import { useCallback, useEffect, useRef, useState } from "react";
import { Composer } from "./components/Composer";
import { History } from "./components/History";
import { Presence } from "./components/Presence";
import { Instrument } from "./components/Instrument";
import { ReachedOut } from "./components/ReachedOut";
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
  const {
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
  } = useHerSource();
  const [instrument, setInstrument] = useState(false);
  const [ledger, setLedger] = useState(false);
  usePaper(state);

  // the two overlays are alternatives, not layers — either one covers the
  // room whole, so opening one puts the other away
  const toggleInstrument = useCallback(() => {
    setInstrument((v) => !v);
    setLedger(false);
  }, []);
  const toggleLedger = useCallback(() => {
    setLedger((v) => !v);
    setInstrument(false);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // typing in the composer is speech, not a keyboard shortcut —
      // "h" especially, which she should receive rather than intercept
      if ((e.target as HTMLElement | null)?.tagName === "INPUT") return;
      // "`" too — on most layouts ~ lives behind shift, and the split
      // between presence and instrument shouldn't depend on remembering that
      if (e.key === "~" || e.key === "`") {
        e.preventDefault();
        toggleInstrument();
      } else if (e.key === "h" || e.key === "H") {
        e.preventDefault();
        toggleLedger();
      } else if (e.key === "p" || e.key === "P") {
        // holding her words is not an overlay — it doesn't close the others,
        // and Escape leaves it alone: you can read the ledger over a held line
        e.preventDefault();
        togglePin();
      } else if (e.key === "Escape") {
        setInstrument(false);
        setLedger(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleInstrument, toggleLedger, togglePin]);

  // seeing is opening. The ledger is where her reach-outs are actually read,
  // so the moment it opens the unseen trace is spent — cleared here, by the
  // one path every opener funnels through (the h key, the history control,
  // and the ember all set ledger true), so no opener can clear differently.
  useEffect(() => {
    if (ledger) markHistorySeen();
  }, [ledger, markHistorySeen]);

  if (!state) return null; // mock emits synchronously; real SSE may not

  return (
    <main className="flex h-full flex-col">
      <header className="px-6 py-5">
        <h1 className="text-[11px] font-medium tracking-[0.15em] text-(--ink)">
          YORISHIRO <span className="text-(--ink-dim)">依り代</span>
        </h1>
      </header>

      {/* the stack hangs from the top rather than floating centered, so the
          free space in the room collects below her words instead of being
          split above and below the core. That is what lets the slot grow: the
          growth is taken out of that space, and the core above it never moves */}
      <section className="flex flex-1 flex-col items-center justify-start">
        <Presence state={state} ticks={ticks} arrivals={arrivals} replies={replies} />
        {/* her words, live. The floor is what the slot used to be fixed at, so
            short lines and the silence between them still hold the layout open.
            The ceiling is whichever comes first: 45vh, or the room actually
            left over once the core and the lines below have taken theirs —
            44.5rem is the header, the 440px core, those two lines, the footer
            and the margin below, added up. Past that the Voice scrolls inside
            its own banks instead of growing, because growing further would
            push the words below off the bottom of the room. If the core canvas
            is ever resized, this number moves with it. The margin keeps the air
            the old fixed slot gave for free — a 104px Voice inside 144px always
            left a gap, and without it her last line runs into the lines below. */}
        <div className="mb-6 flex max-h-[min(45vh,calc(100vh-44.5rem))] min-h-36 items-center justify-center">
          <Voice utterance={utterance} pinned={pinned} onTogglePin={togglePin} />
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
          <div className="flex items-center gap-5">
            {/* the ember sits just left of the ledger it points at, its lane
                reserved so the control never moves as it comes and goes.
                Opening the ledger from here goes through the very handler the
                h key uses — one door, so both clear the count identically. */}
            <div className="flex items-center gap-2.5">
              <ReachedOut count={unseen} onOpen={toggleLedger} />
              <button
                onClick={toggleLedger}
                className="cursor-pointer text-[11px] font-medium tracking-[0.1em] text-(--ink-dim) uppercase outline-offset-4 outline-(--ink-dim) hover:text-(--ink) focus-visible:outline"
              >
                h history
              </button>
            </div>
            <button
              onClick={toggleInstrument}
              className="cursor-pointer text-[11px] font-medium tracking-[0.1em] text-(--ink-dim) uppercase outline-offset-4 outline-(--ink-dim) hover:text-(--ink) focus-visible:outline"
            >
              ~ instrument
            </button>
          </div>
        </div>
      </footer>

      {instrument && <Instrument state={state} ticks={ticks} />}
      {ledger && <History history={history} />}
    </main>
  );
}
