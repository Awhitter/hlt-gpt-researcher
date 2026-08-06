/**
 * The suggestion strip shows three of a large bank and cycles on demand.
 *
 * Two properties matter. The window must WRAP, so the refresh control never
 * dead-ends on a short bank — an obvious-looking `slice(offset, offset+3)`
 * silently returns fewer chips, then none. And the first paint must be
 * deterministic, because this renders during SSR and hydration has to agree.
 *
 * Run: node --test frontend/nextjs/lib/suggestions.test.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = fs.readFileSync(path.join(import.meta.dirname, "suggestions.ts"), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
});
const moduleContext = { exports: {} };
vm.runInNewContext(outputText, {
  exports: moduleContext.exports,
  module: moduleContext,
  require: () => ({}), // the only import is a type, erased by transpile
});
const { windowAt, cycleLength, WINDOW_SIZE } = moduleContext.exports;

const bank = (n) => Array.from({ length: n }, (_, i) => ({ id: `s${i}`, label: `s${i}` }));

// Array.from, not .map: values come back from the vm sandbox, which is a
// separate realm with its own Array prototype. deepStrictEqual compares
// prototypes, so a foreign array fails against a local literal with the
// confusing message "same structure but not reference-equal".
const ids = (items) => Array.from(items, (i) => i.id);

test("shows the first three by default", () => {
  assert.deepEqual(ids(windowAt(bank(10), 0)), ["s0", "s1", "s2"]);
});

test("cycling advances by a full window", () => {
  const b = bank(10);
  assert.deepEqual(ids(windowAt(b, WINDOW_SIZE)), ["s3", "s4", "s5"]);
  assert.deepEqual(ids(windowAt(b, WINDOW_SIZE * 2)), ["s6", "s7", "s8"]);
});

test("the window wraps instead of running out", () => {
  const b = bank(10);
  // 9,0,1 — not a one-item tail, and never an empty strip.
  assert.deepEqual(ids(windowAt(b, 9)), ["s9", "s0", "s1"]);
  assert.equal(windowAt(b, 30).length, 3, "a full lap still returns three");
  assert.equal(windowAt(b, 1000).length, 3);
});

test("a bank smaller than the window shows what there is, without repeats", () => {
  assert.deepEqual(ids(windowAt(bank(2), 0)), ["s0", "s1"]);
  assert.deepEqual(ids(windowAt(bank(1), 0)), ["s0"]);
});

test("an empty bank yields nothing rather than throwing", () => {
  assert.equal(windowAt([], 0).length, 0);
  assert.equal(cycleLength([]), 0);
});

test("negative offsets are handled", () => {
  assert.equal(windowAt(bank(5), -1).length, 3);
});

test("cycleLength says whether refresh is worth showing", () => {
  assert.equal(cycleLength(bank(3)), 1, "one window means no point offering refresh");
  assert.equal(cycleLength(bank(10)), 4);
});

test("the same offset always yields the same window", () => {
  const b = bank(20);
  assert.deepEqual(ids(windowAt(b, 6)), ids(windowAt(b, 6)));
});
