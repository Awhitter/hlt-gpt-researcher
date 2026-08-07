import { useEffect, useState } from "react";

import {
  defaultHLTResearchScope,
  normalizeHLTResearchScope,
} from "@/lib/hltResearchScope";
import { ChatBoxSettings } from "@/types/data";

const STORAGE_KEY = "chatBoxSettings";

export function defaultChatBoxSettings(): ChatBoxSettings {
  return {
    report_type: "research_report",
    report_source: "web",
    tone: "Objective",
    domains: [],
    defaultReportType: "research_report",
    layoutType: "copilot",
    mcp_enabled: false,
    mcp_configs: [],
    mcp_strategy: "fast",
    hlt_research_scope: { ...defaultHLTResearchScope },
  };
}

export function parseStoredChatBoxSettings(raw: string | null): ChatBoxSettings {
  const defaults = defaultChatBoxSettings();
  if (!raw) return defaults;

  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return defaults;

    return {
      ...defaults,
      ...parsed,
      domains: Array.isArray(parsed.domains) ? parsed.domains : defaults.domains,
      mcp_configs: Array.isArray(parsed.mcp_configs) ? parsed.mcp_configs : defaults.mcp_configs,
      hlt_research_scope: normalizeHLTResearchScope(parsed.hlt_research_scope),
    };
  } catch {
    // Corrupt browser preferences are disposable. Falling back is the useful
    // behavior; surfacing a console error makes a healthy page look broken.
    return defaults;
  }
}

/**
 * Keep the server render and the browser's first render identical, then apply
 * the teammate's saved preferences after hydration. Reading localStorage in a
 * useState initializer makes the client render different controls and copy
 * from the HTML Next.js sent, which React reports as hydration errors.
 */
export function useChatBoxSettings() {
  const [settings, setSettings] = useState<ChatBoxSettings>(defaultChatBoxSettings);
  const [hasLoadedStorage, setHasLoadedStorage] = useState(false);

  useEffect(() => {
    setSettings(parseStoredChatBoxSettings(localStorage.getItem(STORAGE_KEY)));
    setHasLoadedStorage(true);
  }, []);

  useEffect(() => {
    if (!hasLoadedStorage) return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  }, [hasLoadedStorage, settings]);

  return [settings, setSettings] as const;
}
