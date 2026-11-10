// Tests for the extension's stream detection (background.js).
// Run from the repository root:  node --test tests/classify.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

// background.js is a plain service-worker script, so load it into a sandbox
// with just enough of the extension API for it to start.
const event = { addListener() {} };
const sandbox = {
  URL, console, setTimeout, clearTimeout, fetch: async () => ({ json: async () => ({}) }),
  chrome: {
    storage: { session: { get: async () => ({}), set: async () => {} } },
    webRequest: { onBeforeSendHeaders: event, onHeadersReceived: event, onErrorOccurred: event, onCompleted: event },
    tabs: { onUpdated: event, onRemoved: event },
    runtime: { onMessage: event },
    commands: { onCommand: event },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
  },
};
vm.createContext(sandbox);
vm.runInContext(readFileSync(new URL("../extension/background.js", import.meta.url), "utf8"), sandbox);
const { classify, headersToObject } = sandbox;

test("playlists and manifests are streams", () => {
  assert.equal(classify("https://example.org/film/master.m3u8?token=1"), "hls");
  assert.equal(classify("https://example.org/film/manifest.mpd"), "dash");
  assert.equal(classify("https://example.org/api/playlist", "application/vnd.apple.mpegurl"), "hls");
  assert.equal(classify("https://example.org/api/manifest", "application/dash+xml"), "dash");
});

test("plain video files are found by name or type", () => {
  assert.equal(classify("https://example.org/film.mp4"), "file");
  assert.equal(classify("https://example.org/download?id=7", "video/mp4"), "file");
});

test("pieces of a stream are ignored", () => {
  assert.equal(classify("https://example.org/film/seg-00012.ts"), null);
  assert.equal(classify("https://example.org/film/chunk_4.m4s"), null);
  assert.equal(classify("https://example.org/film/segment12", "video/mp4"), null);
  assert.equal(classify("https://example.org/film/part", "video/mp2t"), null);
});

test("subtitle files are found by name or type", () => {
  assert.equal(classify("https://example.org/subs/english.vtt"), "sub");
  assert.equal(classify("https://example.org/subs/english.srt?x=1"), "sub");
  assert.equal(classify("https://example.org/subs/7", "text/vtt; charset=utf-8"), "sub");
});

test("everything else is ignored", () => {
  assert.equal(classify("https://example.org/index.html", "text/html"), null);
  assert.equal(classify("https://example.org/app.js"), null);
  assert.equal(classify("not a url"), null);
});

test("request headers become a lower-case map", () => {
  const got = headersToObject([{ name: "Referer", value: "https://example.org/" }, { name: "Cookie", value: "a=1" }]);
  assert.deepEqual({ ...got }, { referer: "https://example.org/", cookie: "a=1" });
});
