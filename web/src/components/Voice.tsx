import { useEffect, useRef, useState } from "react";
import type { Utterance } from "../state/types";

/**
 * Her current words, surfacing under the core as they stream and receding
 * after the ripples die — never a chat log. One utterance at a time; a
 * new one simply takes the water. The region sizes to what she's said,
 * up to a few lines; a long utterance scrolls inside its own banks — the
 * view follows the stream until the reader reaches back up. No scrollbar
 * on the paper: a soft fade at an edge means more words lie past it.
 */

const LINGER_MS = 9000; // the words hold after her last ripple…
const FADE_MS = 2600; // …then recede back into the pool
const EDGE_PX = 18; // depth of the more-words-beyond-here fade

function mask(up: boolean, down: boolean): string | undefined {
  if (up && down)
    return `linear-gradient(to bottom, transparent, black ${EDGE_PX}px, black calc(100% - ${EDGE_PX}px), transparent)`;
  if (up) return `linear-gradient(to bottom, transparent, black ${EDGE_PX}px, black)`;
  if (down) return `linear-gradient(to bottom, black, black calc(100% - ${EDGE_PX}px), transparent)`;
  return undefined;
}

export function Voice({ utterance }: { utterance: Utterance | null }) {
  const [shown, setShown] = useState<Utterance | null>(null);
  const [fading, setFading] = useState(false);
  const [edges, setEdges] = useState({ up: false, down: false });
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

  useEffect(() => {
    if (!shown || shown.live || fading) return;
    const linger = setTimeout(() => setFading(true), LINGER_MS);
    return () => clearTimeout(linger);
  }, [shown, fading]);

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
      className="max-h-[6.5rem] overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden transition-opacity motion-reduce:transition-none"
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
    </div>
  );
}
