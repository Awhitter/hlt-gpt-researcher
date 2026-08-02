"use client";

import { useEffect, useMemo, useState } from "react";
import { extractReportSources, isChangeOrientedQuestion } from "@/lib/reportTrust";

type Props = { question: string; answer: string };

export default function ChangeRequest({ question, answer }: Props) {
  const sources = useMemo(() => extractReportSources(answer), [answer]);
  const repositories = Array.from(new Set(sources.map((source) => source.repo)));
  const [open, setOpen] = useState(false);
  const [requestedChange, setRequestedChange] = useState("");
  const [targetRepository, setTargetRepository] = useState(repositories[0] || "");
  const [submitting, setSubmitting] = useState(false);
  const [receipt, setReceipt] = useState<{ identifier?: string; url: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!targetRepository && repositories.length > 0) {
      setTargetRepository(repositories[0]);
    }
  }, [repositories, targetRepository]);

  if (!isChangeOrientedQuestion(question) || !answer) return null;

  const submit = async () => {
    if (!requestedChange.trim() || !targetRepository || sources.length === 0) return;
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/brain/change-request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirmed: true,
          question,
          requestedChange: requestedChange.trim(),
          targetRepository,
          sources,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Linear request could not be created.");
      setReceipt(body);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Linear request could not be created.");
    } finally {
      setSubmitting(false);
    }
  };

  if (receipt) {
    return (
      <div className="my-6 rounded-xl border border-emerald-400/25 bg-emerald-400/[0.06] p-4 text-sm text-emerald-100">
        Change request created{receipt.identifier ? ` · ${receipt.identifier}` : ""}.{" "}
        <a href={receipt.url} target="_blank" rel="noreferrer" className="underline underline-offset-4">Open in Linear</a>
      </div>
    );
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="my-6 rounded-lg border border-[#155EEF]/50 bg-[#155EEF]/10 px-4 py-2 text-sm font-semibold text-blue-100 hover:bg-[#155EEF]/20"
      >
        Request a change
      </button>
    );
  }

  return (
    <section className="my-6 rounded-2xl border border-white/10 bg-white/[0.035] p-4 text-white" aria-label="Confirm change request">
      <h3 className="text-sm font-semibold">Confirm a Linear request</h3>
      <p className="mt-1 text-xs text-slate-400">This creates an issue with the cited research. It does not edit code or production data.</p>
      {sources.length === 0 ? (
        <p className="mt-4 rounded-lg bg-amber-400/10 px-3 py-2 text-xs text-amber-100">A validated, commit-specific source is required before a change request can be created.</p>
      ) : (
        <>
          <textarea
            value={requestedChange}
            onChange={(event) => setRequestedChange(event.target.value)}
            placeholder="Describe the change you want"
            className="mt-4 min-h-24 w-full resize-y rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm outline-none focus:border-[#155EEF]"
          />
          <select
            value={targetRepository}
            onChange={(event) => setTargetRepository(event.target.value)}
            className="mt-3 w-full rounded-lg border border-white/10 bg-[#101B26] px-3 py-2 text-sm"
            aria-label="Target repository"
          >
            {repositories.map((repo) => <option key={repo} value={repo}>{repo}</option>)}
          </select>
        </>
      )}
      {error && <p className="mt-3 text-xs text-red-300">{error}</p>}
      <div className="mt-4 flex gap-2">
        <button type="button" onClick={() => void submit()} disabled={submitting || !requestedChange.trim() || !targetRepository || sources.length === 0} className="rounded-lg bg-[#155EEF] px-4 py-2 text-xs font-semibold disabled:opacity-40">
          {submitting ? "Creating…" : "Confirm and create"}
        </button>
        <button type="button" onClick={() => setOpen(false)} className="rounded-lg px-4 py-2 text-xs text-slate-400 hover:text-white">Cancel</button>
      </div>
    </section>
  );
}
