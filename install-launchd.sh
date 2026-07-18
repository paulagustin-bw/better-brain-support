#!/bin/zsh
set -euo pipefail

REPO_ROOT=${0:A:h}
PLIST_ID="com.betterbrain.support-bot"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/BetterBrainSupport"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST_ID.plist"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python3"
ENV_FILE="$REPO_ROOT/.env"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "No venv found at $VENV_PYTHON -- run 'python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt' first." >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "No .env found at $ENV_FILE -- create it with SLACK_SIGNING_SECRET, BETTERSUPPORT_SLACK_BOT_TOKEN, and BETTERSUPPORT_LLM_OPENAI_API_KEY first." >&2
  exit 1
fi

# Source .env at launch time (not baked into the plist) so rotating a credential
# is just editing the file -- no plist regeneration/reinstall needed.
COMMAND="export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:\$PATH; set -a; source '$ENV_FILE'; set +a; cd '$REPO_ROOT'; exec '$VENV_PYTHON' local-server.py"

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
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>$COMMAND</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$REPO_ROOT</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/support-bot.log</string>

  <key>StandardErrorPath</key>
  <string>$LOG_DIR/support-bot.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$PLIST_ID" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/$PLIST_ID"
launchctl kickstart -k "gui/$(id -u)/$PLIST_ID"

cat <<MSG
Installed $PLIST_ID
Plist: $PLIST_PATH
Log: $LOG_DIR/support-bot.log

Starts on login and restarts automatically if it crashes (KeepAlive, unless it
exits cleanly on its own -- SuccessfulExit=false means "only restart on non-zero
exit," so a deliberate stop via launchctl bootout won't fight you).

To check it's alive:  launchctl list | grep $PLIST_ID
To stop it:           launchctl bootout gui/\$(id -u)/$PLIST_ID
To restart it:        launchctl kickstart -k gui/\$(id -u)/$PLIST_ID
To tail logs:         tail -f $LOG_DIR/support-bot.log

This is a LaunchAgent, so it runs in your user session -- the mini needs to
stay logged in (auto-login is common for a dedicated mini) for this to survive
a reboot.

IMPORTANT -- this does NOT solve how Slack reaches this server. See the public
endpoint note below.
MSG
