// Runs inside the page (MAIN world) before the page's own scripts.
// When the page downloads a subtitle file, keep a copy of the text so it can
// be sent to the TV even if the server won't serve that link a second time.
(() => {
  if (window.__tvcastHooked) return;
  window.__tvcastHooked = true;

  const SUB_URL = /\.(vtt|webvtt|srt|ass|ssa)(\?|#|$)/i;
  const SUB_TYPE = /(text\/vtt|subrip|text\/srt|x-ssa|x-ass)/i;
  const MAX = 5 * 1024 * 1024;

  const looksLikeSubs = t => /^\s*(WEBVTT|\[Script Info\]|\d+\s*\r?\n\d\d:\d\d:\d\d[,.]\d+\s+-->)/.test(t);

  function post(url, text) {
    if (!text || text.length > MAX || !looksLikeSubs(text)) return;
    try {
      window.postMessage({ __tvcast: "sub", url: new URL(url, location.href).href, text }, "*");
    } catch {}
  }

  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      const p = origFetch.apply(this, arguments);
      try {
        const url = typeof input === "string" ? input : (input && input.url) || String(input);
        p.then(res => {
          const ct = res.headers.get("content-type") || "";
          if (SUB_URL.test(url) || SUB_TYPE.test(ct) || SUB_URL.test(res.url)) {
            res.clone().text().then(t => post(res.url || url, t)).catch(() => {});
          }
        }).catch(() => {});
      } catch {}
      return p;
    };
  }

  const XO = XMLHttpRequest.prototype.open;
  const XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__tvcastUrl = url;
    return XO.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener("load", () => {
      try {
        const url = this.responseURL || this.__tvcastUrl || "";
        const ct = this.getResponseHeader("content-type") || "";
        if (!(SUB_URL.test(url) || SUB_TYPE.test(ct))) return;
        let t = null;
        if (this.responseType === "" || this.responseType === "text") t = this.responseText;
        else if (this.responseType === "json") return;
        else if (this.response instanceof ArrayBuffer) t = new TextDecoder().decode(this.response);
        else if (this.response instanceof Blob) { this.response.text().then(x => post(url, x)); return; }
        post(url, t);
      } catch {}
    });
    return XS.apply(this, arguments);
  };
})();
