const HELPER = "http://127.0.0.1:8770";
const $ = id => document.getElementById(id);

let tab = null;
let found = [];          // streams and subtitle files seen on this tab
let lastStatus = null;   // last /status answer from the helper
let sendingStep = null;  // what this popup is doing while a video is being sent
let acting = null;       // short label while a control (seek, stop…) is in flight
let expectUntil = 0;     // just sent a video: keep the player card up until it starts
let seeking = false;     // the user is dragging the position slider
let toastTimer = null;

async function helper(path, body) {
  const r = await fetch(HELPER + path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  } : {});
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "helper error");
  return j;
}

// A short result message under everything else. Good news fades by itself.
function toast(text, kind) {
  const m = $("msg");
  clearTimeout(toastTimer);
  m.hidden = !text;
  m.textContent = text || "";
  m.className = "toast" + (kind ? " " + kind : "");
  if (text && kind !== "bad") toastTimer = setTimeout(() => { m.hidden = true; }, 6000);
}

// A standing problem (no helper, no TV). Cleared when the problem goes away.
function banner(text) {
  $("banner").hidden = !text;
  $("banner").textContent = text || "";
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

function fileName(u) {
  const name = u.split(/[?#]/)[0].split("/").pop() || u;
  try { return decodeURIComponent(name); } catch { return name; }
}

// ---------------------------------------------------------------- list

const KIND_LABEL = { hls: "Stream", dash: "Stream", file: "Video file" };
const KIND_NOTE = { hls: "HLS", dash: "DASH", file: "" };

function rank(i) {
  // Prefer master playlists, then other playlists, then plain files.
  const u = i.url.toLowerCase();
  let r = { hls: 0, dash: 1, file: 2 }[i.kind] ?? 3;
  if (i.kind === "hls" && /master|playlist|index|manifest/.test(u)) r -= 0.5;
  return r;
}

function itemRow(v, best) {
  const row = document.createElement("div");
  row.className = "item" + (best ? " best" : "");
  const meta = document.createElement("div");
  meta.className = "meta";
  const kind = document.createElement("div");
  kind.className = "kind";
  kind.append(KIND_LABEL[v.kind] + (KIND_NOTE[v.kind] ? " · " + KIND_NOTE[v.kind] : ""));
  if (best) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = "Best match";
    kind.append(badge);
  }
  const url = document.createElement("div");
  url.className = "url";
  url.textContent = shortUrl(v.url);
  url.title = v.url;
  meta.append(kind, url);
  const btn = document.createElement("button");
  btn.className = best ? "primary" : "";
  btn.textContent = best ? "Send to TV" : "Send";
  btn.onclick = () => send(v);
  row.append(meta, btn);
  return row;
}

function renderList() {
  const vids = found.filter(i => i.kind !== "sub").sort((a, b) => rank(a) - rank(b) || a.seen - b.seen);
  $("list").textContent = "";
  $("moreList").textContent = "";
  $("empty").hidden = vids.length > 0;
  if (vids.length) $("list").append(itemRow(vids[0], true));
  const others = vids.slice(1);
  others.forEach(v => $("moreList").append(itemRow(v, false)));
  $("more").hidden = others.length === 0;
  $("moreSummary").textContent = others.length === 1
    ? "1 other stream on this page" : `${others.length} other streams on this page`;

  const subs = found.filter(i => i.kind === "sub");
  const sel = $("subs");
  const keep = sel.value;
  [...sel.options].slice(2).forEach(o => o.remove());
  subs.forEach(s => {
    const o = document.createElement("option");
    o.value = s.url;
    o.textContent = fileName(s.url);
    sel.append(o);
  });
  if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
}

async function refreshFound() {
  const r = await chrome.runtime.sendMessage({ type: "list", tabId: tab.id });
  return (r && r.items) || [];
}

// ---------------------------------------------------------------- subtitles

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

// undefined = let the helper look inside the stream, null = off.
async function chosenSubtitle() {
  const v = $("subs").value;
  // Re-read the list: subtitle text may have arrived since the popup opened.
  found = await refreshFound().catch(() => found);
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

// ---------------------------------------------------------------- the page's player

async function pausePage() {
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: () => document.querySelectorAll("video,audio").forEach(m => m.pause())
    });
  } catch { /* some pages block scripts; not important */ }
}

// ---------------------------------------------------------------- sending

// Runs one "send a video" job with the loading card up from the first click
// until the helper answers. `job` returns the helper's /cast answer.
async function sendJob(firstStep, failPrefix, job) {
  if (sendingStep) return;
  sendingStep = firstStep;
  toast("");
  render();
  try {
    const r = await job(step => { sendingStep = step; render(); });
    await pausePage();
    expectUntil = Date.now() + 12000;
    if (r.wantedSubs && !r.subtitles) {
      toast(`Playing on the TV, but without subtitles: ${r.warning || "they could not be read"}.`, "bad");
    } else {
      toast(`Playing on the TV${r.subtitles ? ", with subtitles" : ""}.`, "good");
    }
  } catch (e) {
    toast(failPrefix + e.message, "bad");
  } finally {
    sendingStep = null;
    await poll();
  }
}

function send(v) {
  return sendJob("Reading the page…", "Couldn't send it: ", async step => {
    const sub = await chosenSubtitle();
    const body = { url: v.url, headers: v.headers, title: tab.title };
    if (sub !== undefined) body.subtitle = sub;      // null = explicitly off
    step("Contacting the TV…");
    const r = await helper("/cast", body);
    return { ...r, wantedSubs: !!sub };
  });
}

