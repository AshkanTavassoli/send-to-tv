// Watches each tab's network traffic for video streams and subtitle files,
// remembering the request headers (cookies, referer...) the site expects.

const HELPER = "http://127.0.0.1:8770";
const MAX_PER_TAB = 40;

// tabId -> { items: [{url, kind, headers, seen}] }
// Kept in chrome.storage.session too, because Chrome stops idle service workers.
const tabs = new Map();
// requestId -> headers (kept until we see the response)
const pending = new Map();

const loaded = chrome.storage.session.get("tabs").then(r => {
  for (const [k, v] of Object.entries(r.tabs || {})) {
    if (!tabs.has(+k)) tabs.set(+k, v);
  }
}).catch(() => {});

let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    chrome.storage.session.set({ tabs: Object.fromEntries(tabs) }).catch(() => {});
  }, 300);
}

const VIDEO_TYPES = /(mpegurl|dash\+xml|^video\/)/i;
const SUB_TYPES = /(text\/vtt|application\/x-subrip|text\/srt|ttml)/i;

function classify(url, contentType) {
  let path;
  try { path = new URL(url).pathname.toLowerCase(); } catch { return null; }
  if (/\.m3u8$/.test(path)) return "hls";
  if (/\.mpd$/.test(path)) return "dash";
  if (/\.(vtt|webvtt|srt|ass|ssa|ttml|dfxp)$/.test(path)) return "sub";
  // Skip pieces of a stream: the playlist is what we want, not its segments.
  if (/\.(ts|m4s|m4a|m4v|aac|cmfv|cmfa|key)$/.test(path)) return null;
  if (/\/(seg|segment|chunk|frag|fragment)[-_]?\d/i.test(path)) return null;
  if (/\.(mp4|webm|mkv|mov|flv)$/.test(path)) return "file";
  if (contentType) {
    if (/mpegurl/i.test(contentType)) return "hls";
    if (/dash\+xml/i.test(contentType)) return "dash";
    if (SUB_TYPES.test(contentType)) return "sub";
    if (VIDEO_TYPES.test(contentType) && !/mp2t|iso.segment/i.test(contentType)) return "file";
  }
  return null;
}

function remember(tabId, url, kind, headers) {
  if (tabId < 0 || !kind) return;
  let t = tabs.get(tabId);
  if (!t) { t = { items: [] }; tabs.set(tabId, t); }
  const key = url.split("#")[0];
  const found = t.items.find(i => i.url === key);
  if (found) { found.headers = headers || found.headers; return; }
  t.items.push({ url: key, kind, headers: headers || {}, seen: Date.now() });
  if (t.items.length > MAX_PER_TAB) t.items.shift();
  updateBadge(tabId);
  save();
}

function updateBadge(tabId) {
  const t = tabs.get(tabId);
  const n = t ? t.items.filter(i => i.kind !== "sub").length : 0;
  chrome.action.setBadgeText({ tabId, text: n ? String(n) : "" }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ tabId, color: "#2563eb" }).catch(() => {});
}

function headersToObject(list) {
  const out = {};
  for (const h of list || []) out[h.name.toLowerCase()] = h.value;
  return out;
}

const FILTER = { urls: ["<all_urls>"] };

chrome.webRequest.onBeforeSendHeaders.addListener(d => {
  if (d.tabId < 0 || d.url.startsWith(HELPER)) return;
  const headers = headersToObject(d.requestHeaders);
  const kind = classify(d.url);
  if (kind) remember(d.tabId, d.url, kind, headers);
  else pending.set(d.requestId, headers);
}, FILTER, ["requestHeaders", "extraHeaders"]);

chrome.webRequest.onHeadersReceived.addListener(d => {
  const headers = pending.get(d.requestId);
  pending.delete(d.requestId);
  if (d.tabId < 0 || !headers) return;
  const ct = (d.responseHeaders || []).find(h => h.name.toLowerCase() === "content-type");
  const kind = classify(d.url, ct && ct.value);
  if (kind) remember(d.tabId, d.url, kind, headers);
}, FILTER, ["responseHeaders"]);

chrome.webRequest.onErrorOccurred.addListener(d => pending.delete(d.requestId), FILTER);
chrome.webRequest.onCompleted.addListener(d => pending.delete(d.requestId), FILTER);

// New page in the tab -> forget the old page's streams.
chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "loading" && info.url) {
    tabs.set(tabId, { items: [] });
    updateBadge(tabId);
    save();
  }
});
chrome.tabs.onRemoved.addListener(tabId => { tabs.delete(tabId); save(); });

// ---------------------------------------------------------------- messaging

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  loaded.then(() => {
    if (msg.type === "list") {
      const t = tabs.get(msg.tabId);
      reply({ items: t ? t.items : [] });
    } else if (msg.type === "subtext" && sender.tab) {
      // Subtitle text the page itself downloaded (captured by hook.js).
      const tabId = sender.tab.id;
      remember(tabId, msg.url, "sub", null);
      const item = tabs.get(tabId).items.find(i => i.url === msg.url.split("#")[0]);
      if (item) { item.text = msg.text; save(); }
      reply({ ok: true });
    } else if (msg.type === "clear") {
      tabs.set(msg.tabId, { items: [] });
      updateBadge(msg.tabId);
      save();
      reply({ ok: true });
    }
  });
  return true; // async reply
});

// ---------------------------------------------------------------- shortcuts

async function helper(path, body) {
  const r = await fetch(HELPER + path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  } : {});
  return r.json();
}

chrome.commands.onCommand.addListener(async cmd => {
  try {
    if (cmd === "toggle-play") {
      const s = await helper("/status");
      await helper("/control", { action: s.state === "PLAYING" ? "pause" : "play" });
    } else if (cmd === "skip-forward") {
      await helper("/control", { action: "skip", value: 30 });
    } else if (cmd === "skip-back") {
      await helper("/control", { action: "skip", value: -10 });
    }
  } catch (e) { console.warn("TV helper not reachable", e); }
});
