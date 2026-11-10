#!/bin/bash
# Removes the Send to TV helper. Remove the Chrome extension in chrome://extensions.
LABEL="com.sendtotv.helper"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pkill -f "Send to TV/helper/tvcast.py" 2>/dev/null || true
rm -rf "$HOME/Applications/Send to TV" "$HOME/Library/Application Support/TVCast"
rm -f "$HOME/Library/Logs/SendToTV.log"
echo "Send to TV helper removed. Also remove the extension in chrome://extensions."
