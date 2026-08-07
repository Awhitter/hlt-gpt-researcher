import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = fs.readFileSync(
  path.join(process.cwd(), "lib/reportTrust.ts"),
  "utf8",
);
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2020,
  },
});
const moduleContext = { exports: {} };
vm.runInNewContext(outputText, {
  exports: moduleContext.exports,
  module: moduleContext,
});

const {
  buildMobileReportMessages,
  extractReportSources,
  filterVisibleReportSources,
  getReportVerification,
  isChangeOrientedQuestion,
  resolveReportAnswer,
} = moduleContext.exports;
const sha = "a".repeat(40);

test("extracts only immutable GitHub sources", () => {
  const refs = extractReportSources(
    `See https://github.com/Awhitter/nursing-mastery/blob/${sha}/app/page.tsx#L10`,
  );
  assert.equal(refs.length, 1);
  assert.equal(refs[0].commitSha, sha);
  assert.equal(
    extractReportSources("https://github.com/Awhitter/repo/blob/main/app.ts")
      .length,
    0,
  );
});

test("internal reports hide public-web cards and keep exact repository sources", () => {
  const exact = `https://github.com/Awhitter/nursing-mastery/blob/${sha}/app/page.tsx#L10`;
  const verification = getReportVerification({
    verificationStatus: "verified",
    verificationReason: "Every source was validated.",
    deliveryBlocked: false,
    hlt_research_scope: { active_sources: ["codebase", "recruiting"] },
  });

  assert.equal(verification.isCodeScoped, true);
  assert.equal(verification.isInternalOnly, true);
  assert.equal(
    JSON.stringify(
      filterVisibleReportSources(
        [
          { name: "random.example", url: "https://random.example/article" },
          { name: "github.com", url: exact },
          {
            name: "github.com",
            url: "https://github.com/Awhitter/repo/blob/main/file.ts",
          },
        ],
        verification,
      ),
    ),
    JSON.stringify([{ name: "github.com", url: exact }]),
  );
});

test("public research keeps its visible web sources", () => {
  const sources = [
    { name: "example.com", url: "https://example.com/research" },
  ];
  const verification = getReportVerification({
    verificationStatus: "unverified",
    hlt_research_scope: { active_sources: ["firecrawl"] },
  });

  assert.equal(verification.isCodeScoped, false);
  assert.equal(
    JSON.stringify(filterVisibleReportSources(sources, verification)),
    JSON.stringify(sources),
  );
});

test("mixed code and audience research keeps both valid source families", () => {
  const sources = [
    { name: "example.com", url: "https://example.com/nurse-research" },
    {
      name: "github.com",
      url: `https://github.com/Awhitter/nursing-mastery/blob/${sha}/app/page.tsx#L10`,
    },
  ];
  const verification = getReportVerification({
    verificationStatus: "verified",
    hlt_research_scope: { active_sources: ["codebase", "audience"] },
  });

  assert.equal(verification.isCodeScoped, true);
  assert.equal(verification.isInternalOnly, false);
  assert.equal(
    JSON.stringify(filterVisibleReportSources(sources, verification)),
    JSON.stringify(sources),
  );
});

test("recognizes change-oriented questions", () => {
  assert.equal(
    isChangeOrientedQuestion("How can I change the onboarding questions?"),
    true,
  );
  assert.equal(
    isChangeOrientedQuestion(
      "What do we ask, and how can those questions be changed?",
    ),
    true,
  );
  assert.equal(
    isChangeOrientedQuestion("What attributes do we capture?"),
    false,
  );
});

test("legacy answer renders when structured report data is absent", () => {
  assert.equal(
    resolveReportAnswer(undefined, "Recovered legacy answer"),
    "Recovered legacy answer",
  );
  assert.equal(
    resolveReportAnswer("Structured answer", "Legacy"),
    "Structured answer",
  );
});

test("mobile reopening renders a legacy answer and its stored question", () => {
  assert.equal(
    JSON.stringify(
      buildMobileReportMessages(
        [],
        "Recovered legacy answer",
        "Stored question",
      ),
    ),
    JSON.stringify([
      { type: "question", content: "Stored question" },
      { type: "chat", content: "Recovered legacy answer" },
    ]),
  );
});

test("mobile reopening renders a structured report once", () => {
  assert.equal(
    JSON.stringify(
      buildMobileReportMessages(
        [
          { type: "question", content: "Stored question" },
          { type: "reportBlock", content: "Structured report" },
        ],
        "Structured report",
        "Stored question",
      ),
    ),
    JSON.stringify([
      { type: "question", content: "Stored question" },
      { type: "chat", content: "Structured report" },
    ]),
  );
});
