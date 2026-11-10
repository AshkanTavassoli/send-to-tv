#!/usr/bin/env python3
"""
Send to TV - Mac helper.

Runs on the Mac. The Chrome extension tells it which video stream a page is
playing (plus the headers/cookies the site expects, and any subtitle file).
The helper pulls the stream with ffmpeg, remuxes it to MPEG-TS (no re-encode
for H.264/HEVC) and serves it on the LAN. The TV (any DLNA/UPnP renderer,
found automatically) is told to play it, so its own hardware player decodes.
Subtitles are converted to SRT and handed to the TV through Samsung's
CaptionInfo DLNA extension, so the TV draws them itself.

Control API (localhost only):
  GET  /status
  POST /cast    {url, headers, title, audioUrl?, subtitle?: {url, headers, text?}}
                or {pageUrl, title?}            (resolved with yt-dlp)
  POST /control {action: play|pause|stop|seek|skip|volume, value}
  POST /subtitle {url, headers, text?} | {url: null}   (add/replace/remove)
LAN (for the TV):
  GET /stream/<id>.ts      GET /sub/<id>.srt
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

CONTROL_PORT = int(os.environ.get("TVCAST_PORT", "8770"))
SUB_LANGS = os.environ.get("TVCAST_SUB_LANGS", "en,en-US,en-GB").split(",")
SUB_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "tvcast-subs")
os.makedirs(SUB_DIR, exist_ok=True)


def tool(name):
    return shutil.which(name) or f"/opt/homebrew/bin/{name}"


FFMPEG, FFPROBE, YTDLP = tool("ffmpeg"), tool("ffprobe"), tool("yt-dlp")

# Headers worth forwarding from the browser request to the video server.
FORWARD_HEADERS = {"cookie", "referer", "origin", "user-agent", "authorization",
                   "accept-language"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def clean_headers(h):
    return {k.lower(): v for k, v in (h or {}).items()
            if k.lower() in FORWARD_HEADERS and v}


def header_args(headers):
    """ffmpeg/ffprobe input options carrying the browser's headers."""
    args = []
    ua = headers.get("user-agent")
    if ua:
        args += ["-user_agent", ua]
    extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if k != "user-agent")
    if extra:
        args += ["-headers", extra]
    return args


def is_http(url):
    return url.startswith("http://") or url.startswith("https://")


RECONNECT = ["-reconnect", "1", "-reconnect_streamed", "1",
             "-reconnect_delay_max", "5", "-rw_timeout", "15000000"]


# --------------------------------------------------------------------------- TV

CONFIG_DIR = os.path.expanduser("~/Library/Application Support/TVCast")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
# Optional: the TV's IP address or part of its name, if you have several.
TV_HINT = os.environ.get("TVCAST_TV", "").strip()
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(d):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception as e:
        log("could not save config:", e)


def tag(xml, name):
    m = re.search(rf"<(?:\w+:)?{name}>([^<]*)</", xml)
    return m.group(1).strip() if m else None


