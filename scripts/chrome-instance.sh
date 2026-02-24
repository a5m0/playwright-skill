#!/usr/bin/env bash
# Manage a persistent Chrome instance with remote debugging enabled.
# Allows patchright to connect via connect_over_cdp() for debugging and testing
# without needing a separate MCP server.
#
# Usage:
#   scripts/chrome-instance.sh start [port]   # Start Chrome (default port: 9222)
#   scripts/chrome-instance.sh stop            # Stop the managed instance
#   scripts/chrome-instance.sh status [port]   # Show status and connection details
#
# The session profile persists between starts, preserving cookies, localStorage,
# and authenticated sessions.

set -euo pipefail

DEFAULT_PORT=9222
SESSION_DIR="${PATCHRIGHT_SESSION_DIR:-$HOME/.patchright-session}"
PID_FILE="/tmp/patchright-chrome.pid"
LOG_FILE="/tmp/patchright-chrome.log"

# ── Chrome binary discovery ───────────────────────────────────────────────────

find_chrome() {
    # Prefer patchright-managed Chrome for best compatibility
    local patchright_chrome
    patchright_chrome=$(python3 -c "
try:
    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        path = p.chromium.executable_path
        print(path)
except Exception:
    pass
" 2>/dev/null || true)

    if [[ -n "$patchright_chrome" && -x "$patchright_chrome" ]]; then
        echo "$patchright_chrome"
        return
    fi

    # Fall back to system Chrome/Chromium
    for cmd in google-chrome google-chrome-stable chromium chromium-browser; do
        if command -v "$cmd" &>/dev/null; then
            command -v "$cmd"
            return
        fi
    done

    echo ""
}

# ── Helpers ───────────────────────────────────────────────────────────────────

devtools_responding() {
    local port="${1:-$DEFAULT_PORT}"
    curl -sf --max-time 2 "http://localhost:${port}/json/version" &>/dev/null
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd_start() {
    local port="${1:-$DEFAULT_PORT}"

    # Already running?
    if [[ -f "$PID_FILE" ]]; then
        local existing_pid
        existing_pid=$(cat "$PID_FILE")
        if kill -0 "$existing_pid" 2>/dev/null && devtools_responding "$port"; then
            echo "✅ Chrome already running (PID $existing_pid) on port $port"
            echo "   DevTools: http://localhost:$port"
            echo "   Connect:  await p.chromium.connect_over_cdp('http://localhost:$port')"
            return 0
        fi
        rm -f "$PID_FILE"
    fi

    local chrome
    chrome=$(find_chrome)
    if [[ -z "$chrome" ]]; then
        echo "❌ Chrome not found."
        echo "   Install with: uv run patchright install chrome"
        exit 1
    fi

    mkdir -p "$SESSION_DIR"

    echo "🚀 Starting Chrome..."
    echo "   Binary:  $chrome"
    echo "   Profile: $SESSION_DIR"
    echo "   Port:    $port"

    "$chrome" \
        --remote-debugging-port="$port" \
        --user-data-dir="$SESSION_DIR" \
        --no-first-run \
        --no-default-browser-check \
        --disable-features=TranslateUI \
        > "$LOG_FILE" 2>&1 &

    local pid=$!
    echo "$pid" > "$PID_FILE"

    # Wait for DevTools endpoint to become available (up to 10 seconds)
    local attempts=0
    while [[ $attempts -lt 20 ]]; do
        if devtools_responding "$port"; then
            echo "✅ Chrome started (PID $pid)"
            echo "   DevTools: http://localhost:$port"
            echo "   Connect:  await p.chromium.connect_over_cdp('http://localhost:$port')"
            echo ""
            echo "   Or use the helper:"
            echo "   from lib.persistent_session import connect_to_persistent_session"
            echo "   browser = await connect_to_persistent_session(p)"
            return 0
        fi
        sleep 0.5
        (( attempts++ )) || true
    done

    echo "⚠️  Chrome started (PID $pid) but DevTools not yet responding."
    echo "   Check log: $LOG_FILE"
}

cmd_stop() {
    if [[ ! -f "$PID_FILE" ]]; then
        echo "ℹ️  No managed Chrome instance found (no PID file)"
        return 0
    fi

    local pid
    pid=$(cat "$PID_FILE")

    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid"
        rm -f "$PID_FILE"
        echo "✅ Chrome stopped (PID $pid)"
    else
        rm -f "$PID_FILE"
        echo "ℹ️  Chrome was not running (stale PID file removed)"
    fi
}

cmd_status() {
    local port="${1:-$DEFAULT_PORT}"

    echo "=== Persistent Chrome Status ==="
    echo ""

    # Process
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "  Process : running (PID $pid)"
        else
            echo "  Process : stopped (stale PID file)"
        fi
    else
        echo "  Process : unknown (no PID file)"
    fi

    # DevTools
    if devtools_responding "$port"; then
        echo "  DevTools: ✅ responding on http://localhost:$port"
        local info
        info=$(curl -sf "http://localhost:$port/json/version")
        local browser_ver
        browser_ver=$(echo "$info" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Browser','?'))" 2>/dev/null || echo "?")
        echo "  Browser : $browser_ver"
        echo ""
        echo "  Connect with patchright:"
        echo "    browser = await p.chromium.connect_over_cdp('http://localhost:$port')"
        echo ""
        echo "  Or via helper:"
        echo "    from lib.persistent_session import connect_to_persistent_session"
        echo "    browser = await connect_to_persistent_session(p, port=$port)"
    else
        echo "  DevTools: ❌ not responding on port $port"
        echo ""
        echo "  Start with: scripts/chrome-instance.sh start"
    fi

    echo ""
    echo "  Session dir : $SESSION_DIR"
    echo "  Log file    : $LOG_FILE"
}

usage() {
    echo "Usage: $(basename "$0") {start|stop|status} [port]"
    echo ""
    echo "  start [port]   Start Chrome with remote debugging (default: $DEFAULT_PORT)"
    echo "  stop           Stop the managed Chrome instance"
    echo "  status [port]  Show status and connection details"
    echo ""
    echo "Environment variables:"
    echo "  PATCHRIGHT_SESSION_DIR   Chrome profile directory (default: ~/.patchright-session)"
    echo ""
    echo "Examples:"
    echo "  $(basename "$0") start          # Start on port $DEFAULT_PORT"
    echo "  $(basename "$0") start 9333     # Start on custom port"
    echo "  $(basename "$0") status         # Show status"
    echo "  $(basename "$0") stop           # Stop Chrome"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

case "${1:-}" in
    start)  cmd_start "${2:-$DEFAULT_PORT}" ;;
    stop)   cmd_stop ;;
    status) cmd_status "${2:-$DEFAULT_PORT}" ;;
    *)      usage; exit 1 ;;
esac
