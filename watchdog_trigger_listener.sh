#!/usr/bin/env bash
# watchdog_trigger_listener.sh — restart discord_trigger_listener.py if it is not running.
# Intended to be called from crontab every 10 minutes.
#
# Setup:
#   1. Create a .env file next to this script with DISCORD_BOT_TOKEN=<token>
#   2. chmod +x watchdog_trigger_listener.sh
#   3. Add to crontab (crontab -e):
#        */10 * * * * /Users/mckerracher.joshua/Code/Mine/agent-development/watchdog_trigger_listener.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="${SCRIPT_DIR}/logs/trigger_listener.pid"
LOGFILE="${SCRIPT_DIR}/logs/watchdog.log"
LISTENER="${SCRIPT_DIR}/discord_trigger_listener.py"
ENV_FILE="${SCRIPT_DIR}/.env"

# Ensure logs directory exists
mkdir -p "${SCRIPT_DIR}/logs"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] $*" >> "$LOGFILE"
}

# Source environment variables (DISCORD_BOT_TOKEN, etc.)
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
else
    log "WARNING: ${ENV_FILE} not found. DISCORD_BOT_TOKEN may not be set."
fi

# Check if process is alive via PID file
is_running() {
    if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        else
            log "Stale PID file (pid=$pid). Removing."
            rm -f "$PIDFILE"
        fi
    fi
    return 1
}

if is_running; then
    log "Listener is running (pid=$(cat "$PIDFILE")). Nothing to do."
    exit 0
fi

log "Listener is NOT running. Starting..."

# Start the listener in the background, redirect stdout to log
nohup python3 "$LISTENER" \
    --repo "/Users/mckerracher.joshua/Code/mcs-products-mono-ui" \
    --runner-script "/Users/mckerracher.joshua/Code/mcs-products-mono-ui/.claude/agent-runner/run_headless.py" \
    >> "${SCRIPT_DIR}/logs/trigger_listener_stdout.log" 2>&1 &

NEW_PID=$!
echo "$NEW_PID" > "$PIDFILE"
log "Started listener with PID=${NEW_PID}."
