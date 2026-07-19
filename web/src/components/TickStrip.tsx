import type { TickEvent } from "../state/types";

/**
 * The quiet record. One graphite tally per tick where she woke,
 * considered, and chose not to disturb. Marginalia, not a chart:
 * ragged spacing, ragged height-line, a person's hand — never bars
 * on an axis. Not clickable, not labeled — it is not information to
 * act on, it is evidence of restraint.
 */

// deterministic per-mark jitter: pencil lines are never twice the same
const jitter = (seed: number) => {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
};

export function TickStrip({ ticks }: { ticks: TickEvent[] }) {
  return (
    <div
      aria-hidden
      className="pointer-events-none flex h-6 items-center justify-center select-none"
    >
      {ticks.slice(-56).map((t) =>
        t.tag === "spoke" ? (
          // a spoken tick belongs to the life-stream, not here — a wider breath
          <span key={t.at} style={{ marginLeft: 18 }} />
        ) : (
          <span
            key={t.at}
            className="w-[1.5px]"
            style={{
              // short ticks, all near one length — variation lives in the
              // hand (tilt, drop, spacing), never in "amplitude"
              height: `${6 + jitter(t.at) * 1.5}px`,
              marginLeft: `${3 + jitter(t.at + 3) * 7 + (jitter(t.at + 4) > 0.82 ? 14 : 0)}px`,
              transform: `translateY(${(jitter(t.at + 2) - 0.5) * 5}px) rotate(${(jitter(t.at + 1) - 0.5) * 20}deg)`,
              background: "var(--pencil)",
              opacity: t.tag === "intercepted" ? 0.65 : 0.4,
            }}
          />
        ),
      )}
    </div>
  );
}