class TV:
    """A DLNA/UPnP media renderer (tested on Samsung Tizen TVs)."""

    def __init__(self, location, desc):
        self.location = location
        self.ip = urllib.parse.urlparse(location).hostname
        self.name = tag(desc, "friendlyName") or self.ip
        self.model = tag(desc, "modelName") or ""
        self.samsung = "samsung" in (tag(desc, "manufacturer") or "").lower()
        base = tag(desc, "URLBase") or location
        self.avt_url = self.rc_url = None
        for svc in re.findall(r"<service>(.*?)</service>", desc, re.S):
            st, cu = tag(svc, "serviceType") or "", tag(svc, "controlURL")
            if cu and ":AVTransport:" in st:
                self.avt_url = urllib.parse.urljoin(base, cu)
            elif cu and ":RenderingControl:" in st:
                self.rc_url = urllib.parse.urljoin(base, cu)
        if not self.avt_url:
            raise ValueError("not a media renderer")

    @classmethod
    def from_location(cls, location, timeout=4):
        with urllib.request.urlopen(location, timeout=timeout) as r:
            return cls(location, r.read().decode("utf-8", "replace"))

    def info(self):
        return {"ip": self.ip, "name": self.name, "model": self.model}

    def matches(self, hint):
        return not hint or hint == self.ip or hint.lower() in self.name.lower()

    def _soap(self, url, service, action, args=""):
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
            f'<InstanceID>0</InstanceID>{args}</u:{action}></s:Body></s:Envelope>'
        ).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"urn:schemas-upnp-org:service:{service}:1#{action}"',
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read().decode("utf-8", "replace")

    def avt(self, action, args=""):
        return self._soap(self.avt_url, "AVTransport", action, args)

    def rc(self, action, args=""):
        if not self.rc_url:
            raise RuntimeError("this TV has no volume control over DLNA")
        return self._soap(self.rc_url, "RenderingControl", action, args)

    def load(self, uri, title, sub_uri=None):
        # sec:CaptionInfo is Samsung's DLNA subtitle extension; others ignore it.
        sub = (f'<sec:CaptionInfoEx sec:type="srt">{escape(sub_uri)}</sec:CaptionInfoEx>'
               f'<sec:CaptionInfo sec:type="srt">{escape(sub_uri)}</sec:CaptionInfo>'
               if sub_uri else "")
        didl = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:sec="http://www.sec.co.kr/">'
            '<item id="0" parentID="-1" restricted="1">'
            f'<dc:title>{escape(title)}</dc:title>'
            '<upnp:class>object.item.videoItem</upnp:class>'
            f'<res protocolInfo="http-get:*:video/mp2t:*">{escape(uri)}</res>'
            f'{sub}</item></DIDL-Lite>'
        )
        try:
            self.avt("Stop")
        except Exception:
            pass
        self.avt("SetAVTransportURI",
                 f"<CurrentURI>{escape(uri)}</CurrentURI>"
                 f"<CurrentURIMetaData>{escape(didl)}</CurrentURIMetaData>")
        self.avt("Play", "<Speed>1</Speed>")

    def play(self):
        self.avt("Play", "<Speed>1</Speed>")

    def pause(self):
        self.avt("Pause")

    def stop(self):
        self.avt("Stop")

    def state(self):
        m = re.search(r"<CurrentTransportState>([^<]*)", self.avt("GetTransportInfo"))
        return m.group(1) if m else "UNKNOWN"

    def position(self):
        m = re.search(r"<RelTime>([^<]*)", self.avt("GetPositionInfo"))
        return hms_to_s(m.group(1)) if m else 0.0

    def get_volume(self):
        m = re.search(r"<CurrentVolume>(\d+)",
                      self.rc("GetVolume", "<Channel>Master</Channel>"))
        return int(m.group(1)) if m else None

    def set_volume(self, v):
        v = max(0, min(100, int(v)))
        self.rc("SetVolume",
                f"<Channel>Master</Channel><DesiredVolume>{v}</DesiredVolume>")
        return v


