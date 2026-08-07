import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = fs.readFileSync(
  path.join(process.cwd(), "hooks/useChatBoxSettings.ts"),
  "utf8",
);

function load({ react } = {}) {
  const effects = [];
  const reactMock = react || {
    useState(initial) {
      return [typeof initial === "function" ? initial() : initial, () => {}];
    },
    useEffect(effect) {
      effects.push(effect);
    },
  };
  const scope = {
    auto: true,
    codebase: false,
    cms: false,
    qbank: false,
    metrics: false,
    firecrawl: false,
    media: false,
    audience: false,
    recruiting: false,
    depth: "balanced",
    mode: "standard",
  };
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  const moduleContext = { exports: {} };
  vm.runInNewContext(outputText, {
    exports: moduleContext.exports,
    module: moduleContext,
    console,
    require(name) {
      if (name === "react") return reactMock;
      if (name === "@/lib/hltResearchScope") {
        return {
          defaultHLTResearchScope: scope,
          normalizeHLTResearchScope: (value) => ({ ...scope, ...(value || {}) }),
        };
      }
      return {};
    },
  });
  return { ...moduleContext.exports, effects };
}

test("the first render is deterministic and does not read browser storage", () => {
  const { useChatBoxSettings, effects } = load();

  const [settings] = useChatBoxSettings();

  assert.equal(settings.layoutType, "copilot");
  assert.equal(settings.hlt_research_scope.auto, true);
  assert.equal(effects.length, 2, "storage work is deferred to effects");
});

test("stored preferences merge onto complete current defaults", () => {
  const { parseStoredChatBoxSettings } = load();
  const settings = parseStoredChatBoxSettings(JSON.stringify({
    layoutType: "report",
    hlt_research_scope: { codebase: true, depth: "deep" },
  }));

  assert.equal(settings.layoutType, "report");
  assert.equal(settings.report_type, "research_report");
  assert.equal(settings.hlt_research_scope.auto, true);
  assert.equal(settings.hlt_research_scope.codebase, true);
  assert.equal(settings.hlt_research_scope.depth, "deep");
});

test("malformed storage falls back without producing a partial settings object", () => {
  const { parseStoredChatBoxSettings } = load();
  const settings = parseStoredChatBoxSettings("not-json");

  assert.equal(settings.report_source, "web");
  assert.deepEqual(Array.from(settings.domains), []);
  assert.deepEqual(Array.from(settings.mcp_configs), []);
});
