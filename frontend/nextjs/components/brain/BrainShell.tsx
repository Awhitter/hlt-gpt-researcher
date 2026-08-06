"use client";

import { ReactNode } from "react";
import CodebaseExplorer from "@/components/brain/CodebaseExplorer";
import VisionPanel from "@/components/brain/VisionPanel";
import ChangelogTimeline from "@/components/brain/ChangelogTimeline";
import RoadmapPanel from "@/components/brain/RoadmapPanel";
import AudiencePanel from "@/components/brain/AudiencePanel";
import LibraryPanel from "@/components/brain/LibraryPanel";
import Modal from "@/components/Settings/Modal";
import { BRAIN_TABS, BrainTabId } from "@/lib/brainTabs";
import { ChatBoxSettings } from "@/types/data";

type Props = {
  activeTab: BrainTabId;
  onTabChange: (id: BrainTabId) => void;
  onCodebaseAsk: (question: string) => void;
  askChildren: ReactNode;
  chatBoxSettings: ChatBoxSettings;
  setChatBoxSettings: React.Dispatch<React.SetStateAction<ChatBoxSettings>>;
};

// Ask is not in this list: it is the always-on surface behind the shell, not a
// tab. Everything else comes from BRAIN_TABS so there is one registry rather
// than two that drift — these had already forked ("Codebase" vs "Codebases").
const secondaryTabs = BRAIN_TABS.filter((tab) => tab.id !== "ask");

export default function BrainShell({
  activeTab,
  onTabChange,
  onCodebaseAsk,
  askChildren,
  chatBoxSettings,
  setChatBoxSettings,
}: Props) {
  return (
    <div className="w-full max-w-full overflow-x-hidden">
      {activeTab === "ask" && askChildren}

      <details
        className={`group mx-auto mb-10 w-full max-w-full px-4 text-center sm:max-w-5xl ${
          activeTab === "ask" ? "mt-6" : "mt-0"
        }`}
      >
        <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 rounded-md px-3 py-2 text-xs font-medium text-slate-500 transition hover:text-slate-300">
          More research tools
          <span aria-hidden="true" className="transition group-open:rotate-180">⌄</span>
        </summary>
        <div className="mt-3 rounded-2xl border border-white/10 bg-white/[0.025] p-3 text-left">
          <div className="flex flex-wrap items-center gap-2">
            {secondaryTabs.map(({ id, label, description }) => (
              <button
                key={id}
                type="button"
                title={description}
                aria-current={activeTab === id ? "page" : undefined}
                onClick={() => onTabChange(id)}
                className={`rounded-md border px-3 py-1.5 text-xs transition ${
                  activeTab === id
                    ? "border-[#155EEF] bg-[#155EEF]/15 text-white"
                    : "border-white/10 text-slate-400 hover:text-white"
                }`}
              >
                {label}
              </button>
            ))}
            {activeTab !== "ask" && (
              <button
                type="button"
                onClick={() => onTabChange("ask")}
                className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-slate-400 transition hover:text-white"
              >
                Back to Ask
              </button>
            )}
            <div className="ml-auto">
              <Modal
                chatBoxSettings={chatBoxSettings}
                setChatBoxSettings={setChatBoxSettings}
              />
            </div>
          </div>
        </div>
      </details>

      <div role="tabpanel" className="min-h-[50vh]">
        {activeTab === "audience" && <AudiencePanel />}
        {activeTab === "codebase" && <CodebaseExplorer onAsk={onCodebaseAsk} />}
        {activeTab === "library" && <LibraryPanel />}
        {activeTab === "vision" && <VisionPanel />}
        {activeTab === "changelog" && <ChangelogTimeline />}
        {activeTab === "roadmap" && <RoadmapPanel />}
      </div>
    </div>
  );
}
