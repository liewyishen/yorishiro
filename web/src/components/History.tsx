import { useEffect, useRef } from "react";
import type { ExchangeEntry } from "../state/types";

/**
 * The looking-back layer. The presence holds only what she is saying now,
 * and must — this is the drawer you pull open to reread, collapsed until
 * asked for, emptied by a reload. Twin of the instrument overlay in
 * mechanism, its opposite in skin: the instrument is a dark console held
 * up to the room, this is a page of the room itself, so it takes the
 * paper and dims with her daylight like every other surface.
 *
 * A ledger, not a chat panel: no bubbles, no avatars, no send box. The
 * only thing that marks who spoke is the face the words are set in —
 * yours in the machine's mono, hers in the serif she is given everywhere
 * else.
 */

interface Exchange {
  at: number;
  you: string | null;
  her: { text: string; self: boolean } | null;
}

/**
 * One thing you said and the reply it drew are a single exchange. A line
 * she started answers nothing, so it stands on its own — which is exactly
 * what should make it visible as hers.
 */
function group(history: ExchangeEntry[]): Exchange[] {
  const out: Exchange[] = [];
  for (const e of history) {
    const last = out[out.length - 1];
    if (e.role === "you") out.push({ at: e.at, you: e.text, her: null });
    else if (!e.self && last && last.her === null) last.her = { text: e.text, self: false };
    else out.push({ at: e.at, you: null, her: { text: e.text, self: e.self } });
  }
  return out;
}

function hhmm(at: number): string {
  return new Date(at).toTimeString().slice(0, 5);
}

export function History({ history }: { history: ExchangeEntry[] }) {
  const sheet = useRef<HTMLDivElement>(null);
  const exchanges = group(history);

  // open at the most recent, then hold still. You came here to read back;
  // a line arriving while you read must not pull the page out from under you
  useEffect(() => {
    const el = sheet.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  return (
    <div
      ref={sheet}
      role="dialog"
      aria-label="history"
      className="fixed inset-0 z-10 flex flex-col overflow-y-auto bg-(--paper)/95 backdrop-blur-[2px]"
    >
      <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col p-6 pt-12">
        {exchanges.length === 0 ? (
          <p className="m-auto text-[13px] text-(--ink-dim)">nothing remembered yet this session</p>
        ) : (
          <div className="flex flex-col gap-7">
            {exchanges.map((x) => (
              <article key={`${x.at}-${x.you === null ? "her" : "you"}`} className="flex gap-4">
                {/* the margin: when, and — only when it was hers to begin — that it was */}
                <div className="w-9 shrink-0 pt-[3px] text-right text-[10px] leading-4 text-(--ink-dim) tabular-nums">
                  <div>{hhmm(x.at)}</div>
                  {x.her?.self && <div title="she started this one">self</div>}
                </div>
                <div className="flex min-w-0 flex-1 flex-col gap-2">
                  {x.you !== null && (
                    <p className="font-mono text-[13px] leading-relaxed break-words text-(--ink-dim)">
                      {x.you}
                    </p>
                  )}
                  {x.her !== null && (
                    <p className="font-serif text-[16px] leading-relaxed break-words text-(--ink)">
                      {x.her.text}
                    </p>
                  )}
                </div>
              </article>
            ))}
          </div>
        )}
      </div>

      <p className="mt-auto pb-6 text-center text-[10px] tracking-[0.1em] text-(--ink-dim) uppercase">
        h presence
      </p>
    </div>
  );
}
