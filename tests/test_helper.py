"""Tests for the Mac helper. No TV, ffmpeg or network needed.

Run from the repository root:  python3 -m unittest discover -s tests -v
"""

import http.client
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "helper"))
import tvcast  # noqa: E402


class TimeFormats(unittest.TestCase):
    def test_seconds_to_dlna_time(self):
        self.assertEqual(tvcast.s_to_hms(3661.5), "1:01:01.500")
        self.assertEqual(tvcast.s_to_hms(-3), "0:00:00.000")

    def test_dlna_time_to_seconds(self):
        self.assertEqual(tvcast.hms_to_s("1:01:01.5"), 3661.5)
        self.assertEqual(tvcast.hms_to_s("90"), 90)
        self.assertEqual(tvcast.hms_to_s("NOT_IMPLEMENTED"), 0.0)

    def test_srt_times(self):
        self.assertEqual(tvcast.s_to_srt(3661.5), "01:01:01,500")
        self.assertEqual(tvcast.srt_to_s("00:01:02,500"), 62.5)

    def test_time_seek_header(self):
        self.assertEqual(tvcast.parse_npt("npt=600.5-"), 600.5)
        self.assertEqual(tvcast.parse_npt("npt=0:10:00.000-"), 600)
        self.assertIsNone(tvcast.parse_npt(None))


class Headers(unittest.TestCase):
    def test_only_useful_headers_are_forwarded(self):
        got = tvcast.clean_headers({"Cookie": "a=1", "Referer": "https://example.org/",
                                    "X-Other": "x", "Origin": ""})
        self.assertEqual(got, {"cookie": "a=1", "referer": "https://example.org/"})

    def test_ffmpeg_header_options(self):
        args = tvcast.header_args({"user-agent": "UA", "cookie": "a=1"})
        self.assertEqual(args, ["-user_agent", "UA", "-headers", "cookie: a=1\r\n"])
        self.assertEqual(tvcast.header_args({}), [])


class Addresses(unittest.TestCase):
    def test_only_web_addresses_are_accepted(self):
        self.assertEqual(tvcast.require_http("https://example.org/v.m3u8", "video"),
                         "https://example.org/v.m3u8")
        for bad in ("file:///etc/hosts", "/etc/hosts", "concat:a|b", "-version", "", None, 7):
            with self.assertRaises(ValueError, msg=repr(bad)):
                tvcast.require_http(bad, "video address")

    def test_subtitle_address(self):
        self.assertIsNone(tvcast.check_subtitle(None))
        tvcast.check_subtitle({"url": "https://example.org/en.vtt"})
        # With the text included, the address is only a label.
        tvcast.check_subtitle({"url": "blob:https://example.org/1", "text": "WEBVTT"})
        with self.assertRaises(ValueError):
            tvcast.check_subtitle({"url": "/etc/hosts"})
        with self.assertRaises(ValueError):
            tvcast.check_subtitle("https://example.org/en.vtt")


MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="fr",NAME="Francais",URI="audio/fr.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="en",NAME="English",DEFAULT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",LANGUAGE="en",NAME="English",URI="subs/en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",AUDIO="aud"
720p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="aud",SUBTITLES="subs"
1080p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=3840x2160,CODECS="vp09.00.50.08,mp4a.40.2",AUDIO="aud"
2160p/index.m3u8
"""


class Hls(unittest.TestCase):
    def test_parse_master_playlist(self):
        media, variants = tvcast.parse_hls_master(MASTER)
        self.assertEqual(len(media), 3)
        self.assertEqual([v["URI"] for v in variants],
                         ["720p/index.m3u8", "1080p/index.m3u8", "2160p/index.m3u8"])
        self.assertEqual(variants[1]["CODECS"], "avc1.640028,mp4a.40.2")

    def test_best_variant_is_the_largest_the_tv_can_decode(self):
        _, variants = tvcast.parse_hls_master(MASTER)
        self.assertEqual(tvcast.best_hls_variant(variants)["URI"], "1080p/index.m3u8")

    def test_falls_back_when_nothing_is_directly_playable(self):
        only_vp9 = [{"URI": "a", "RESOLUTION": "1280x720", "CODECS": "vp09.00"},
                    {"URI": "b", "RESOLUTION": "1920x1080", "CODECS": "vp09.00"}]
        self.assertEqual(tvcast.best_hls_variant(only_vp9)["URI"], "b")

    def test_preferred_language_wins(self):
        media, _ = tvcast.parse_hls_master(MASTER)
        audio = [m for m in media if m["TYPE"] == "AUDIO"]
        self.assertEqual(max(audio, key=tvcast.rendition_rank)["LANGUAGE"], "en")


SRT = """1
00:00:01,000 --> 00:00:03,000
First line

