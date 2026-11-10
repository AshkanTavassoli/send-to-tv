const HELPER = "http://127.0.0.1:8770";
const $ = id => document.getElementById(id);
let tab = null;
let found = [];
let lastStatus = null;
let seeking = false;

async function helper(path, body) {
  const r = await fetch(HELPER + path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  } : {});
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "helper error");
  return j;
}

function say(text, bad) {
  const m = $("msg");
  m.hidden = !text;
  m.textContent = text || "";
  m.className = "msg" + (bad ? " bad" : "");
}

function fmt(s) {
  if (s == null || isNaN(s)) return "--:--";
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
}

function shortUrl(u) {
  try { const x = new URL(u); return x.host + x.pathname; } catch { return u; }
}

// ---------------------------------------------------------------- list

function rank(i) {
  // Prefer master playlists, then other playlists, then plain files.
  const u = i.url.toLowerCase();
  let r = { hls: 0, dash: 1, file: 2 }[i.kind] ?? 3;
  if (i.kind === "hls" && /master|playlist|index|manifest/.test(u)) r -= 0.5;
  return r;
}

function renderList() {
  const vids = found.filter(i => i.kind !== "sub").sort((a, b) => rank(a) - rank(b) || a.seen - b.seen);
  const list = $("list");
  list.textContent = "";
  $("empty").hidden = vids.length > 0;
  vids.forEach((v, n) => {
    const row = document.createElement("div");
    row.className = "item";
    const meta = document.createElement("div");
    meta.className = "meta";
    const kind = document.createElement("div");
    kind.className = "kind";
    kind.textContent = ({ hls: "Stream (HLS)", dash: "Stream (DASH)", file: "Video file" })[v.kind]
      + (n === 0 ? " · best guess" : "");
    const url = document.createElement("div");
    url.className = "url";
    url.textContent = shortUrl(v.url);
    url.title = v.url;
    meta.append(kind, url);
    const btn = document.createElement("button");
    btn.className = n === 0 ? "primary" : "";
    btn.textContent = "Send to TV";
    btn.onclick = () => send(v);
    row.append(meta, btn);
    list.append(row);
  });

  const subs = found.filter(i => i.kind === "sub");
  const sel = $("subs");
  const keep = sel.value;
  [...sel.options].slice(2).forEach(o => o.remove());
  subs.forEach(s => {
    const o = document.createElement("option");
    o.value = s.url;
    o.textContent = "File: " + decodeURIComponent(s.url.split("?")[0].split("/").pop());
    sel.append(o);
  });
  if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
}

// Re-download a subtitle file from inside the page (same cookies, origin and
// referer as the page's own player). Tries every frame; first success wins.
async function fetchInPage(url) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      world: "MAIN",
      args: [url],
      func: async u => {
        try {
          const r = await fetch(u, { credentials: "include" });
          if (!r.ok) return null;
          const t = await r.text();
          return /^\s*(WEBVTT|\[Script Info\]|\d+\s*\r?\n\d\d:\d\d)/.test(t) ? t : null;
        } catch { return null; }
      }
    });
    const hit = results.find(r => r && r.result);
    return hit ? hit.result : null;
  } catch { return null; }
}

async function withText(s) {
  if (!s) return s;
  if (!s.text) s.text = await fetchInPage(s.url);
  return s.text ? { url: s.url, text: s.text } : { url: s.url, headers: s.headers };
}

async function chosenSubtitle() {
  const v = $("subs").value;
  // Re-read the list: subtitle text may have arrived since the popup opened.
  const r = await chrome.runtime.sendMessage({ type: "list", tabId: tab.id });
  found = r.items || found;
  if (v === "auto") {
    // A subtitle file the page loaded (the helper also finds subtitle tracks
    // inside HLS streams by itself). Prefer one we already have the text of.
    const subs = found.filter(i => i.kind === "sub");
    const s = subs.find(i => i.text) || subs[subs.length - 1];
    return s ? await withText(s) : undefined;
  }
  if (!v) return null;
  const s = found.find(i => i.url === v) || { url: v, headers: {} };
  return await withText(s);
}

async function pausePage() {
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: () => document.querySelectorAll("video,audio").forEach(m => m.pause())
    });
  } catch { /* some pages block scripts; not important */ }
}

async function send(v) {
  say("Sending to the TV… (takes a few seconds)");
  try {
    const sub = await chosenSubtitle();
    const body = { url: v.url, headers: v.headers, title: tab.title };
    if (sub !== undefined) body.subtitle = sub;      // null = explicitly off
    const r = await helper("/cast", body);
    await pausePage();
    say("Playing on the TV" + (r.subtitles ? " with subtitles."
      : sub ? ". Subtitles failed: " + (r.warning || "unknown error") : "."), !r.subtitles && !!sub);
    poll();
  } catch (e) {
    say("Couldn't send: " + e.message, true);
  }
}

