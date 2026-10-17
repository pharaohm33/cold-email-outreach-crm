#!/bin/bash
# Starts the helper by itself when you log in to this Mac, and restarts it if it ever stops.
#   bash tools/install_autostart.sh            install (asks nothing; run it only when you want this)
#   bash tools/install_autostart.sh --open-crm also open the CRM page at login (optional: the loop runs in the helper now, the page is only a viewer/controller)
#   bash tools/install_autostart.sh --print    show what would be written, change nothing
#   bash tools/install_autostart.sh --remove   uninstall
# It adds two small files in ~/Library/LaunchAgents (com.clipper.helper.plist, and com.clipper.crm.plist with --open-crm). Nothing else on the Mac is touched.
set -e
CRM="$(cd "$(dirname "$0")/.." && pwd)"
PY="$CRM/tools/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
AG="$HOME/Library/LaunchAgents"; LOGS="$HOME/Library/Logs/clipper"; UID_=$(id -u)
H="$AG/com.clipper.helper.plist"; C="$AG/com.clipper.crm.plist"
URL="https://pharaohm33.github.io/cold-email-outreach-crm/"

helper_plist() { cat <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.clipper.helper</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$CRM/tools/local_runner.py</string></array>
  <key>WorkingDirectory</key><string>$CRM</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>20</integer>
  <key>StandardOutPath</key><string>$LOGS/helper.log</string>
  <key>StandardErrorPath</key><string>$LOGS/helper.log</string>
</dict></plist>
PL
}
crm_plist() { cat <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.clipper.crm</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>$URL</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
PL
}

case "$1" in
  --print) echo "== $H"; helper_plist; echo; echo "== $C (only with --open-crm)"; crm_plist; exit 0 ;;
  --remove)
    launchctl bootout "gui/$UID_/com.clipper.helper" 2>/dev/null || true
    launchctl bootout "gui/$UID_/com.clipper.crm" 2>/dev/null || true
    rm -f "$H" "$C"; echo "Removed. The helper no longer starts at login."; exit 0 ;;
esac

mkdir -p "$AG" "$LOGS"
helper_plist > "$H"; plutil -lint "$H" >/dev/null
[ "$1" = "--open-crm" ] && { crm_plist > "$C"; plutil -lint "$C" >/dev/null; }
# a helper started by hand would hold the port: stop it, launchd starts the managed one
PID=$(lsof -ti :8765 2>/dev/null || true); [ -n "$PID" ] && kill $PID 2>/dev/null && sleep 1
launchctl bootout "gui/$UID_/com.clipper.helper" 2>/dev/null || true
launchctl bootstrap "gui/$UID_" "$H"
[ "$1" = "--open-crm" ] && { launchctl bootout "gui/$UID_/com.clipper.crm" 2>/dev/null || true; launchctl bootstrap "gui/$UID_" "$C"; }
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -m 2 localhost:8765/status >/dev/null 2>&1 && break; sleep 1; done
curl -s -m 2 localhost:8765/status >/dev/null 2>&1 && echo "Helper is running and will start at every login." || echo "WARNING: the helper did not answer. See $LOGS/helper.log"
echo "Remove it any time with: bash tools/install_autostart.sh --remove"