def hms_to_s(t):
    try:
        parts = [float(p) for p in t.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        return 0.0


def discover(timeout=3):
    """Every DLNA media renderer that answers on the LAN."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\n'
           "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(0.5)
    locations = []
    try:
        s.sendto(msg, ("239.255.255.250", 1900))
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            m = re.search(rb"(?im)^location:\s*(\S+)", data)
            if m and m.group(1).decode() not in locations:
                locations.append(m.group(1).decode())
    finally:
        s.close()
    tvs = []
    for loc in locations:
        try:
            tvs.append(TV.from_location(loc))
        except Exception:
            pass
    return tvs


TVREF = {"tv": None, "last_search": 0.0}
_tv_lock = threading.Lock()


def _reachable(t):
    try:
        t.avt("GetTransportInfo")
        return True
    except Exception:
        return False


def _remember(t):
    TVREF["tv"] = t
    save_config({"location": t.location, "name": t.name})
    return t


def get_tv():
    """The TV to use: the last one (if it still answers), else search the LAN.
    Returns None if no TV can be found."""
    with _tv_lock:
        t = TVREF["tv"]
        if t and _reachable(t):
            return t
        cfg = load_config()
        loc = cfg.get("location")
        if loc and (not TV_HINT or TV_HINT == urllib.parse.urlparse(loc).hostname
                    or TV_HINT.lower() in (cfg.get("name") or "").lower()):
            try:
                t = TV.from_location(loc)
                if _reachable(t):
                    return _remember(t)
            except Exception:
                pass
        if IP_RE.match(TV_HINT):  # Samsung's fixed description address
            try:
                return _remember(TV.from_location(f"http://{TV_HINT}:9197/dmr"))
            except Exception:
                pass
        if time.time() - TVREF["last_search"] < 15:  # don't flood while the TV is off
            return None
        TVREF["last_search"] = time.time()
        try:
            tvs = [x for x in discover() if x.matches(TV_HINT)]
        except OSError as e:
            log("can't search the network (macOS Local Network permission?):", e)
            return None
        if not tvs:
            log("no TV found on the network")
            return None
        tvs.sort(key=lambda x: (not x.samsung, x.name))
        log("using TV:", tvs[0].name, tvs[0].ip,
            f"({len(tvs) - 1} other renderer(s) found)" if len(tvs) > 1 else "")
        return _remember(tvs[0])


def need_tv():
    t = get_tv()
    if not t:
        raise RuntimeError("No TV found. Is it on and on the same Wi-Fi as this Mac?")
    return t


def lan_ip_towards(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------------- session

class Session:
    def __init__(self, url, headers, title, audio_url=None):
        self.id = uuid.uuid4().hex[:10]
        self.url = url
        self.audio_url = audio_url
        self.headers = clean_headers(headers)
        self.title = title or "Video"
        self.offset = 0.0
        self.duration = None
        self.vcodec = None
        self.proc = None
        self.lock = threading.Lock()
        self.last_error = ""
        self.sub_url = None
        self.sub_headers = {}
        self.sub_srt = None      # full SRT text (unshifted), cached
        self.hls_sub_url = None  # subtitle rendition found in the HLS playlist

    # -- HLS master playlist ---------------------------------------------
    def pick_hls_variant(self):
        """If url is an HLS master playlist, pin the best variant (so ffmpeg
        doesn't download every quality at once) and discover its audio and
        subtitle renditions."""
        if not is_http(self.url):
            return
        try:
            req = urllib.request.Request(self.url, headers={
                ("User-Agent" if k == "user-agent" else k.title()): v
                for k, v in self.headers.items()})
            with urllib.request.urlopen(req, timeout=15) as r:
                text = r.read(2_000_000).decode("utf-8", "replace")
                base = r.geturl()
        except Exception as e:
            log("playlist fetch failed:", e)
            return
        if "#EXT-X-STREAM-INF" not in text:
            return
        attr = lambda line: dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line))
        unq = lambda v: (v or "").strip('"')
        media, variants, lines = [], [], text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-MEDIA:"):
                media.append({k: unq(v) for k, v in attr(line).items()})
            elif line.startswith("#EXT-X-STREAM-INF:"):
                a = {k: unq(v) for k, v in attr(line).items()}
                uri = next((l.strip() for l in lines[i + 1:]
                            if l.strip() and not l.startswith("#")), None)
                if uri:
                    a["URI"] = uri
                    variants.append(a)
        codecs_ok = [v for v in variants
                     if not re.search(r"vp09|av01|dvh1|dvhe", v.get("CODECS", ""))]
        best = max(codecs_ok or variants, key=lambda v: (
            int((v.get("RESOLUTION", "0x0").split("x") + ["0"])[1] or 0),
            int(v.get("BANDWIDTH", "0") or 0)))
        join = lambda u: urllib.parse.urljoin(base, u)
        self.url = join(best["URI"])
        log("HLS variant:", best.get("RESOLUTION"), best.get("BANDWIDTH"))

        def rendition(kind, group):
            cands = [m for m in media if m.get("TYPE") == kind and m.get("URI")
                     and (not group or m.get("GROUP-ID") == group)]
            if not cands:
                return None
            def rank(m):
                lang = (m.get("LANGUAGE") or "").lower()
                return (lang in [l.lower() for l in SUB_LANGS] or
                        lang.split("-")[0] == SUB_LANGS[0].split("-")[0],
                        m.get("DEFAULT") == "YES",
                        m.get("FORCED") != "YES")
            return join(max(cands, key=rank)["URI"])

        if best.get("AUDIO") and not self.audio_url:
            self.audio_url = rendition("AUDIO", best["AUDIO"])
        self.hls_sub_url = rendition("SUBTITLES", best.get("SUBTITLES"))
        if self.hls_sub_url:
            log("stream has a subtitle track")

    # -- probing ---------------------------------------------------------
    def probe(self):
        cmd = [FFPROBE, "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams"] + header_args(self.headers) + [self.url]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=25, text=True)
            info = json.loads(out.stdout or "{}")
        except Exception as e:
            log("probe failed:", e)
            return
        try:
            d = float(info.get("format", {}).get("duration"))
            self.duration = d if d > 0 else None
        except (TypeError, ValueError):
            self.duration = None
        vids = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
        if vids:
            self.vcodec = max(vids, key=lambda s: s.get("height") or 0).get("codec_name")
        log(f"probe: codec={self.vcodec} duration={self.duration}")

    # -- video -----------------------------------------------------------
    def ffmpeg_cmd(self):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin"]

        def add_input(url):
            nonlocal cmd
            if is_http(url):
                cmd += RECONNECT + header_args(self.headers)
            if self.offset > 0:
                cmd += ["-ss", f"{self.offset:.2f}"]
            cmd += ["-i", url]

        add_input(self.url)
        if self.audio_url:
            add_input(self.audio_url)
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
        cmd += ["-sn", "-dn"]
        if self.vcodec in (None, "h264", "hevc"):
            cmd += ["-c:v", "copy"]
        else:  # VP9/AV1/etc: the 2018 TV can't take these in TS -> HW encode
            cmd += ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-maxrate", "12M",
                    "-bufsize", "16M", "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-f", "mpegts", "-mpegts_flags", "+resend_headers",
                "-muxdelay", "0", "pipe:1"]
        return cmd

    def kill(self):
        with self.lock:
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.kill()
            try:
                p.wait(3)
            except Exception:
                pass

    # -- subtitles -------------------------------------------------------
    def set_subtitle(self, url, headers=None, text=None):
        self.sub_url = url
        self.sub_headers = clean_headers(headers) or self.headers
        self.sub_srt = None
        if text:
            # The browser already downloaded it: convert that copy.
            head = text.lstrip()[:20]
            ext = ".vtt" if head.startswith("WEBVTT") else \
                  ".ass" if head.startswith("[Script Info]") else ".srt"
            path = os.path.join(SUB_DIR, f"sub-{self.id}{ext}")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.sub_url = path
            self.load_subtitle()
        elif url:
            self.load_subtitle()

    def load_subtitle(self):
        """Download (WebVTT / SRT / ASS / HLS subtitle playlist) -> SRT text."""
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if is_http(self.sub_url):
            cmd += header_args(self.sub_headers)
        cmd += ["-i", self.sub_url, "-map", "0:s:0", "-c:s", "srt", "-f", "srt", "pipe:1"]
        out = subprocess.run(cmd, capture_output=True, timeout=60)
        if out.returncode != 0 or not out.stdout.strip():
            raise RuntimeError("could not read subtitles: " +
                               out.stderr.decode("utf-8", "replace").strip()[-200:])
        self.sub_srt = out.stdout.decode("utf-8", "replace")
        log(f"subtitles loaded ({self.sub_srt.count(' --> ')} cues)")

    def shifted_srt(self):
        """SRT re-timed so it lines up after a seek (the TV's clock restarts at 0)."""
        if self.sub_srt is None:
            return None
        if self.offset <= 0:
            return self.sub_srt
        off = self.offset
        out, n = [], 0
        for block in re.split(r"\r?\n\r?\n", self.sub_srt.strip()):
            lines = block.splitlines()
            idx = next((i for i, l in enumerate(lines) if " --> " in l), None)
            if idx is None:
                continue
            a, b = [srt_to_s(x) for x in lines[idx].split(" --> ")[:2]]
            if b - off <= 0:
                continue
            n += 1
            out.append(f"{n}\n{s_to_srt(max(0, a - off))} --> {s_to_srt(b - off)}\n"
                       + "\n".join(lines[idx + 1:]))
        return "\n\n".join(out) + "\n"


def srt_to_s(t):
    t = t.strip().split(" ")[0].replace(",", ".")
    return hms_to_s(t)


def s_to_srt(s):
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


STATE = {"session": None}


def current():
    return STATE["session"]


def urls_for(s, tv):
    host = lan_ip_towards(tv.ip)
    base = f"http://{host}:{CONTROL_PORT}"
    return (f"{base}/stream/{s.id}.ts",
            f"{base}/sub/{s.id}.srt" if s.sub_srt is not None else None)


def resolve_page(page_url):
    """Use yt-dlp to find a playable stream (and subtitles) for a page URL."""
    fmt = ("b[vcodec^=avc1]/bv*[vcodec^=avc1]+ba[acodec^=mp4a]/"
           "bv*[vcodec^=avc1]+ba/b/bv*+ba")
    out = subprocess.run([YTDLP, "-J", "--no-playlist", "-f", fmt, page_url],
                         capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        msg = (out.stderr.strip().splitlines() or ["yt-dlp could not find a video"])[-1]
        raise RuntimeError(msg)
    info = json.loads(out.stdout)
    fmts = info.get("requested_formats") or [info]
    video = fmts[0]
    audio = fmts[1]["url"] if len(fmts) > 1 else None
    sub = None
    subs = info.get("subtitles") or {}
    for lang in SUB_LANGS + list(subs.keys()):
        tracks = subs.get(lang)
        if tracks:
            pref = sorted(tracks, key=lambda t: {"vtt": 0, "srt": 1, "ass": 2}
                          .get(t.get("ext"), 9))
            sub = {"url": pref[0]["url"], "headers": video.get("http_headers", {})}
            break
    return (video["url"], video.get("http_headers", {}), info.get("title"),
            audio, sub)


def start_cast(url, headers, title, audio_url=None, subtitle=None):
    old = current()
    if old:
        old.kill()
    tv = need_tv()
    s = Session(url, headers, title, audio_url)
    STATE["session"] = s
    s.pick_hls_variant()
    s.probe()
    if not (subtitle and subtitle.get("url")) and s.hls_sub_url:
        subtitle = {"url": s.hls_sub_url, "headers": s.headers}
    if subtitle and (subtitle.get("url") or subtitle.get("text")):
        try:
            s.set_subtitle(subtitle.get("url"), subtitle.get("headers"),
                           subtitle.get("text"))
        except Exception as e:
            log("subtitle error:", e)
            s.last_error = str(e)
    stream, sub = urls_for(s, tv)
    log("casting", s.title, "->", stream, "subs:", bool(sub))
    tv.load(stream, s.title, sub)
    return s


def reload_at(offset):
    s = current()
    if not s:
        raise RuntimeError("nothing is playing")
    s.kill()
    s.offset = max(0.0, offset)
    if s.duration:
        s.offset = min(s.offset, max(0.0, s.duration - 5))
    s.id = uuid.uuid4().hex[:10]
    tv = need_tv()
    stream, sub = urls_for(s, tv)
    tv.load(stream, s.title, sub)


def status():
    s = current()
    out = {"ok": True, "tv": None, "session": None}
    tv = get_tv()
    if not tv:
        out["state"] = "NO_TV"
        out["tvError"] = "No TV found on the network"
        return out
    out["tv"] = tv.info()
    try:
        out["state"] = tv.state()
    except Exception as e:
        out["state"] = "UNREACHABLE"
        out["tvError"] = str(e)
    try:
        out["volume"] = tv.get_volume()
    except Exception:
        out["volume"] = None
    if s:
        pos = None
        try:
            pos = s.offset + tv.position()
        except Exception:
            pass
        out["session"] = {"title": s.title, "url": s.url, "position": pos,
                          "duration": s.duration, "codec": s.vcodec,
                          "subtitles": s.sub_srt is not None,
                          "error": s.last_error}
    return out


# ------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SendToTV/1.0"

    def log_message(self, fmt, *args):
        pass

    def _local_only(self):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.send_error(403)
            return False
        return True

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _stream_headers(self, s):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org",
                         "DLNA.ORG_OP=00;DLNA.ORG_CI=1;"
                         "DLNA.ORG_FLAGS=01700000000000000000000000000000")
        if s.sub_srt is not None:
            host = self.connection.getsockname()[0]
            self.send_header("CaptionInfo.sec",
                             f"http://{host}:{CONTROL_PORT}/sub/{s.id}.srt")
        self.send_header("Connection", "close")
        self.end_headers()

    def _session_for(self, prefix, ext):
        s = current()
        if s and self.path.split("?")[0] == f"{prefix}{s.id}{ext}":
            return s
        return None

    def do_HEAD(self):
        s = self._session_for("/stream/", ".ts")
        if s:
            return self._stream_headers(s)
        self.send_error(404)

    def do_GET(self):
        if self.path.startswith("/stream/"):
            return self.serve_stream()
        if self.path.startswith("/sub/"):
            return self.serve_sub()
        if not self._local_only():
            return
        if self.path == "/status":
            return self._json(200, status())
        self._json(404, {"ok": False, "error": "not found"})

    def serve_sub(self):
        s = self._session_for("/sub/", ".srt")
        text = s.shifted_srt() if s else None
        if text is None:
            return self.send_error(404)
        body = text.encode("utf-8")
        log("TV fetched subtitles")
        self.send_response(200)
        self.send_header("Content-Type", "text/srt; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_stream(self):
        s = self._session_for("/stream/", ".ts")
        if not s:
            return self.send_error(404)
        s.kill()  # the TV re-requests on reconnect; only one pipe at a time
        log("TV connected, starting ffmpeg at", f"{s.offset:.0f}s")
        p = subprocess.Popen(s.ffmpeg_cmd(), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        with s.lock:
            s.proc = p
        # Keep the Mac awake only while this stream is being fed to the TV.
        try:
            subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(p.pid)])
        except Exception:
            pass
        threading.Thread(target=self._drain_err, args=(s, p), daemon=True).start()
        self.close_connection = True
        self._stream_headers(s)
        try:
            while True:
                chunk = p.stdout.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if p.poll() is None:
                p.kill()
            log("stream connection closed")

    @staticmethod
    def _drain_err(s, p):
        for line in p.stderr:
            line = line.decode("utf-8", "replace").strip()
            if line and not re.search(r"rfc8216|Packet corrupt|PES packet size", line):
                s.last_error = line[-300:]
                log("ffmpeg:", line[:300])

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        if not self._local_only():
            return
        try:
            data = self._body()
            if self.path == "/cast":
                if data.get("pageUrl") and not data.get("url"):
                    url, headers, title, audio, sub = resolve_page(data["pageUrl"])
                    s = start_cast(url, headers, data.get("title") or title, audio,
                                   data.get("subtitle") or sub)
                else:
                    s = start_cast(data["url"], data.get("headers"), data.get("title"),
                                   data.get("audioUrl"), data.get("subtitle"))
                return self._json(200, {"ok": True, "title": s.title,
                                        "duration": s.duration, "codec": s.vcodec,
                                        "subtitles": s.sub_srt is not None,
                                        "warning": s.last_error or None})
            if self.path == "/subtitle":
                s = current()
                if not s:
                    raise RuntimeError("nothing is playing")
                s.set_subtitle(data.get("url"), data.get("headers"), data.get("text"))
                pos = s.offset + need_tv().position()
                reload_at(pos)  # TV only reads subtitles when media is loaded
                return self._json(200, {"ok": True, "subtitles": s.sub_srt is not None})
            if self.path == "/control":
                a, v = data.get("action"), data.get("value")
                s = current()
                tv = need_tv()
                if a == "play":
                    tv.play()
                elif a == "pause":
                    tv.pause()
                elif a == "stop":
                    tv.stop()
                    if s:
                        s.kill()
                elif a == "seek":       # absolute seconds
                    reload_at(float(v))
                elif a == "skip":       # relative seconds
                    reload_at((s.offset + tv.position() if s else 0) + float(v))
                elif a == "volume":
                    return self._json(200, {"ok": True, "volume": tv.set_volume(v)})
                else:
                    return self._json(400, {"ok": False, "error": f"bad action {a}"})
                return self._json(200, {"ok": True})
            self._json(404, {"ok": False, "error": "not found"})
        except Exception as e:
            log("error:", repr(e))
            self._json(500, {"ok": False, "error": str(e)})


IDLE_EXIT = int(os.environ.get("TVCAST_IDLE_EXIT", "900"))  # seconds; 0 = never
LAST_ACTIVITY = [time.time()]


def launchd_socket(name="Listener"):
    """Socket handed over by launchd when started on demand, else None."""
    import ctypes
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        fds = ctypes.POINTER(ctypes.c_int)()
        cnt = ctypes.c_size_t(0)
        fn = lib.launch_activate_socket
        fn.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
                       ctypes.POINTER(ctypes.c_size_t)]
        if fn(name.encode(), ctypes.byref(fds), ctypes.byref(cnt)) != 0 or not cnt.value:
            return None
        return socket.socket(fileno=fds[0])
    except Exception:
        return None


def idle_watchdog():
    """Exit when nothing is playing and nobody has talked to us for a while.
    launchd starts us again on the next connection."""
    while True:
        time.sleep(30)
        s = current()
        streaming = bool(s and s.proc and s.proc.poll() is None)
        if streaming:
            LAST_ACTIVITY[0] = time.time()
        elif time.time() - LAST_ACTIVITY[0] > IDLE_EXIT:
            log("idle, shutting down until needed")
            os._exit(0)


class ActivityServer(ThreadingHTTPServer):
    daemon_threads = True

    def process_request(self, request, client_address):
        LAST_ACTIVITY[0] = time.time()
        super().process_request(request, client_address)


def main():
    sock = launchd_socket()
    if sock:
        srv = ActivityServer(("0.0.0.0", CONTROL_PORT), Handler, bind_and_activate=False)
        srv.socket.close()
        srv.socket = sock
        srv.server_address = sock.getsockname()[:2]
        log("started on demand by launchd")
    else:
        srv = ActivityServer(("0.0.0.0", CONTROL_PORT), Handler)
    if IDLE_EXIT and sock:
        threading.Thread(target=idle_watchdog, daemon=True).start()
    log(f"Send to TV helper listening on port {CONTROL_PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        s = current()
        if s:
            s.kill()


if __name__ == "__main__":
    main()
