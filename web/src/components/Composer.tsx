import { useState } from "react";

/**
 * One quiet line. The presence is the main character; this is only the
 * doorway in — mono, hairline, nothing that could be mistaken for a
 * chat panel. Enter sends; delivery failure shows as the room's
 * unreachable label, never as a dialog.
 */
export function Composer({ send }: { send: (text: string) => Promise<boolean> }) {
  const [text, setText] = useState("");

  const submit = () => {
    const t = text.trim();
    if (!t) return;
    setText("");
    void send(t);
  };

  return (
    <div className="mb-4 flex justify-center">
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
        placeholder="write to her"
        aria-label="write to her"
        className="w-72 border-b border-(--hairline) bg-transparent pb-1 text-center font-mono text-[12px] text-(--ink) outline-none placeholder:text-(--ink-dim)/50 focus:border-(--ink-dim)"
      />
    </div>
  );
}
