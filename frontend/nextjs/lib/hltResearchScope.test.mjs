import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = fs.readFileSync(
  path.join(import.meta.dirname, "hltResearchScope.ts"),
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
  require: () => ({}),
});
const { sourceLimitForDepth } = moduleContext.exports;

test("research depth widens the per-query source pool", () => {
  assert.equal(sourceLimitForDepth("fast"), 5);
  assert.equal(sourceLimitForDepth("balanced"), 8);
  assert.equal(sourceLimitForDepth("deep"), 12);
  assert.equal(sourceLimitForDepth(), 8);
});
