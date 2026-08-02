import assert from "node:assert/strict";
import fs from "node:fs";
import { webcrypto } from "node:crypto";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = fs.readFileSync(path.join(process.cwd(), "lib/teamAccess.ts"), "utf8");

function load(env) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  const moduleContext = { exports: {} };
  vm.runInNewContext(outputText, {
    exports: moduleContext.exports,
    module: moduleContext,
    process: { env },
    crypto: webcrypto,
    TextEncoder,
    Date,
    Math,
    Uint8Array,
  });
  return moduleContext.exports;
}

test("production fails closed when either auth variable is missing", () => {
  assert.equal(load({ NODE_ENV: "production" }).teamAccessState().mode, "misconfigured");
  assert.equal(load({ NODE_ENV: "production", TEAM_ACCESS_PASSWORD: "mastery" }).teamAccessState().mode, "misconfigured");
  assert.equal(load({ NODE_ENV: "production", TEAM_ACCESS_COOKIE_SECRET: "secret" }).teamAccessState().mode, "misconfigured");
});

test("local development retains explicit bypass", () => {
  assert.equal(load({ NODE_ENV: "development" }).teamAccessState().mode, "local-bypass");
});

test("rotating cookie secret invalidates existing sessions", async () => {
  const first = load({ NODE_ENV: "production", TEAM_ACCESS_PASSWORD: "mastery", TEAM_ACCESS_COOKIE_SECRET: "one" });
  const cookie = await first.createTeamAccessCookieValue();
  assert.equal(await first.verifyTeamAccessCookieValue(cookie), true);
  const rotated = load({ NODE_ENV: "production", TEAM_ACCESS_PASSWORD: "mastery", TEAM_ACCESS_COOKIE_SECRET: "two" });
  assert.equal(await rotated.verifyTeamAccessCookieValue(cookie), false);
});
