import { useEffect, useState } from "react";
import type { Utterance } from "../state/types";

/**
 * Her current words, surfacing under the core as they stream and receding
 * after the ripples die — never a chat log. One utterance at a time; a
 * new one simply takes the water.
 */

const LINGER_MS = 9000; // the words hold after her last ripple…
const FADE_MS = 2600; // …then recede back into the pool

export function Voice({ utterance }: { utterance: Utterance | null }) {
  const [shown, setShown] = useState<Utterance | null>(null);
  const [fading, setFading] = useState(false);

  useEffect(() => {
    if (!utterance) return;
    setShown(utterance);
    if (utterance.live) setFading(false);
  }, [utterance]);

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
  return (
    <p
      className="max-w-xl px-6 text-center font-serif text-[16px] leading-relaxed text-(--ink) transition-opacity motion-reduce:transition-none"
      style={{ opacity: fading ? 0 : 1, transitionDuration: `${FADE_MS}ms` }}
    >
      {shown.text}
    </p>
  );
}
