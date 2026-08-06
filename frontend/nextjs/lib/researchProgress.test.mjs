/**
 * A research run that FAILS used to be indistinguishable from one that
 * succeeded. `deriveProgress` had no failure branch, so `done` stayed false,
 * `loading` went false, and the component's `finished = done || !loading`
 * turned true — which the rail read as "every phase complete" and painted all
 * four green. The backend was emitting the failure the whole time, as
 * `{content: "error", output: "Error: …"}`.
 *
 * Run: node --test frontend/nextjs/lib/researchProgress.test.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = fs.readFileSync(
  path.join(import.meta.dirname, "researchProgress.ts"),
  "utf8",
);
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
});
const moduleContext = { exports: {} };
vm.runInNewContext(outputText, { exports: moduleContext.exports, module: moduleContext });
const { deriveProgress, PHASES } = moduleContext.exports;

const ev = (content, extra = {}) => ({ type: "logs", content, ...extra });

test("a clean run reports no error and finishes", () => {
  const derived = deriveProgress([
    ev("starting_research"),
    ev("subqueries", { metadata: ["a", "b"] }),
    ev("scraping_content"),
    ev("writing_report"),
    { type: "report_complete" },
  ]);

  assert.equal(derived.error, null);
  assert.equal(derived.done, true);
});

test("an error event is captured and the run is NOT marked done", () => {
  const derived = deriveProgress([
    ev("starting_research"),
    ev("subqueries", { metadata: ["a"] }),
    ev("error", { output: "Error: Tavily returned 429" }),
  ]);

  assert.equal(derived.done, false, "a failed run must never report done");
  assert.equal(derived.error, "Tavily returned 429", "the Error: prefix is stripped for readers");
});

test("type:error events are captured too, not just type:logs", () => {
  const derived = deriveProgress([
    { type: "error", content: "error", output: "Error: Unknown command received by server" },
  ]);

  assert.equal(derived.error, "Unknown command received by server");
});

test("an error with no message still surfaces something a human can act on", () => {
  const derived = deriveProgress([ev("error")]);

  assert.equal(derived.error, "The research run failed.");
});

test("failing mid-run keeps the phase it died in, rather than advancing", () => {
  const derived = deriveProgress([
    ev("starting_research"),
    ev("scraping_urls"),
    ev("error", { output: "Error: boom" }),
  ]);

  assert.equal(PHASES[derived.reachedIndex].id, "search");
  assert.equal(derived.done, false);
});

test("progress still advances through the phases on a healthy stream", () => {
  const at = (events) => PHASES[deriveProgress(events).reachedIndex].id;

  assert.equal(at([ev("starting_research")]), "plan");
  assert.equal(at([ev("starting_research"), ev("scraping_urls")]), "search");
  assert.equal(at([ev("starting_research"), ev("scraping_content")]), "read");
  assert.equal(at([ev("starting_research"), ev("writing_report")]), "write");
});

test("sources and sub-questions are counted for the run summary", () => {
  const derived = deriveProgress([
    ev("subqueries", { metadata: ["a", "b", "c"] }),
    ev("added_source_url", { metadata: "https://example.com/1" }),
    ev("added_source_url", { metadata: "https://example.com/2" }),
    ev("added_source_url", { metadata: "https://example.com/1" }),
  ]);

  assert.equal(derived.subqueryCount, 3);
  assert.equal(derived.sourceCount, 2, "duplicate source URLs are de-duplicated");
});
