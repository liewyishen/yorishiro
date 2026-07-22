import { useEffect, useRef, useState } from "react";
import type { Utterance } from "../state/types";

/**
 * Her current words, surfacing under the core as they stream and receding
 * after the ripples die — never a chat log. One utterance at a time; a
 * new one simply takes the water. The region sizes to what she's said,
 * up to a few lines; a long utterance scrolls inside its own banks — the
 * view follows the stream until the reader reaches back up. No scrollbar
 * on the paper: a soft fade at an edge means more words lie past it.
 *
 * A held utterance stops receding — the linger and the fade are suspended
 * for as long as the hold lasts. It holds against time only: the hold is
 * released upstream the moment anyone speaks again.
 */

const LINGER_MS = 9000; // the words hold after her last ripple…
const FADE_MS = 2600; // …then recede back into the pool
const EDGE_PX = 18; // depth of the more-words-beyond-here fade
const HINT_MS = 6000; // the mark is pointed at once, for about as long as you'd read a line
const HINT_FADE_MS = 1200; // …and withdrawn slowly enough not to look like a bug

function mask(up: boolean, down: boolean): string | undefined {
  if (up && down)
    return `linear-gradient(to bottom, transparent, black ${EDGE_PX}px, black calc(100% - ${EDGE_PX}px), transparent)`;
  if (up) return `linear-gradient(to bottom, transparent, black ${EDGE_PX}px, black)`;
  if (down) return `linear-gradient(to bottom, black, black calc(100% - ${EDGE_PX}px), transparent)`;
  return undefined;
}

export function Voice({
  utterance,
  pinned,
  onTogglePin,
}: {
  utterance: Utterance | null;
  pinned: boolean;
  onTogglePin: () => void;
}) {
  const [shown, setShown] = useState<Utterance | null>(null);
  const [fading, setFading] = useState(false);
  const [edges, setEdges] = useState({ up: false, down: false });
  // what the mark is for is worth saying once and then never again. Lives
  // here rather than upstream because it is about this room's first minute,
  // not about her: mounted for the life of the page, so a reload re-arms it.
  const [hint, setHint] = useState<"armed" | "up" | "receding" | "gone">("armed");
  const banks = useRef<HTMLDivElement>(null);
  const follow = useRef(true); // stick to the newest words until the reader scrolls up

  const measure = () => {
    const el = banks.current;
    if (!el) return;
    const up = el.scrollTop > 2;
    const down = el.scrollTop + el.clientHeight < el.scrollHeight - 2;
    setEdges((prev) => (prev.up === up && prev.down === down ? prev : { up, down }));
  };

  useEffect(() => {
    if (!utterance) return;
    setShown(utterance);
    if (utterance.live) {
      setFading(false);
      if (utterance.text === "") follow.current = true; // a fresh utterance starts followed
    }
  }, [utterance]);

  // follow the stream: keep the newest words in view unless the reader
  // has reached up to reread — their place is theirs until they let go
  useEffect(() => {
    const el = banks.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
    measure();
  }, [shown]);

  // a hold can land while the words are already receding — it pulls them
  // back up rather than freezing them half-gone
  useEffect(() => {
    if (pinned) setFading(false);
  }, [pinned]);

  // the first line she finishes is the one that gets to point at the mark —
  // mid-stream would be asking you to read two things at once
  useEffect(() => {
    if (hint === "armed" && shown && !shown.live && shown.text) setHint("up");
  }, [hint, shown]);

  // holding anything at all is the lesson landing; if it landed before the
  // hint was ever up, the hint has nothing left to say and never appears
  useEffect(() => {
    if (!pinned) return;
    setHint((h) => (h === "up" ? "receding" : h === "armed" ? "gone" : h));
  }, [pinned]);

  useEffect(() => {
    if (hint !== "up" && hint !== "receding") return;
    const done = setTimeout(
      () => setHint(hint === "up" ? "receding" : "gone"),
      hint === "up" ? HINT_MS : HINT_FADE_MS,
    );
    return () => clearTimeout(done);
  }, [hint]);

  // held words don't linger, because lingering is what ends in fading.
  // Letting go re-arms the full linger: you have only just stopped reading.
  useEffect(() => {
    if (!shown || shown.live || pinned || fading) return;
    const linger = setTimeout(() => setFading(true), LINGER_MS);
    return () => clearTimeout(linger);
  }, [shown, fading, pinned]);

  useEffect(() => {
    if (!fading) return;
    const gone = setTimeout(() => {
      setShown(null);
      setFading(false);
    }, FADE_MS);
    return () => clearTimeout(gone);
  }, [fading]);

  if (!shown || !shown.text) return null;
  const edgeMask = mask(edges.up, edges.down);
  return (
    <div
      ref={banks}
      onScroll={() => {
        const el = banks.current;
        if (el) follow.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 12;
        measure();
      }}
      className="group max-h-full overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden transition-opacity motion-reduce:transition-none"
      style={{
        opacity: fading ? 0 : 1,
        transitionDuration: `${FADE_MS}ms`,
        maskImage: edgeMask,
        WebkitMaskImage: edgeMask,
      }}
    >
      <p className="max-w-xl px-6 text-center font-serif text-[16px] leading-relaxed text-(--ink)">
        {shown.text}
      </p>
      {/* the hold. It sits under her last line rather than out in the
          margin — her words are centred, so for a short line the margin is
          nowhere near them. Always there once she has said something: an
          affordance nobody can find is not an affordance. Solid only when
          held, so the same mark is both the control and the sign that these
          words are no longer going anywhere.

          Clicking it is the way in. The key still works, but it is the
          quiet path now: you keep the composer focused while you talk to
          her, and a bare letter belongs to her before it belongs to a
          shortcut. So the mark has to carry the whole affordance itself —
          hence a real 24px target under a 13px glyph, and a halo that
          answers the cursor.

          The row is a fixed height and the hint's lane is reserved on both
          sides whether or not anything is in it, so the ring stays exactly
          centred and the core above never moves when the hint comes or
          goes. Reserved rather than absolutely positioned because her box
          is only as wide as her words, and a floated hint would spill out
          of it — and be clipped — on a short line. */}
      {/* leading-none is load-bearing: without it the hint's lane inherits
          the room's 24px line strut, outgrows this row's content box, and
          spills past the bottom of her banks — enough to arm the
          more-words-below fade over an utterance that has nothing below it */}
      <div className="flex h-8 items-center justify-center pt-2 leading-none">
        <div className="w-28 shrink-0" />
        <button
          onClick={onTogglePin}
          aria-pressed={pinned}
          aria-label={pinned ? "let her words recede" : "hold her words"}
          title={pinned ? "held — click to let go" : "click to hold"}
          className={`flex h-6 w-6 shrink-0 cursor-pointer items-center justify-center rounded-full text-[13px] leading-none text-(--ink-dim) outline-offset-2 outline-(--ink-dim) transition hover:scale-110 hover:bg-(--ink-dim)/10 focus-visible:opacity-100 focus-visible:outline motion-reduce:transition-none ${
            pinned ? "opacity-100" : "opacity-45 hover:opacity-90"
          }`}
        >
          {pinned ? "●" : "○"}
        </button>
        <div className="w-28 shrink-0 pl-2 text-left">
          {(hint === "up" || hint === "receding") && (
            <span
              className="font-mono text-[10px] tracking-[0.06em] whitespace-nowrap text-(--ink-dim) transition-opacity motion-reduce:transition-none"
              style={{ opacity: hint === "up" ? 0.55 : 0, transitionDuration: `${HINT_FADE_MS}ms` }}
            >
              click to hold
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
