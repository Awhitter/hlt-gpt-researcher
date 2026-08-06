import { Data } from "../types/data";

/**
 * Derives Plan / Search / Read / Write progress from the raw websocket event
 * stream, so non-technical teammates see a readable storyline instead of a log
 * firehose.
 *
 * Pure and separate from the component so the state machine can be tested —
 * it previously had no failure branch at all, and painted a dead run green.
 */

export type PhaseId = "plan" | "search" | "read" | "write";

export const PHASES: { id: PhaseId; label: string; hint: string }[] = [
  { id: "plan", label: "Planning", hint: "Choosing the agent and outlining sub-questions" },
  { id: "search", label: "Searching", hint: "Querying the web, code graph, and connected tools" },
  { id: "read", label: "Reading", hint: "Scraping and digesting the sources it found" },
  { id: "write", label: "Writing", hint: "Composing the cited report" },
];

export const PHASE_EVENTS: Record<PhaseId, string[]> = {
  plan: [
    "agent_generated",
    "starting_research",
    "planning_research",
    "research_plan",
    "generating_subtopics",
    "subtopics_generated",
  ],
  search: [
    "subqueries",
    "running_subquery_research",
    "running_subquery_with_vectorstore_research",
    "added_source_url",
    "scraping_urls",
    "mcp_retrieval",
    "mcp_results",
    "mcp_comprehensive",
    "mcp_comprehensive_run",
  ],
  read: [
    "scraping_content",
    "scraping_complete",
    "scraping_images",
    "fetching_query_content",
    "subquery_context_window",
    "context_combined",
    "research_step_finalized",
    "mcp_research_complete",
  ],
  write: [
    "writing_report",
    "writing_introduction",
    "writing_conclusion",
    "generating_draft_sections",
    "draft_sections_generated",
    "introduction_written",
    "conclusion_written",
    "report_written",
  ],
};

export const EVENT_LABELS: Record<string, string> = {
  agent_generated: "Picked a specialist agent",
  starting_research: "Kicking off the research run",
  planning_research: "Planning the research outline",
  research_plan: "Research plan ready",
  subqueries: "Sub-questions chosen",
  running_subquery_research: "Searching a sub-question",
  added_source_url: "Found a source",
  scraping_urls: "Collecting pages to read",
  scraping_content: "Reading sources",
  scraping_complete: "Finished reading sources",
  fetching_query_content: "Pulling page content",
  context_combined: "Combining everything it learned",
  research_step_finalized: "Research step complete",
  mcp_retrieval: "Querying connected tools",
  mcp_results: "Tool results in",
  writing_report: "Writing the report",
  writing_introduction: "Writing the introduction",
  writing_conclusion: "Writing the conclusion",
  report_written: "Report finished",
};

export interface ScopeInfo {
  auto: boolean;
  active: string[];
  reasons: Record<string, string[]>;
}

export const eventPhase = (content: string): PhaseId | null => {
  for (const phase of PHASES) {
    if (PHASE_EVENTS[phase.id].includes(content)) return phase.id;
  }
  return null;
};

export const prettify = (content: string) =>
  EVENT_LABELS[content] ||
  content.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export interface Derived {
  currentPhase: PhaseId;
  reachedIndex: number;
  done: boolean;
  error: string | null;
  sourceCount: number;
  subqueryCount: number;
  latestLabel: string;
  scope: ScopeInfo | null;
}

export const deriveProgress = (orderedData: Data[]): Derived => {
  let reachedIndex = 0;
  let done = false;
  let error: string | null = null;
  let subqueryCount = 0;
  let latestLabel = "Warming up";
  let scope: ScopeInfo | null = null;
  const sources = new Set<string>();

  for (const item of orderedData as any[]) {
    if (item.type === "path" || item.type === "report_complete") {
      done = true;
      continue;
    }
    if (item.type === "report") {
      reachedIndex = Math.max(reachedIndex, 3);
      latestLabel = "Writing the report";
      continue;
    }
    const content = typeof item.content === "string" ? item.content : "";
    if (!content) continue;
    // The backend emits {type: "logs"|"error", content: "error", output: "Error: …"}
    // on every failure path. Without this the rail treats a dead run as a
    // finished one and paints all four phases green.
    if (content === "error") {
      const output = typeof item.output === "string" ? item.output : "";
      error = output.replace(/^Error:\s*/, "") || "The research run failed.";
      continue;
    }
    if (content === "added_source_url" && typeof item.metadata === "string") {
      sources.add(item.metadata);
    }
    if (content === "subqueries" && Array.isArray(item.metadata)) {
      subqueryCount = item.metadata.length;
    }
    if (content === "hlt_scope_status") {
      const scopeMeta = item.metadata?.hlt_research_scope;
      if (scopeMeta && typeof scopeMeta === "object") {
        const autoReasons =
          scopeMeta.auto_scope?.reasons &&
          typeof scopeMeta.auto_scope.reasons === "object"
            ? scopeMeta.auto_scope.reasons
            : {};
        scope = {
          auto: Boolean(scopeMeta.auto_scope?.requested),
          active: Array.isArray(scopeMeta.active_sources)
            ? scopeMeta.active_sources
            : [],
          reasons: autoReasons as Record<string, string[]>,
        };
      }
    }
    const phase = eventPhase(content);
    if (phase) {
      const idx = PHASES.findIndex((p) => p.id === phase);
      reachedIndex = Math.max(reachedIndex, idx);
      latestLabel = prettify(content);
    }
  }

  return {
    currentPhase: PHASES[reachedIndex].id,
    reachedIndex,
    done,
    error,
    sourceCount: sources.size,
    subqueryCount,
    latestLabel,
    scope,
  };
};

