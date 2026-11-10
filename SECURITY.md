# Security

## Reporting a problem

Please report security problems privately through
[GitHub's "Report a vulnerability" form](https://github.com/AshkanTavassoli/send-to-tv/security/advisories/new),
not in a public issue. You should get an answer within a week.

Only the latest release is supported.

## How the pieces are protected

- **The helper's control API** (`/status`, `/cast`, `/control`, `/subtitle`) answers only
  requests that come from the same Mac, are addressed to `127.0.0.1` / `localhost`, and
  either have no `Origin` (a local tool) or a browser-extension origin. Requests from web
  pages are refused, so a site open in the browser cannot drive the TV. Bodies must be JSON.
- **Addresses** given to the helper must be `http(s)`; local files and other protocols are
  refused before anything is passed to ffmpeg or yt-dlp.
- **The stream and subtitle endpoints** (`/stream/<id>.ts`, `/sub/<id>.srt`) must be
  reachable by the TV, so they are open to the local network. They exist only while
  something is playing, and the address contains a random id that changes on every cast.
  Anyone on your network who learns that address can watch the same stream; use the tool
  on networks you trust.
- **The extension** sees request headers (including cookies) for video and subtitle
  requests so the helper can fetch the same stream. They are kept in memory for the
  browser session, sent only to the helper on `127.0.0.1`, and never written to disk.
- The helper runs as your user, never as root. It connects only to the site of the video
  you cast and to the TV, and searches the local network for the TV (SSDP).
