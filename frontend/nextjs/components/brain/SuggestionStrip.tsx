"use client";

import { useEffect, useState } from "react";

import {
  Suggestion,
  WINDOW_SIZE,
  cycleLength,
  fetchSuggestions,
  windowAt,
} from "@/lib/suggestions";

/**
 * Three example questions under the ask box, with a control to cycle to the
 * next three.
 *
 * The tool could already answer estate questions; nobody knew to ask. The bank
 * is built server-side from live sources (Linear, the weekly content sweep, the
 * code graph) so it does not go stale, and every entry pins the scope it needs
 * — clicking one is a real, correctly-routed run, not a text prefill.
 */

type Props = {
  onSelect: (suggestion: Suggestion) => void;
};

const CHIP =
  "rounded-full border px-3 py-1.5 text-left text-xs transition duration-200 " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400/70 " +
  "focus-visible:ring-offset-2 focus-visible:ring-offset-[#06101C]";

export default function SuggestionStrip({ onSelect }: Props) {
  const [bank, setBank] = useState<Suggestion[]>([]);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    fetchSuggestions(controller.signal)
      .then(setBank)
      .catch(() => setBank([])) // a nicety failing must not shout at the user
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  if (loading) {
    return (
      <div className="mt-5 flex flex-wrap justify-center gap-2" aria-hidden="true">
        {Array.from({ length: WINDOW_SIZE }).map((_, i) => (
          <span
            key={i}
            className="h-[30px] animate-pulse rounded-full border border-white/10 bg-white/[0.03]"
            style={{ width: `${9 + i * 3}rem` }}
          />
        ))}
      </div>
    );
  }

  // No bank means the estate probes are all down. Say nothing rather than
  // showing an error for something nobody asked for.
  if (bank.length === 0) return null;

  const shown = windowAt(bank, offset);
  const canCycle = cycleLength(bank) > 1;

  return (
    <div className="mt-5">
      <div
        className="flex flex-wrap items-center justify-center gap-2"
        role="group"
        aria-label="Example questions"
      >
        {shown.map((suggestion) => (
          <button
            key={suggestion.id}
            type="button"
            title={suggestion.prompt}
            onClick={() => onSelect(suggestion)}
            className={`${CHIP} border-white/15 text-slate-300 hover:border-teal-400/40 hover:text-white active:border-teal-400/60`}
          >
            {suggestion.label}
          </button>
        ))}

        {canCycle && (
          <button
            type="button"
            onClick={() => setOffset((prev) => prev + WINDOW_SIZE)}
            aria-label="Show different questions"
            title="Show different questions"
            className={`${CHIP} group border-white/10 px-2 text-slate-400 hover:border-white/25 hover:text-slate-100`}
          >
            <svg
              viewBox="0 0 16 16"
              aria-hidden="true"
              className="h-3.5 w-3.5 transition-transform duration-300 ease-out group-hover:rotate-90 group-active:rotate-180 motion-reduce:transition-none motion-reduce:group-hover:rotate-0 motion-reduce:group-active:rotate-0"
            >
              <path
                d="M13.5 8a5.5 5.5 0 1 1-1.61-3.89M13.5 2.5V6H10"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}
