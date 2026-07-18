#!/bin/zsh
# Installs a persistent LaunchAgent for `cloudflared tunnel run`, so the tunnel
# survives reboots the same way install-launchd.sh does for the Flask server
# itself. Run this AFTER the one-time `cloudflared tunnel create` / `route dns`
# setup (see the README section this script's usage message points to).
set -euo pipefail

TUNNEL_NAME="${CLOUDFLARE_TUNNEL_NAME:-betterbrain-support}"
PLIST_ID="com.betterbrain.support-bot-tunnel"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/BetterBrainSupport"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST_ID.plist"
CLOUDFLARED_BIN="$(command -v cloudflared || echo /opt/homebrew/bin/cloudflared)"

if [[ ! -x "$CLOUDFLARED_BIN" ]]; then
  echo "cloudflared not found -- run 'brew install cloudflared' first." >&2
  exit 1
fi
if [[ ! -f "$HOME/.cloudflared/config.yml" ]]; then
  echo "No ~/.cloudflared/config.yml found -- complete the one-time 'cloudflared tunnel create'" >&2
  echo "and 'cloudflared tunnel route dns' setup first (see CLOUDFLARE_SETUP.md)." >&2
  exit 1
fi

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$PLIST_ID</string>

  <key>ProgramArguments</key>
  <array>
    <string>$CLOUDFLARED_BIN</string>
    <string>tunnel</string>
    <string>run</string>
    <string>$TUNNEL_NAME</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/cloudflared-tunnel.log</string>

  <key>StandardErrorPath</key>
  <string>$LOG_DIR/cloudflared-tunnel.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$PLIST_ID" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/$PLIST_ID"
launchctl kickstart -k "gui/$(id -u)/$PLIST_ID"

cat <<MSG
Installed $PLIST_ID (tunnel: $TUNNEL_NAME)
Plist: $PLIST_PATH
Log: $LOG_DIR/cloudflared-tunnel.log

To check it's alive:  launchctl list | grep $PLIST_ID
To tail logs:          tail -f $LOG_DIR/cloudflared-tunnel.log
MSG