function tryPage() {
  return sendJob("Reading the page…", "The page link didn't work: ", async step => {
    const sub = await chosenSubtitle();
    const body = { pageUrl: tab.url, title: tab.title };
    if (sub) body.subtitle = sub;
    step("Finding the video on the page…");
    const r = await helper("/cast", body);
    return { ...r, wantedSubs: !!sub };
  });
}

// ---------------------------------------------------------------- display

const ACTIVE_STATES = ["PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING"];

function render() {
  const s = lastStatus;
  // The helper reports what a cast is doing even if another popup started it.
  const step = (s && s.phase ? s.phase + "…" : null) || sendingStep;
  document.body.classList.toggle("is-busy", !!step);
  $("busy").hidden = !step;
  if (step) $("busyStep").textContent = step;

  const sess = s && s.session;
  const active = !step && sess &&
    (ACTIVE_STATES.includes(s.state) || Date.now() < expectUntil);
  $("now").hidden = !active;
  if (!active) return;

  const playing = s.state === "PLAYING", paused = s.state === "PAUSED_PLAYBACK";
  if (playing || paused) expectUntil = 0;
  $("now").classList.toggle("paused", paused && !acting);
  $("now").classList.toggle("waiting", !!acting || !(playing || paused));
  $("nowState").textContent = acting || (playing ? "Playing on the TV" : paused ? "Paused" : "Starting…");
  $("nowTitle").textContent = sess.title;
  $("nowTitle").title = sess.title;
  if (!seeking) $("pos").textContent = fmt(sess.position);
  $("dur").textContent = sess.duration ? fmt(sess.duration) : "Live";
  if (!seeking && sess.duration) {
    $("seek").max = Math.floor(sess.duration);
    $("seek").value = Math.floor(sess.position || 0);
  }
  $("seek").disabled = !sess.duration;
  for (const id of ["back30", "back10", "fwd10", "fwd30"]) $(id).disabled = !sess.duration;
  if (s.volume != null && document.activeElement !== $("volume")) {
    $("volume").value = s.volume;
    $("volNum").textContent = s.volume;
  }
  $("nowSubs").textContent = sess.subtitles
    ? "Subtitles are on. Not showing? Turn them on with the remote's Subtitle setting."
    : "";
}

function renderConnection(s) {
  const reachable = s.state !== "UNREACHABLE" && s.state !== "NO_TV";
  $("connText").textContent = reachable ? (s.tv && s.tv.name ? s.tv.name : "TV connected")
    : s.state === "NO_TV" ? "No TV found" : "TV not reachable";
  $("conn").title = s.tv ? [s.tv.name, s.tv.model, s.tv.ip].filter(Boolean).join(" · ") : "";
  $("conn").className = "chip " + (reachable ? "ok" : "bad");
  banner(reachable ? "" : /route to host/i.test(s.tvError || "")
    ? "macOS is blocking the helper from your network. Open System Settings → Privacy & Security → Local Network and turn on Python."
    : "Can't find the TV. Check that it is on and on the same Wi-Fi as this Mac.");
}

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    lastStatus = await helper("/status");
    renderConnection(lastStatus);
  } catch {
    lastStatus = null;
    $("connText").textContent = "Helper not running";
    $("conn").className = "chip bad";
    banner("The Mac helper didn't answer. Run ./install.sh in the send-to-tv folder (see the README).");
  } finally {
    polling = false;
  }
  render();
}

async function control(action, value, label) {
  acting = label || null;
  render();
  try { await helper("/control", { action, value }); }
  catch (e) { toast(e.message, "bad"); }
  acting = null;
  await poll();
}

// ---------------------------------------------------------------- wiring

$("playpause").onclick = () => control(lastStatus && lastStatus.state === "PLAYING" ? "pause" : "play");
$("back30").onclick = () => control("skip", -30, "Jumping…");
$("back10").onclick = () => control("skip", -10, "Jumping…");
$("fwd10").onclick = () => control("skip", 10, "Jumping…");
$("fwd30").onclick = () => control("skip", 30, "Jumping…");
$("stop").onclick = () => control("stop", undefined, "Stopping…");
$("seek").oninput = () => { seeking = true; $("pos").textContent = fmt(+$("seek").value); };
$("seek").onchange = async () => { await control("seek", +$("seek").value, "Jumping…"); seeking = false; };
$("volume").oninput = () => { $("volNum").textContent = $("volume").value; };
$("volume").onchange = () => control("volume", +$("volume").value);
$("tryPage").onclick = tryPage;
$("refresh").onclick = async () => {
  await chrome.runtime.sendMessage({ type: "clear", tabId: tab.id });
  await chrome.tabs.reload(tab.id);
  window.close();
};
$("subs").onchange = async () => {
  if (!lastStatus || !lastStatus.session) return;   // nothing on the TV: applies to the next send
  acting = "Updating subtitles…";
  render();
  try {
    const sub = await chosenSubtitle();
    await helper("/subtitle", sub ? sub : { url: null });
    toast(sub ? "Subtitles updated." : "Subtitles off.", "good");
  } catch (e) { toast(e.message, "bad"); }
  acting = null;
  await poll();
};

(async () => {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  found = await refreshFound();
  renderList();
  // Check the helper often while something is being sent, otherwise calmly.
  const loop = async () => {
    await poll();
    setTimeout(loop, document.body.classList.contains("is-busy") ? 500 : 1500);
  };
  loop();
  setInterval(async () => {
    const items = await refreshFound().catch(() => null);
    if (items && items.length !== found.length) { found = items; renderList(); }
  }, 2000);
})();
