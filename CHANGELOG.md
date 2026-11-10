# Changelog

Notable changes to Send to TV. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-11-09

### Added
- The popup shows a loading card with the current step ("Checking the stream",
  "Preparing subtitles", "Starting the TV's player"…) from the click until the TV plays.
- Redesigned popup: player card with clear playing / paused / jumping states, the best
  match on the page first with other streams folded away, and clearer problem messages.
- Tests for the helper and the extension, and CI that runs them with linters.
- `SECURITY.md`, `CONTRIBUTING.md`, issue forms and this changelog.

### Changed
- A video always starts from the beginning on the TV; jump from there with the remote
  or the popup. Starting from the page's position (added in 1.1.0) is removed: when the
  TV refused the jump, its clock started at the wrong place.

### Security
- The helper's control API now refuses requests from web pages. Before, any site open
  in the browser could send it commands (start a video, change the volume).
- Only `http(s)` addresses are accepted for video, audio, subtitles and page links.
- Requests must be addressed to `127.0.0.1` / `localhost` and carry a JSON body.

### Fixed
- Sound and picture stay in sync after a jump. Streams with a separate audio track could
  play their sound up to one segment (several seconds) late after any seek.
- A cast no longer logs an error when the popup is closed before it finishes.

## [1.1.0] - 2026-11-09

### Added
- Seek with the TV remote: the TV's progress bar and arrow keys jump anywhere in the video.
- Continue where the page was: the TV starts from the page player's current position.

## [1.0.0] - 2026-11-09

### Added
- Chrome extension that detects HLS, DASH and MP4 streams and subtitle files on a page.
- On-demand macOS helper that remuxes the stream with ffmpeg and plays it on a DLNA TV.
- Subtitles on the TV through Samsung's CaptionInfo extension.
- Playback controls and keyboard shortcuts on the Mac; automatic TV discovery.

[1.2.0]: https://github.com/AshkanTavassoli/send-to-tv/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/AshkanTavassoli/send-to-tv/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/AshkanTavassoli/send-to-tv/releases/tag/v1.0.0
