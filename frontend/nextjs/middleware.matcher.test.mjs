/**
 * The team gate is one regex. When that regex is wrong, every page still
 * redirects to /login and every obvious API call still 401s — so the hole is
 * invisible until someone goes looking. It was wrong: a bare `.*\.png$`
 * alternative in the negative lookahead matched the whole pathname rather than
 * the last segment, so `/api/files/<anything>.png` bypassed the middleware and
 * reached a DELETE proxy holding the server's API_AUTH_KEY.
 *
 * These tests assert the property that matters — every /api path is matched by
 * the middleware — rather than the spelling of the regex.
 *
 * Run: node --test frontend/nextjs/middleware.matcher.test.mjs
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const source = fs.readFileSync(path.join(import.meta.dirname, "middleware.ts"), "utf8");

/** Pull the `matcher: [...]` string literals out of the exported config. */
function readMatchers() {
  const block = source.match(/matcher:\s*\[([\s\S]*?)\n\s*\],/);
  assert.ok(block, "could not find `matcher: [...]` in middleware.ts");
  const entries = [...block[1].matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) =>
    m[1].replace(/\\\\/g, "\\"),
  );
  assert.ok(entries.length > 0, "matcher array parsed as empty — extractor is broken");
  return entries;
}

/**
 * Translate a Next matcher source string into the regex Next compiles it to.
 *
 * Deliberately handles only the two forms this file uses, and THROWS on
 * anything else. A translator that silently guesses at an unknown form is a
 * gate that scans wrong: it would report "all paths gated" for a matcher it
 * never really understood.
 */
function toRegExp(matcher) {
  if (/^\/\(\(\?!.*\)\.\*\)$/s.test(matcher)) {
    // Full-path custom group, e.g. "/((?!api/|img/).*)" — used verbatim.
    return new RegExp(`^${matcher}$`);
  }
  if (/^\/[a-z-]+\/:[a-z]+\*$/i.test(matcher)) {
    // Segment wildcard, e.g. "/api/:path*" -> /api plus any nested path.
    const prefix = matcher.slice(0, matcher.indexOf("/:"));
    return new RegExp(`^${prefix}(?:/.*)?$`);
  }
  throw new Error(
    `middleware matcher form not understood by this test: ${matcher}\n` +
      "Teach toRegExp() the new form rather than deleting the assertion.",
  );
}

const matchers = readMatchers().map(toRegExp);
const isGated = (pathname) => matchers.some((re) => re.test(pathname));

test("every /api route is gated, whatever it is named", () => {
  for (const pathname of [
    "/api/reports",
    "/api/files/report.png", // the live bypass: reached DELETE unauthenticated
    "/api/files/logo.svg",
    "/api/files/favicon.ico",
    "/api/brain/vision",
    "/api/upload",
    "/api/ws-token",
    "/api/reports/abc123/chat",
  ]) {
    assert.equal(isGated(pathname), true, `${pathname} must be gated`);
  }
});

test("pages are gated, including ones that look like assets", () => {
  for (const pathname of ["/", "/settings", "/research/abc", "/reports/summary.png"]) {
    assert.equal(isGated(pathname), true, `${pathname} must be gated`);
  }
});

test("genuine static assets stay ungated", () => {
  for (const pathname of [
    "/_next/static/chunks/main.js",
    "/_next/image",
    "/favicon.ico",
    "/manifest.json",
    "/sw.js",
    "/workbox-f1770938.js",
    "/embed.js",
    "/img/logo.png",
  ]) {
    assert.equal(isGated(pathname), false, `${pathname} should not be gated`);
  }
});

test("no matcher excludes by bare file extension", () => {
  // The exact shape of the original bug. A suffix alternative like `.*\.png$`
  // is unanchored to a path segment and swallows whole routes.
  for (const matcher of readMatchers()) {
    assert.equal(
      /\.\*\\?\.[a-z0-9]+\$/i.test(matcher),
      false,
      `matcher excludes by bare extension, which matches whole paths: ${matcher}`,
    );
  }
});
