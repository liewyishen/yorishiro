import type { ReactNode } from "react";
import {
  bodyHsl,
  breathAmpl,
  breathPeriodS,
  daylight,
  hsl,
  poolHsl,
  presenceWords,
  retractDurationS,
  retractReachPx,
  ringHsl,
  rippleDurationS,
  rippleStrength,
  rotationPeriodS,
  warmth,
} from "../lib/projection";
import type { HerState, TickEvent, TickTag } from "../state/types";

/**
 * The other mode. Presence hides the numbers; this shows all of them.
 * Deliberately dark in both day and night: an operator console you hold
 * up to the room, not a page of the room itself. Derived values come
 * from the same projection functions that drive the body, so the panel
 * can only ever say what the material is already doing.
 */

function Panel({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section className="relative border border-[#2a2724] bg-[#12100e] p-3 pt-4">
      <h2 className="absolute -top-[8px] left-2 bg-[#12100e] px-1 text-[10px] tracking-[0.09em] text-[#8f8779] uppercase">
        {label}
      </h2>
      {children}
    </section>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-4 text-[11px] leading-5">
      <span className="text-[#8a8173]">{k}</span>
      <span className="text-[#d6cfc2]">{v}</span>
    </div>
  );
}

const TAG_TONE: Record<TickTag, string> = {
  spoke: "", // her own warmth — set inline, it follows the valence band
  silent: "text-[#9c948a]",
  intercepted: "text-[#6b6459]",
};

function hhmmss(at: number): string {
  return new Date(at).toTimeString().slice(0, 8);
}

export function Instrument({ state, ticks }: { state: HerState; ticks: TickEvent[] }) {
  const band = bodyHsl(state.valence);
  const accent = hsl(band);

  const counts: Record<TickTag, number> = { spoke: 0, silent: 0, intercepted: 0 };
  for (const t of ticks) counts[t.tag] += 1;
  const total = Math.max(ticks.length, 1);

  return (
    <div
      role="dialog"
      aria-label="instrument"
      className="fixed inset-0 z-10 flex flex-col overflow-y-auto bg-[#0b0a09]/90 backdrop-blur-[2px]"
    >
      <div className="mx-auto grid w-full max-w-3xl grid-cols-1 gap-4 p-6 pt-12 sm:grid-cols-2">
        <Panel label="raw state">
          <Row k="presence" v={state.presence} />
          <Row k="tick_interval_seconds" v={String(state.tick_interval_seconds)} />
          <Row k="valence" v={state.valence.toFixed(3)} />
          <Row k="arousal" v={state.arousal.toFixed(3)} />
          <Row k="activity_detail" v={state.activity_detail || "—"} />
          <Row k="ticks_recorded" v={String(ticks.length)} />
        </Panel>

        <Panel label="projection">
          <Row k="warmth" v={`${(warmth(state.valence) * 100).toFixed(0)} %`} />
          <Row k="pool" v={hsl(poolHsl(state.valence, 1))} />
          <Row k="ring" v={hsl(ringHsl(state.valence, 1))} />
          <Row k="breath" v={`${breathPeriodS(state).toFixed(1)} s ± ${(breathAmpl(state) * 100).toFixed(1)} %`} />
          <Row k="rotation" v={`${rotationPeriodS(state).toFixed(0)} s`} />
          <Row k="ripple" v={`${rippleDurationS(state).toFixed(1)} s @ ${(rippleStrength(state) * 100).toFixed(0)} %`} />
          <Row k="retract" v={`${retractReachPx(state).toFixed(0)} px · ${retractDurationS(state).toFixed(1)} s`} />
          <Row k="daylight" v={`${(daylight(state) * 100).toFixed(0)} %`} />
          <Row k="words" v={presenceWords(state)} />
        </Panel>

        <Panel label="tick log">
          <div className="flex flex-col-reverse">
            {ticks.slice(-12).map((t) => (
              <div key={t.at} className="flex justify-between text-[11px] leading-5">
                <span className="text-[#8a8173]">{hhmmss(t.at)}</span>
                <span
                  className={TAG_TONE[t.tag]}
                  style={t.tag === "spoke" ? { color: accent } : undefined}
                >
                  {t.tag === "spoke" && t.self ? "spoke · self" : t.tag}
                </span>
              </div>
            ))}
          </div>
        </Panel>

        <Panel label="distribution">
          {(["silent", "intercepted", "spoke"] as const).map((tag) => (
            <div key={tag} className="flex items-center gap-2 text-[11px] leading-5">
              <span className="w-24 text-[#8a8173]">{tag}</span>
              <div className="h-[5px] flex-1 bg-white/[0.05]">
                <div
                  className="h-full"
                  style={{
                    width: `${(counts[tag] / total) * 100}%`,
                    background: tag === "spoke" ? accent : "rgb(255 255 255 / 0.22)",
                  }}
                />
              </div>
              <span className="w-8 text-right text-[#9c948a]">{counts[tag]}</span>
            </div>
          ))}
          <p className="mt-3 text-[10px] tracking-[0.07em] text-[#8a8173]">
            urge distribution — waits on L1
          </p>
        </Panel>
      </div>

      <p className="mt-auto pb-6 text-center text-[10px] tracking-[0.1em] text-[#8f8779] uppercase">
        ~ presence
      </p>
    </div>
  );
}
