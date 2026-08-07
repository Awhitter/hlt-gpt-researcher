"use client";

import { HLTResearchScope } from "@/types/data";
import { normalizeHLTResearchScope, selectedScopeCount } from "@/lib/hltResearchScope";

type ResearchScopeSelectorProps = {
  value?: HLTResearchScope;
  onChange: (next: HLTResearchScope) => void;
  compact?: boolean;
};

type ScopeKey =
  | "firecrawl"
  | "qbank"
  | "media"
  | "codebase"
  | "cms"
  | "metrics"
  | "audience"
  | "recruiting";

const scopeOptions: Array<{
  key: ScopeKey;
  label: string;
  title: string;
}> = [
  {
    key: "firecrawl",
    label: "Deep web",
    title: "Use deeper public web extraction.",
  },
  {
    key: "audience",
    label: "Nurse voice",
    title:
      "Ground answers in what nurses actually say: forums (r/nursing, r/StudentNurse, allnurses), verbatim quotes with receipts, plus the internal voice-of-nurse corpus. Nursing only — this does not cover ASVAB, PANCE or DAT candidates.",
  },
  {
    key: "recruiting",
    label: "Recruiting",
    title:
      "Specialize in nurse recruiting: nursingmastery.com content inventory, gap analysis vs the best recruiting content anywhere, audience cross-checks.",
  },
  {
    key: "qbank",
    label: "QBank",
    title:
      "Use read-only corporate CMS and question-bank context through the protected Katailyst tool path.",
  },
  {
    key: "media",
    label: "Media",
    title: "Search the Cloudinary media library through the server-side HLT media connection.",
  },
  {
    key: "codebase",
    label: "Code",
    title:
      "Search current source across Mastery Research, HLT Account API, Nursing Mastery, ScraperVault, Katailyst2, MMM2, and EBB. Authority depends on the field or workflow.",
  },
  {
    key: "cms",
    label: "Registry",
    title:
      "Search Katailyst2 entities, playbooks, docs, skills, and knowledge-base context.",
  },
  {
    key: "metrics",
    label: "Metrics",
    title:
      "Include analytics and performance context when metrics access is configured.",
  },
];

const depthOptions: Array<{ value: HLTResearchScope["depth"]; label: string }> =
  [
    { value: "fast", label: "Fast" },
    { value: "balanced", label: "Balanced" },
    { value: "deep", label: "Deep" },
  ];

const modeOptions: Array<{
  value: HLTResearchScope["mode"];
  label: string;
  title: string;
}> = [
  { value: "standard", label: "Standard", title: "Regular cited research." },
  {
    value: "top1",
    label: "Top 1%",
    title:
      "Rhyme mode: find the best examples anywhere on earth, distill why they win, propose how the mechanism maps to nursing, verify against audience truth.",
  },
];

