"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Data } from "../../types/data";
import {
  PHASES,
  deriveProgress,
} from "../../lib/researchProgress";

/**
 * Phase rail for a live research run. Derives Plan / Search / Read / Write
 * progress from the raw websocket event stream so non-technical teammates
 * see a readable storyline instead of a log firehose.
 */

const SCOPE_LABELS: Record<string, string> = {
  codebase: "Code",
  cms: "Registry",
  qbank: "QBank",
  metrics: "Metrics",
  firecrawl: "Deep web",
  media: "Media",
  audience: "Audience",
  recruiting: "Recruiting",
};

const formatElapsed = (seconds: number) => {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}m ${s.toString().padStart(2, "0")}s` : `${s}s`;
};

interface ResearchProgressProps {
  orderedData: Data[];
  loading: boolean;
}

export default function ResearchProgress({
  orderedData,
  loading,
}: ResearchProgressProps) {
  const derived = useMemo(() => deriveProgress(orderedData), [orderedData]);
  const startRef = useRef<number>(Date.now());
  const [elapsed, setElapsed] = useState(0);
  const finishedAtRef = useRef<number | null>(null);

  // Three states, not two. `settled` stops the clock; only `succeeded` is
  // allowed to paint the rail green. A failed run used to satisfy the old
  // `finished` and render as four completed phases.
  const failed = Boolean(derived.error);
  const settled = derived.done || !loading;
  const succeeded = settled && !failed;
  const finished = settled;

  useEffect(() => {
    if (finished) {
      if (finishedAtRef.current === null) {
        finishedAtRef.current = Math.floor(
          (Date.now() - startRef.current) / 1000,
        );
        setElapsed(finishedAtRef.current);
      }
      return;
    }
    const timer = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    }, 1000);
    return () => clearInterval(timer);
  }, [finished]);

  if (orderedData.length === 0) return null;

  return (
    <div className="container mt-2 w-full rounded-lg border border-solid border-gray-700/35 bg-black/25 p-4 shadow-lg backdrop-blur-md">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-1 items-center">
          {PHASES.map((phase, index) => {
            const isComplete = succeeded || index < derived.reachedIndex;
            const isFailed = failed && index === derived.reachedIndex;
            const isCurrent = !settled && index === derived.reachedIndex;
            return (
              <div key={phase.id} className="flex flex-1 items-center last:flex-none">
                <div className="group relative flex items-center gap-2" title={phase.hint}>
                  <span
                    className={`flex h-6 w-6 items-center justify-center rounded-full border text-[10px] font-bold transition-colors ${
                      isFailed
                        ? "border-rose-400/70 bg-rose-500/20 text-rose-200"
                        : isComplete
                          ? "border-teal-500/60 bg-teal-500/20 text-teal-300"
                          : isCurrent
                            ? "border-teal-400 bg-teal-400/10 text-teal-200"
                            : "border-white/15 bg-white/[0.03] text-slate-500"
                    }`}
                  >
                    {isFailed ? (
                      <span aria-hidden="true">!</span>
                    ) : isComplete ? (
                      <svg viewBox="0 0 12 12" className="h-3 w-3 fill-current" aria-hidden="true">
                        <path d="M4.5 8.1 2.4 6l-.9.9 3 3 6-6-.9-.9z" />
                      </svg>
                    ) : isCurrent ? (
                      <span className="h-2 w-2 animate-pulse rounded-full bg-teal-300" />
                    ) : (
                      index + 1
                    )}
                  </span>
                  <span
                    className={`whitespace-nowrap text-xs font-semibold uppercase tracking-wide ${
                      isFailed
                        ? "text-rose-200"
                        : isCurrent
                          ? "text-teal-200"
                          : isComplete
                            ? "text-slate-300"
                            : "text-slate-600"
                    }`}
                  >
                    {phase.label}
                  </span>
                </div>
                {index < PHASES.length - 1 && (
                  <div
                    className={`mx-3 h-px flex-1 ${
                      succeeded || index < derived.reachedIndex
                        ? "bg-teal-500/40"
                        : "bg-white/10"
                    }`}
                  />
                )}
              </div>
            );
          })}
        </div>
        <div className="flex shrink-0 items-center gap-4 text-xs text-slate-400">
          {derived.subqueryCount > 0 && (
            <span title="Sub-questions the researcher is answering">
              {derived.subqueryCount} questions
            </span>
          )}
          {derived.sourceCount > 0 && (
            <span title="Unique sources found so far">
              {derived.sourceCount} sources
            </span>
          )}
          <span title="Elapsed time" className="tabular-nums">
            {formatElapsed(elapsed)}
          </span>
        </div>
      </div>
      {derived.error && (
        <div
          role="alert"
          className="mt-3 rounded-md border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-xs"
        >
          <span className="font-semibold uppercase tracking-wide text-rose-300">
            Run failed
          </span>
          <p className="mt-1 break-words text-rose-100/90">{derived.error}</p>
          <p className="mt-1 text-rose-200/70">
            Nothing was saved. Ask again — if it keeps failing, send Alec this message.
          </p>
        </div>
      )}
      {derived.scope && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs text-slate-400">
          <span className="font-semibold uppercase tracking-wide text-slate-500">
            {derived.scope.auto ? "Auto-detected" : "Scope"}
          </span>
          {derived.scope.active.length > 0 ? (
            derived.scope.active.map((key) => {
              const reasons = derived.scope?.reasons?.[key] || [];
              const reasonText = reasons.length > 0 ? reasons.join("; ") : "";
              return (
                <span
                  key={key}
                  className="rounded border border-teal-400/40 bg-teal-400/10 px-1.5 py-0.5 font-medium text-teal-200"
                  title={
                    reasonText ||
                    (derived.scope?.auto
                      ? "Pulled in automatically because the question needs it"
                      : "Pinned before the run")
                  }
                >
                  {SCOPE_LABELS[key] || key}
                  {reasonText ? (
                    <span className="ml-1 font-normal text-teal-200/70">
                      ({reasons[0]})
                    </span>
                  ) : null}
                </span>
              );
            })
          ) : (
            <span title="No internal context needed for this question">
              public web only
            </span>
          )}
        </div>
      )}
      {!finished && (
        <p className="mt-3 truncate text-xs leading-5 text-slate-400">
          <span className="mr-2 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-teal-300 align-middle" />
          {derived.latestLabel}
        </p>
      )}
    </div>
  );
}
