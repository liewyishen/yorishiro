/**
 * The trace of a visit. When she speaks first, her words surface at the
 * centre and then recede — and if no one was looking, that reach-out is
 * simply gone. This is what it leaves behind: a soft warm ember near the
 * ledger, breathing slowly, saying only "she came by." Not an alert —
 * there is no red, no sound, no bounce; the breath is its whole voice.
 *
 * The lane is a fixed width whether or not the ember is in it, so the
 * footer never shifts as the count appears, grows, or clears — the same
 * reserved-space discipline the Voice's hint lane keeps. When the count
 * is zero there is nothing here at all, only the held space.
 */
export function ReachedOut({ count, onOpen }: { count: number; onOpen: () => void }) {
  const label =
    count > 1 ? `she reached out ${count} times — open history` : "she reached out — open history";
  return (
    <div className="flex h-4 w-7 shrink-0 items-center justify-end">
      {count > 0 && (
        <button
          onClick={onOpen}
          aria-label={label}
          title="she came by — open history"
          className="group flex cursor-pointer items-center gap-1 rounded-full outline-offset-2 outline-(--ink-dim) focus-visible:outline"
        >
          {/* a single visit is just the ember; a count only earns a numeral
              once there is more than one thing waiting to be read */}
          {count > 1 && (
            <span className="font-mono text-[10px] leading-none text-(--ink-dim) tabular-nums">
              {count}
            </span>
          )}
          <span className="reached-ember block h-2 w-2 rounded-full bg-(--ember)" />
        </button>
      )}
    </div>
  );
}
