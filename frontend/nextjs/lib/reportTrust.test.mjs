import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = fs.readFileSync(path.join(process.cwd(), "lib/reportTrust.ts"), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
});
const moduleContext = { exports: {} };
vm.runInNewContext(outputText, { exports: moduleContext.exports, module: moduleContext });

const {
  buildMobileReportMessages,
  extractReportSources,
  isChangeOrientedQuestion,
  resolveReportAnswer,
} = moduleContext.exports;
const sha = "a".repeat(40);

test("extracts only immutable GitHub sources", () => {
  const refs = extractReportSources(`See https://github.com/Awhitter/nursing-mastery/blob/${sha}/app/page.tsx#L10`);
  assert.equal(refs.length, 1);
  assert.equal(refs[0].commitSha, sha);
  assert.equal(extractReportSources("https://github.com/Awhitter/repo/blob/main/app.ts").length, 0);
});

test("recognizes change-oriented questions", () => {
  assert.equal(isChangeOrientedQuestion("How can I change the onboarding questions?"), true);
  assert.equal(isChangeOrientedQuestion("What do we ask, and how can those questions be changed?"), true);
  assert.equal(isChangeOrientedQuestion("What attributes do we capture?"), false);
});

test("legacy answer renders when structured report data is absent", () => {
  assert.equal(resolveReportAnswer(undefined, "Recovered legacy answer"), "Recovered legacy answer");
  assert.equal(resolveReportAnswer("Structured answer", "Legacy"), "Structured answer");
});

test("mobile reopening renders a legacy answer and its stored question", () => {
  assert.equal(
    JSON.stringify(buildMobileReportMessages([], "Recovered legacy answer", "Stored question")),
    JSON.stringify([
      { type: "question", content: "Stored question" },
      { type: "chat", content: "Recovered legacy answer" },
    ]),
  );
});

test("mobile reopening renders a structured report once", () => {
  assert.equal(
    JSON.stringify(buildMobileReportMessages(
      [
        { type: "question", content: "Stored question" },
        { type: "reportBlock", content: "Structured report" },
      ],
      "Structured report",
      "Stored question",
    )),
    JSON.stringify([
      { type: "question", content: "Stored question" },
      { type: "chat", content: "Structured report" },
    ]),
  );
});