2
00:00:10,000 --> 00:00:12,500
Second line
still second

3
00:01:00,000 --> 00:01:02,000
Third line
"""


class SessionLogic(unittest.TestCase):
    def session(self, **kw):
        s = tvcast.Session("https://example.org/v.m3u8", {"Cookie": "a=1"}, "Title")
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_subtitles_are_unchanged_at_the_start(self):
        self.assertEqual(self.session(sub_srt=SRT).shifted_srt(), SRT)
        self.assertIsNone(self.session().shifted_srt())

    def test_subtitles_are_shifted_after_a_seek(self):
        out = self.session(sub_srt=SRT, offset=11.0).shifted_srt()
        self.assertNotIn("First line", out)          # already over
        self.assertTrue(out.startswith(
            "1\n00:00:00,000 --> 00:00:01,500\nSecond line\nstill second"))
        self.assertIn("2\n00:00:49,000 --> 00:00:51,000\nThird line", out)

    def test_size_offered_to_the_tv(self):
        self.assertIsNone(self.session().tv_size())            # live: no length
        size = self.session(duration=600.0, bandwidth=4_000_000).tv_size()
        self.assertEqual(size % 188, 0)                         # whole TS packets
        self.assertAlmostEqual(size, 4_000_000 / 8 * 600 * 1.08, delta=188)

    def test_h264_is_passed_through(self):
        cmd = self.session(vcodec="h264").ffmpeg_cmd()
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertNotIn("-ss", cmd)
        self.assertNotIn("-copyts", cmd)
        self.assertIn("cookie: a=1\r\n", cmd)
        self.assertEqual(cmd[-1], "pipe:1")

    def test_other_codecs_are_reencoded(self):
        cmd = self.session(vcodec="vp9").ffmpeg_cmd()
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "h264_videotoolbox")

    def test_jump_keeps_the_source_timestamps(self):
        # Without -copyts a separate audio track drifts out of sync after a seek.
        s = self.session(vcodec="h264", audio_url="https://example.org/a.m3u8")
        cmd = s.ffmpeg_cmd(120.0)
        self.assertEqual([cmd[i + 1] for i, a in enumerate(cmd) if a == "-ss"],
                         ["120.00", "120.00"])
        self.assertLess(cmd.index("-copyts"), cmd.index("-i"))
        self.assertIn("-start_at_zero", cmd)
        self.assertNotIn("-output_ts_offset", cmd)   # TV clock = media time

    def test_reloaded_stream_restarts_the_tv_clock(self):
        cmd = self.session(vcodec="h264", offset=100.0).ffmpeg_cmd(20.0)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "120.00")
        self.assertEqual(cmd[cmd.index("-output_ts_offset") + 1], "-100.000")
        self.assertIn("-copyts", cmd)

    def test_separate_audio_input(self):
        s = self.session(vcodec="h264", audio_url="https://example.org/a.m3u8")
        cmd = s.ffmpeg_cmd()
        self.assertEqual(cmd.count("-i"), 2)
        self.assertIn("1:a:0", cmd)


DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <device>
  <friendlyName>Living Room TV</friendlyName>
  <manufacturer>Samsung Electronics</manufacturer>
  <modelName>UN50NU7400</modelName>
  <serviceList>
   <service>
    <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
    <controlURL>/upnp/control/RenderingControl1</controlURL>
   </service>
   <service>
    <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
    <controlURL>/upnp/control/AVTransport1</controlURL>
   </service>
  </serviceList>
 </device>
</root>"""


