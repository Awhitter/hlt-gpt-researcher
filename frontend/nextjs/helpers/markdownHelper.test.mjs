/**
 * Every string this pipeline renders is LLM-authored, much of it echoed from
 * web pages the researcher just scraped, and the output goes to five
 * `dangerouslySetInnerHTML` sinks.
 *
 * It ran with `sanitize: false`, which is not a permissive schema — remark-html
 * reads a boolean as `allowDangerousHtml = !clean`, so `false` skips
 * hast-util-sanitize altogether and raw HTML passes straight through.
 *
 * Run: node --test frontend/nextjs/helpers/markdownHelper.test.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import ts from "typescript";
import { pathToFileURL } from "node:url";

// Transpile-and-import the REAL module rather than reimplementing the pipeline:
// the bug was one option on the real `remark().use(html, ...)` chain, so a
// hand-rolled stand-in would have proved nothing.
//
// Written next to the source, not imported from a data: URL — a data: module
// cannot resolve bare specifiers, and this file imports remark, remark-html and
// remark-gfm. (The vm + CommonJS pattern used elsewhere in this repo suits
// modules whose only imports are types; this one has three real ones.)
const source = fs.readFileSync(
  path.join(import.meta.dirname, "markdownHelper.ts"),
  "utf8",
);
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const compiled = path.join(import.meta.dirname, ".markdownHelper.compiled.test.mjs");
fs.writeFileSync(compiled, outputText, "utf8");
process.on("exit", () => fs.rmSync(compiled, { force: true }));
const { markdownToHtml } = await import(pathToFileURL(compiled).href);

const HOSTILE = [
  "# Report",
  "",
  "<script>alert(1)</script>",
  "",
  "<img src=x onerror=alert(1)>",
  "",
  '<a href="javascript:alert(1)">click</a>',
  "",
  "| col | col |",
  "| --- | --- |",
  "| 1   | 2   |",
  "",
  "```js",
  "const a = 1;",
  "```",
  "",
].join("\n");

test("script tags and event handlers never reach the DOM sink", async () => {
  const out = await markdownToHtml(HOSTILE);

  assert.ok(!/<script/i.test(out), "a <script> tag survived into rendered HTML");
  assert.ok(!/onerror/i.test(out), "an inline event handler survived");
  assert.ok(!/javascript:/i.test(out), "a javascript: URL survived");
});

test("sanitising does not cost us the formatting reports rely on", async () => {
  const out = await markdownToHtml(HOSTILE);

  assert.ok(/<table/i.test(out), "GFM tables must still render");
  assert.ok(/language-js/.test(out), "code language class must survive for highlighting");
  assert.ok(/<h1/i.test(out), "headings must still render");
});

test("links still open safely in a new tab", async () => {
  const out = await markdownToHtml("[x](https://example.com)");

  assert.match(out, /target="_blank"/);
  assert.match(out, /rel="noopener noreferrer"/);
});
