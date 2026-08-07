import { HLTResearchScope } from "@/types/data";

export const defaultHLTResearchScope: HLTResearchScope = {
  auto: true,
  codebase: false,
  cms: false,
  qbank: false,
  metrics: false,
  firecrawl: false,
  media: false,
  audience: false,
  recruiting: false,
  depth: "balanced",
  mode: "standard",
};

export function normalizeHLTResearchScope(
  scope?: Partial<HLTResearchScope>,
): HLTResearchScope {
  return {
    ...defaultHLTResearchScope,
    ...(scope || {}),
  };
}

/**
 * The eight pinnable sources — everything in HLTResearchScope except `auto`,
 * `depth` and `mode`, which are not sources.
 *
 * One list, because callers that enumerate these by hand drift: pinning a
 * source must leave Auto, and a source added here but missed there would be
 * silently ignored by the count and the Auto reset.
 */
export const SCOPE_SOURCE_KEYS = [
  "codebase",
  "cms",
  "qbank",
  "metrics",
  "firecrawl",
  "media",
  "audience",
  "recruiting",
] as const satisfies readonly (keyof HLTResearchScope)[];

const SOURCE_LIMIT_BY_DEPTH = {
  fast: 5,
  balanced: 8,
  deep: 12,
} as const;

export function sourceLimitForDepth(depth?: HLTResearchScope["depth"]): number {
  return SOURCE_LIMIT_BY_DEPTH[depth || "balanced"];
}

export function selectedScopeCount(scope?: Partial<HLTResearchScope>): number {
  const normalized = normalizeHLTResearchScope(scope);
  return SCOPE_SOURCE_KEYS.filter((key) => normalized[key]).length;
}
