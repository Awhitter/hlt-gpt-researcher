import React, { FC, useEffect, useState } from "react";
import InputArea from "./ResearchBlocks/elements/InputArea";
import { motion } from "framer-motion";
import { hltBranding } from "@/lib/hltBranding";
import ResearchScopeSelector from "@/components/ResearchScopeSelector";
import { ChatBoxSettings, HLTResearchScope } from "@/types/data";
import { normalizeHLTResearchScope } from "@/lib/hltResearchScope";

type THeroProps = {
  promptValue: string;
  setPromptValue: React.Dispatch<React.SetStateAction<string>>;
  handleDisplayResult: (query: string) => void;
  chatBoxSettings?: ChatBoxSettings;
  setChatBoxSettings?: React.Dispatch<React.SetStateAction<ChatBoxSettings>>;
};

const Hero: FC<THeroProps> = ({
  promptValue,
  setPromptValue,
  handleDisplayResult,
  chatBoxSettings,
  setChatBoxSettings,
}) => {
  const [isVisible, setIsVisible] = useState(false);

  useEffect(() => {
    setIsVisible(true);
  }, []);

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

  const fadeInUp = {
    hidden: { opacity: 0, y: 10 },
    visible: { opacity: 1, y: 0 },
  };

  return (
    // No negative top margin here: this renders inside the Brain tab shell,
    // and pulling the container up made it overlap (and swallow clicks on)
    // the tab bar above it.
    <div className="relative flex items-start overflow-visible pb-2 pt-[18px]">
      <motion.div
        initial="hidden"
        animate={isVisible ? "visible" : "hidden"}
        variants={fadeInUp}
        transition={{ duration: 0.35 }}
        className="flex w-full flex-col items-center justify-start py-2"
      >
        <motion.h1
          variants={fadeInUp}
          transition={{ duration: 0.35, delay: 0.03 }}
          className="max-w-[620px] px-4 pt-2 text-center text-lg font-semibold leading-tight text-white sm:text-xl md:text-2xl"
        >
          {hltBranding.enabled
            ? "What should Mastery research?"
            : "What would you like to research next?"}
        </motion.h1>

        <motion.div
          variants={fadeInUp}
          transition={{ duration: 0.35, delay: 0.08 }}
          className="w-full max-w-[820px] px-4 pb-2 pt-3"
        >
          <div className="group relative">
            <div className="absolute -inset-px rounded-xl bg-[#155EEF]/40 opacity-35 blur-sm transition duration-500 group-hover:opacity-55" />
            <div className="relative rounded-xl bg-[#0A0A0B]/80 shadow-[0_14px_36px_rgba(0,0,0,0.28)] ring-1 ring-white/10 backdrop-blur-sm">
              <InputArea
                promptValue={promptValue}
                setPromptValue={setPromptValue}
                handleSubmit={handleDisplayResult}
              />
            </div>
          </div>

          <motion.div
            variants={fadeInUp}
            transition={{ duration: 0.35, delay: 0.12 }}
            className="mt-2 px-4 text-center"
          >
            {!hltBranding.enabled && (
              <p className="text-[11px] font-light leading-5 text-gray-500">
                GPT Researcher may make mistakes. Verify important information
                and check sources.
              </p>
            )}
          </motion.div>
        </motion.div>

        {hltBranding.enabled && (
          <motion.div
            variants={fadeInUp}
            transition={{ duration: 0.35, delay: 0.16 }}
            className="mt-3 w-full"
          >
            <ResearchScopeSelector
              value={chatBoxSettings?.hlt_research_scope}
              onChange={handleScopeChange}
            />
          </motion.div>
        )}

        <div className="h-1" />
      </motion.div>
    </div>
  );
};

export default Hero;
