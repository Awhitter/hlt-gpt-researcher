import { HLTResearchScope } from "@/types/data";

/**
 * The question bank behind the ask surface.
 *
 * The server returns the whole bank already interleaved by category, so any
 * contiguous window of three is varied. Cycling is a client-side window walk —
 * no refetch, and the first paint is deterministic so SSR and hydration agree.
 */

export type Suggestion = {
  id: string;
  label: string;
  prompt: string;
  /** Pinned scope. Explicit scope beats Auto, so the chip's promise is kept. */
  scope: Partial<HLTResearchScope>;
  category: string;
  source: string;
};

export const WINDOW_SIZE = 3;

/**
 * The `count` suggestions starting at `offset`, wrapping around the bank.
 *
 * Wrapping rather than clamping means the refresh control never dead-ends on a
 * disabled state — there is always a next set, even for a short bank.
 */
export function windowAt(
  bank: Suggestion[],
  offset: number,
  count: number = WINDOW_SIZE,
): Suggestion[] {
  if (bank.length === 0) return [];
  const size = Math.min(count, bank.length);
  const start = ((offset % bank.length) + bank.length) % bank.length;
  return Array.from({ length: size }, (_, i) => bank[(start + i) % bank.length]);
}

/** How many presses of refresh before the bank repeats. */
export function cycleLength(bank: Suggestion[], count: number = WINDOW_SIZE): number {
  if (bank.length === 0) return 0;
  return Math.ceil(bank.length / Math.min(count, bank.length));
}

export async function fetchSuggestions(signal?: AbortSignal): Promise<Suggestion[]> {
  const response = await fetch("/api/brain/suggestions", { signal, cache: "no-store" });
  if (!response.ok) throw new Error(`suggestions: ${response.status}`);
  const data = await response.json();
  return Array.isArray(data?.suggestions) ? (data.suggestions as Suggestion[]) : [];
}
