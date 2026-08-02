export type ReportSourceRef = {
  repo: string;
  commitSha: string;
  path: string;
  line: number | null;
  url: string;
};

export type MobileReportMessage = {
  type: string;
  content: string;
  metadata?: unknown;
};

type MobileReportInput = {
  type?: unknown;
  content?: unknown;
  metadata?: unknown;
};

const immutableGithubSource =
  /https:\/\/github\.com\/([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)\/blob\/([0-9a-fA-F]{40})\/([^\s)#?]+)(?:#L(\d+)(?:-L\d+)?)?/g;

export function extractReportSources(answer: string): ReportSourceRef[] {
  const sources: ReportSourceRef[] = [];
  const seen = new Set<string>();
  immutableGithubSource.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = immutableGithubSource.exec(answer)) !== null) {
    const key = `${match[1]}:${match[2].toLowerCase()}:${match[3]}:${match[4] || ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    sources.push({
      repo: match[1],
      commitSha: match[2].toLowerCase(),
      path: match[3],
      line: match[4] ? Number(match[4]) : null,
      url: match[0],
    });
  }
  return sources;
}

export function isChangeOrientedQuestion(question: string): boolean {
  return /\b(chang(?:e|ed|ing)|edit(?:ed|ing)?|update(?:d|ing)?|modify|modified|replace|remove|add|implement|fix|how (?:can|do|would) (?:i|we))\b/i.test(
    question,
  );
}

export function resolveReportAnswer(structuredAnswer: string | undefined, legacyAnswer: string): string {
  return structuredAnswer || legacyAnswer;
}

export function buildMobileReportMessages(
  orderedData: readonly MobileReportInput[],
  legacyAnswer: string,
  fallbackQuestion = "",
): MobileReportMessage[] {
  const messages: MobileReportMessage[] = [];
  let hasQuestion = false;
  let hasReport = false;

  for (const item of orderedData) {
    if (item.type === "question" && typeof item.content === "string") {
      hasQuestion = true;
      messages.push({ type: "question", content: item.content, metadata: item.metadata });
    } else if (item.type === "chat" && typeof item.content === "string") {
      messages.push({ type: "chat", content: item.content, metadata: item.metadata });
    } else if (
      item.type === "reportBlock" &&
      typeof item.content === "string" &&
      item.content.trim()
    ) {
      hasReport = true;
      messages.push({ type: "chat", content: item.content, metadata: item.metadata });
    }
  }

  if (!hasQuestion && fallbackQuestion.trim()) {
    messages.unshift({ type: "question", content: fallbackQuestion.trim() });
  }

  if (
    !hasReport &&
    legacyAnswer.trim() &&
    !messages.some((item) => item.type === "chat" && item.content === legacyAnswer)
  ) {
    const questionIndex = messages.findIndex((item) => item.type === "question");
    messages.splice(questionIndex >= 0 ? questionIndex + 1 : 0, 0, {
      type: "chat",
      content: legacyAnswer,
    });
  }

  return messages;
}
