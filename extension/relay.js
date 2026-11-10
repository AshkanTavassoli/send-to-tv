// Passes subtitle text captured by hook.js (page world) to the extension.
window.addEventListener("message", e => {
  const d = e.data;
  if (e.source !== window || !d || d.__tvcast !== "sub" || typeof d.text !== "string") return;
  try { chrome.runtime.sendMessage({ type: "subtext", url: d.url, text: d.text }); } catch {}
});
