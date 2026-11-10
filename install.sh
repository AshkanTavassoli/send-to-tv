#!/bin/bash
# Send to TV installer for macOS.
# Copies the helper + extension to ~/Applications/Send to TV and registers an
# on-demand launchd agent: nothing runs until the Chrome extension needs it.
set -euo pipefail

LABEL="com.sendtotv.helper"
PORT="${TVCAST_PORT:-8770}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Applications/Send to TV"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/SendToTV.log"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || die "This installer is for macOS."

for d in /opt/homebrew/bin /usr/local/bin; do
  [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH";; esac
done

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 not found. Install it with: brew install python  (or xcode-select --install)"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "Python 3.8+ is required."

if ! command -v ffmpeg >/dev/null; then
  if command -v brew >/dev/null; then
    say "Installing ffmpeg with Homebrew…"; brew install ffmpeg
  else
    die "ffmpeg not found. Install Homebrew (https://brew.sh) and run: brew install ffmpeg"
  fi
fi
if ! command -v yt-dlp >/dev/null; then
  if command -v brew >/dev/null; then
    say "Installing yt-dlp (optional, for 'Try the page link')…"; brew install yt-dlp || true
  else
    echo "Note: yt-dlp not found; the 'Try the page link' button won't work without it."
  fi
fi

say "Copying files to $DEST"
mkdir -p "$DEST"
rm -rf "$DEST/helper" "$DEST/extension"
cp -R "$SRC/helper" "$SRC/extension" "$DEST/"
cp "$SRC/README.md" "$SRC/uninstall.sh" "$DEST/" 2>/dev/null || true
rm -rf "$DEST/helper/__pycache__"

say "Registering the on-demand helper (port $PORT)"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DEST/helper/tvcast.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH</string>
    <key>TVCAST_PORT</key><string>$PORT</string>
  </dict>
  <key>Sockets</key>
  <dict>
    <key>Listener</key>
    <dict>
      <key>SockServiceName</key><string>$PORT</string>
      <key>SockNodeName</key><string>0.0.0.0</string>
      <key>SockFamily</key><string>IPv4</string>
      <key>SockType</key><string>stream</string>
    </dict>
  </dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PL
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -f "$DEST/helper/tvcast.py" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

say "Looking for your TV…"
sleep 1
STATUS="$(curl -s -m 20 "http://127.0.0.1:$PORT/status" || true)"
TVNAME="$(printf '%s' "$STATUS" | "$PY" -c 'import json,sys
try:
  d=json.load(sys.stdin); t=d.get("tv") or {}
  print(t.get("name","") if d.get("state") not in ("NO_TV","UNREACHABLE") else "")
except Exception: print("")' )"
if [ -n "$TVNAME" ]; then
  echo "Found: $TVNAME"
else
  echo "No TV found yet. Turn the TV on (same Wi-Fi), then open the extension."
  echo "If macOS asks to allow \"Python\" to find devices on your local network, click Allow"
  echo "(System Settings → Privacy & Security → Local Network)."
fi

cat <<MSG

Done. Last step — load the Chrome extension (one time):
  1. Open chrome://extensions
  2. Turn on "Developer mode" (top right)
  3. Click "Load unpacked" and choose:
       $DEST/extension
  4. Pin "Send to TV" in the toolbar (puzzle-piece icon)

After updating (git pull && ./install.sh), press the reload arrow on the
extension in chrome://extensions.
MSG
open -R "$DEST/extension" 2>/dev/null || true