export default function ResearchScopeSelector({
  value,
  onChange,
  compact = false,
}: ResearchScopeSelectorProps) {
  const scope = normalizeHLTResearchScope(value);

  const update = (patch: Partial<HLTResearchScope>) => {
    onChange({ ...scope, ...patch });
  };

  // The folded row still has to say what is in force, or collapsing the
  // controls would hide state rather than tidy it.
  const pinned = selectedScopeCount(scope);
  const depthLabel = depthOptions.find((o) => o.value === scope.depth)?.label ?? "Balanced";
  const modeLabel = modeOptions.find((o) => o.value === scope.mode)?.label ?? "Standard";
  const summaryLabel = [
    pinned > 0 ? `${pinned} scope${pinned === 1 ? "" : "s"} pinned` : "Auto",
    depthLabel,
    modeLabel,
  ].join(" · ");

  const setAuto = (auto: boolean) => {
    if (auto) {
      // Auto mode releases every pinned scope; the server infers what the
      // question needs and reports what it activated mid-run.
      update({
        auto: true,
        codebase: false,
        cms: false,
        qbank: false,
        metrics: false,
        firecrawl: false,
        media: false,
        audience: false,
        recruiting: false,
      });
    } else {
      update({ auto: false });
    }
  };

  return (
    <section
      className={`mx-auto min-w-0 w-full max-w-[900px] overflow-x-hidden sm:overflow-visible ${compact ? "mt-4" : "mt-0"}`}
      aria-label="Research scope"
    >
      <div className="flex flex-col items-center justify-center gap-2.5">
        {/*
          Auto is the only control most people ever need, so it is the only one
          that stays out. The other twelve made the first screen read like a
          settings panel; they are one click away and the summary below reports
          their state so nothing is hidden, only folded.
        */}
        <button
          type="button"
          title="Let Mastery decide: estate code, registry, metrics, media, or nurse-voice context is pulled in automatically when the question needs it — plain web questions stay web-only. Pin a scope under Advanced to take over."
          aria-pressed={scope.auto}
          onClick={() => setAuto(!scope.auto)}
          className={`flex h-8 items-center gap-1.5 rounded-md border px-3 text-xs font-semibold transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-[#06101C] ${
            scope.auto
              ? "border-teal-400/75 bg-teal-400/15 text-teal-100"
              : "border-white/10 bg-white/[0.04] text-slate-300 hover:border-white/20 hover:bg-white/[0.07]"
          }`}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-3 w-3"
            aria-hidden="true"
          >
            <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3Z" />
            <path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15Z" />
          </svg>
          Auto
        </button>

        <details className="group w-full">
          <summary className="mx-auto flex w-fit cursor-pointer list-none items-center gap-1.5 rounded-md px-2 py-1 text-xs text-slate-500 transition-colors hover:text-slate-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-[#06101C] [&::-webkit-details-marker]:hidden">
            <svg
              viewBox="0 0 12 12"
              aria-hidden="true"
              className="h-2.5 w-2.5 transition-transform duration-200 ease-out group-open:rotate-90 motion-reduce:transition-none"
            >
              <path d="M4 2l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span>{summaryLabel}</span>
          </summary>

          <div className="mt-3 flex flex-col items-center justify-center gap-2.5">
        <div className="flex flex-wrap items-center justify-center gap-2">
          <div className="inline-flex w-fit rounded-md border border-white/10 bg-white/[0.04] p-0.5 backdrop-blur">
            {depthOptions.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => update({ depth: option.value })}
                className={`h-7 rounded px-3 text-xs font-semibold transition-colors ${
                  scope.depth === option.value
                    ? "bg-[#155EEF] text-white"
                    : "text-slate-400 hover:text-white"
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
          <div className="inline-flex w-fit rounded-md border border-white/10 bg-white/[0.04] p-0.5 backdrop-blur">
            {modeOptions.map((option) => (
              <button
                key={option.value}
                type="button"
                title={option.title}
                onClick={() => update({ mode: option.value })}
                className={`h-7 rounded px-3 text-xs font-semibold transition-colors ${
                  scope.mode === option.value
                    ? option.value === "top1"
                      ? "bg-amber-500/90 text-black"
                      : "bg-[#155EEF] text-white"
                    : "text-slate-400 hover:text-white"
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex min-w-0 max-w-full snap-x items-center justify-start gap-1.5 overflow-x-auto pb-2 sm:flex-wrap sm:justify-center sm:overflow-visible">
          {scopeOptions.map((option) => {
            const selected = scope[option.key];
            return (
              <label
                key={option.key}
                title={option.title}
                className={`group flex h-8 cursor-pointer items-center gap-1.5 rounded-md border px-2.5 transition-all ${
                  selected
                    ? "bg-[#155EEF]/14 border-[#155EEF]/75 text-white"
                    : "border-white/10 bg-white/[0.04] text-slate-300 hover:border-white/20 hover:bg-white/[0.07]"
                }`}
              >
                <span className="truncate text-xs font-medium">
                  {option.label}
                </span>
                <span
                  className="flex h-4 w-4 items-center justify-center rounded-full border border-white/10 text-slate-500 transition-colors group-hover:text-slate-300"
                  aria-hidden="true"
                >
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="h-2.5 w-2.5"
                  >
                    <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                </span>
                <span
                  className={`flex h-4 w-7 shrink-0 items-center rounded-full p-0.5 transition-colors ${
                    selected ? "bg-[#155EEF]" : "bg-slate-700/80"
                  }`}
                >
                  <span
                    className={`h-3 w-3 rounded-full bg-white transition-transform ${
                      selected ? "translate-x-3" : "translate-x-0"
                    }`}
                  />
                </span>
                <input
                  type="checkbox"
                  className="sr-only"
                  checked={selected}
                  onChange={() =>
                    // Pinning any scope leaves auto mode; the manual selection wins.
                    update({
                      [option.key]: !selected,
                      auto: false,
                    } as Partial<HLTResearchScope>)
                  }
                />
              </label>
            );
          })}
        </div>
          </div>
        </details>
      </div>
    </section>
  );
}