class Renderer(unittest.TestCase):
    def tv(self):
        tv = tvcast.TV("http://192.0.2.10:9197/dmr", DESCRIPTION)
        tv.calls = []
        tv.avt = lambda action, args="": tv.calls.append((action, args)) or ""
        return tv

    def test_description_is_understood(self):
        tv = self.tv()
        self.assertEqual(tv.info(), {"ip": "192.0.2.10", "name": "Living Room TV",
                                     "model": "UN50NU7400"})
        self.assertTrue(tv.samsung)
        self.assertEqual(tv.avt_url, "http://192.0.2.10:9197/upnp/control/AVTransport1")
        self.assertEqual(tv.rc_url, "http://192.0.2.10:9197/upnp/control/RenderingControl1")
        self.assertTrue(tv.matches("living"))
        self.assertTrue(tv.matches("192.0.2.10"))
        self.assertFalse(tv.matches("bedroom"))

    def test_device_without_a_player_is_rejected(self):
        with self.assertRaises(ValueError):
            tvcast.TV("http://192.0.2.11/desc.xml", "<root><device/></root>")

    def test_seekable_video_with_subtitles(self):
        tv = self.tv()
        tv.load("http://192.0.2.2:8770/stream/x.ts", "A & B",
                "http://192.0.2.2:8770/sub/x.srt", duration=600, size=188000)
        self.assertEqual([c[0] for c in tv.calls], ["Stop", "SetAVTransportURI", "Play"])
        meta = tv.calls[1][1]
        self.assertIn("DLNA.ORG_OP=01", meta)
        self.assertIn("CaptionInfoEx", meta)
        self.assertIn('size="188000"', meta)
        self.assertIn("A &amp;amp; B", meta)        # escaped twice: XML inside XML

    def test_live_video_is_not_seekable(self):
        tv = self.tv()
        tv.load("http://192.0.2.2:8770/stream/x.ts", "Live")
        meta = tv.calls[1][1]
        self.assertIn("DLNA.ORG_OP=00", meta)
        self.assertNotIn("CaptionInfo", meta)


class ControlApi(unittest.TestCase):
    """The control API must only answer the extension on this machine."""

    EXTENSION = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"

    @classmethod
    def setUpClass(cls):
        cls._get_tv = tvcast.get_tv
        cls._log = tvcast.log
        tvcast.get_tv = lambda: None             # never search the network
        tvcast.log = lambda *a: None
        tvcast.STATE["session"] = None
        cls.server = tvcast.ActivityServer(("127.0.0.1", 0), tvcast.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        tvcast.get_tv, tvcast.log = cls._get_tv, cls._log

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            h = dict(headers or {})
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                h.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=data, headers=h)
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def test_local_tool_without_origin_is_allowed(self):
        status, headers, body = self.request("GET", "/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["state"], "NO_TV")
        self.assertEqual(data["version"], tvcast.__version__)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_extension_is_allowed(self):
        status, headers, _ = self.request("GET", "/status", headers={"Origin": self.EXTENSION})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.EXTENSION)

    def test_extension_preflight(self):
        status, headers, _ = self.request("OPTIONS", "/cast", headers={"Origin": self.EXTENSION})
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], self.EXTENSION)

    def test_web_pages_are_refused(self):
        for origin in ("https://example.org", "http://localhost:3000", "null"):
            for method, path in (("GET", "/status"), ("OPTIONS", "/cast")):
                status, headers, _ = self.request(method, path, headers={"Origin": origin})
                self.assertEqual(status, 403, (origin, method))
                self.assertNotIn("Access-Control-Allow-Origin", headers)
            status, _, _ = self.request("POST", "/control", {"action": "stop"},
                                        {"Origin": origin})
            self.assertEqual(status, 403, origin)

    def test_other_host_names_are_refused(self):
        # A page that points its own domain at 127.0.0.1 (DNS rebinding).
        status, _, _ = self.request("GET", "/status", headers={"Host": "example.org:8770"})
        self.assertEqual(status, 403)

    def test_posts_must_be_json(self):
        status, _, body = self.request("POST", "/control", {"action": "stop"},
                                       {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertFalse(json.loads(body)["ok"])

    def test_local_files_cannot_be_cast(self):
        for body in ({"url": "file:///etc/hosts"}, {"url": "/etc/hosts"},
                     {"pageUrl": "--exec=id"},
                     {"url": "https://example.org/v.m3u8", "audioUrl": "/etc/hosts"},
                     {"url": "https://example.org/v.m3u8", "subtitle": {"url": "/etc/hosts"}}):
            status, _, answer = self.request("POST", "/cast", body,
                                             {"Origin": self.EXTENSION})
            self.assertEqual(status, 400, body)
            self.assertIn("http(s) address", json.loads(answer)["error"])
        self.assertIsNone(tvcast.current())
        self.assertIsNone(tvcast.STATE["phase"])

    def test_unknown_stream_is_not_found(self):
        self.assertEqual(self.request("GET", "/stream/nope.ts")[0], 404)
        self.assertEqual(self.request("GET", "/sub/nope.srt")[0], 404)


if __name__ == "__main__":
    unittest.main()