async function tryPage() {
  say("Asking yt-dlp to find the video on this page…");
  try {
    const body = { pageUrl: tab.url, title: tab.title };
    const sub = await chosenSubtitle();
    if (sub) body.subtitle = sub;
    const r = await helper("/cast", body);
    await pausePage();
    say("Playing on the TV" + (r.subtitles ? " with subtitles." : "."));
    poll();
  } catch (e) {
    say("yt-dlp couldn't use this page: " + e.message, true);
  }
}

// ---------------------------------------------------------------- now playing

async function poll() {
  let s;
  try {
    s = await helper("/status");
  } catch {
    $("conn").textContent = "Helper not set up";
    $("conn").className = "pill bad";
    say("The Mac helper didn't answer. Run ./install.sh in the send-to-tv folder (see README).", true);
    return;
  }
  lastStatus = s;
  const reachable = s.state !== "UNREACHABLE" && s.state !== "NO_TV";
  $("conn").textContent = reachable ? (s.tv && s.tv.name ? s.tv.name : "TV connected")
    : s.state === "NO_TV" ? "No TV found" : "TV not reachable";
  $("conn").title = s.tv ? `${s.tv.name} · ${s.tv.model} · ${s.tv.ip}` : "";
  $("conn").className = "pill " + (reachable ? "ok" : "bad");
  if (!reachable) {
    say(/route to host/i.test(s.tvError || "")
      ? "macOS is blocking the helper from the network: System Settings → Privacy & Security → Local Network → turn on Python."
      : "Can't find the TV. Is it on and on the same Wi-Fi as this Mac?", true);
  }
  const sess = s.session;
  const active = sess && ["PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING"].includes(s.state);
  $("now").hidden = !active;
  if (!active) return;
  $("nowTitle").textContent = sess.title;
  $("pos").textContent = fmt(sess.position);
  $("dur").textContent = sess.duration ? fmt(sess.duration) : "live";
  if (!seeking && sess.duration) {
    $("seek").max = Math.floor(sess.duration);
    $("seek").value = Math.floor(sess.position || 0);
  }
  $("seek").disabled = !sess.duration;
  $("playpause").textContent = s.state === "PLAYING" ? "❚❚" : "▶";
  if (s.volume != null && document.activeElement !== $("volume")) {
    $("volume").value = s.volume;
    $("volNum").textContent = s.volume;
  }
  $("nowSubs").textContent = sess.subtitles
    ? "Subtitles sent. If you don't see them, press Tools/Subtitle on the TV remote and turn them on."
    : "";
}

async function control(action, value) {
  try { await helper("/control", { action, value }); } catch (e) { say(e.message, true); }
  setTimeout(poll, 700);
}

// ---------------------------------------------------------------- wiring

$("playpause").onclick = () => control(lastStatus && lastStatus.state === "PLAYING" ? "pause" : "play");
$("back30").onclick = () => control("skip", -30);
$("back10").onclick = () => control("skip", -10);
$("fwd10").onclick = () => control("skip", 10);
$("fwd30").onclick = () => control("skip", 30);
$("stop").onclick = () => control("stop");
$("seek").oninput = () => { seeking = true; $("pos").textContent = fmt(+$("seek").value); };
$("seek").onchange = async () => { await control("seek", +$("seek").value); seeking = false; };
$("volume").oninput = () => { $("volNum").textContent = $("volume").value; };
$("volume").onchange = () => control("volume", +$("volume").value);
$("tryPage").onclick = tryPage;
$("refresh").onclick = async () => {
  await chrome.runtime.sendMessage({ type: "clear", tabId: tab.id });
  await chrome.tabs.reload(tab.id);
  window.close();
};
$("subs").onchange = async () => {
  if (!lastStatus || !lastStatus.session) return;
  try {
    const sub = await chosenSubtitle();
    say("Updating subtitles on the TV…");
    await helper("/subtitle", sub ? sub : { url: null });
    say(sub ? "Subtitles updated." : "Subtitles off.");
  } catch (e) { say(e.message, true); }
};

(async () => {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const r = await chrome.runtime.sendMessage({ type: "list", tabId: tab.id });
  found = r.items || [];
  renderList();
  await poll();
  setInterval(poll, 1500);
  setInterval(async () => {
    const r = await chrome.runtime.sendMessage({ type: "list", tabId: tab.id });
    if ((r.items || []).length !== found.length) { found = r.items; renderList(); }
  }, 2000);
})();
