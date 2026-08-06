import React, { FC } from "react";
import InputArea from "./ResearchBlocks/elements/InputArea";
import ResearchScopeSelector from "@/components/ResearchScopeSelector";
import SuggestionStrip from "@/components/brain/SuggestionStrip";
import { ChatBoxSettings, HLTResearchScope } from "@/types/data";
import { normalizeHLTResearchScope } from "@/lib/hltResearchScope";
import { Suggestion } from "@/lib/suggestions";

type THeroProps = {
  promptValue: string;
  setPromptValue: React.Dispatch<React.SetStateAction<string>>;
  handleDisplayResult: (query: string) => void;
  chatBoxSettings?: ChatBoxSettings;
  setChatBoxSettings?: React.Dispatch<React.SetStateAction<ChatBoxSettings>>;
  onSuggestion?: (suggestion: Suggestion) => void;
};

const Hero: FC<THeroProps> = ({
  promptValue,
  setPromptValue,
  handleDisplayResult,
  chatBoxSettings,
  setChatBoxSettings,
  onSuggestion,
}) => {
  const handleScopeChange = (scope: HLTResearchScope) => {
    const normalized = normalizeHLTResearchScope(scope);
    setChatBoxSettings?.((prev) => ({
      ...prev,
      hlt_research_scope: normalized,
      // Deep depth runs the deep-research pipeline; leaving deep must revert
      // it, otherwise every later run silently stays in deep mode.
      report_type:
        normalized.depth === "deep"
          ? "deep"
          : prev.report_type === "deep"
            ? "research_report"
            : prev.report_type,
      mcp_enabled:
        prev.mcp_enabled ||
        normalized.codebase ||
        normalized.cms ||
        normalized.qbank ||
        normalized.metrics,
      mcp_strategy: normalized.depth === "fast" ? "fast" : "deep",
    }));
  };

  return (
    // No fixed min-height and no translate: both reserved space the Hero was
    // not using, which the next block then clawed back with a negative margin.
    // That collided the moment the scope disclosure opened.
    <div className="flex w-full max-w-full items-center justify-center overflow-x-hidden px-4 pb-6 pt-[12vh] sm:pt-[14vh]">
      <div className="min-w-0 w-full max-w-full sm:max-w-[900px]">
        <h1 className="mb-8 text-center text-3xl font-medium tracking-[-0.03em] text-white sm:text-4xl">
          Ask about HLT
        </h1>
        <InputArea
          promptValue={promptValue}
          setPromptValue={setPromptValue}
          handleSubmit={handleDisplayResult}
        />
        {onSuggestion && <SuggestionStrip onSelect={onSuggestion} />}
        <ResearchScopeSelector
          value={chatBoxSettings?.hlt_research_scope}
          onChange={handleScopeChange}
          compact
        />
      </div>
    </div>
  );
};

export default Hero;
