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
# Environment handling mirrors get_browser_config() in lib/proxy_wrapper.py:
#
#   Display:
#     - Local env with display: headed browser (visible window)
#     - Remote env (CLAUDE_CODE_REMOTE=true): starts Xvfb, headed via virtual display
#     - No display + no Xvfb: falls back to --headless=new
#
#   Proxy (CLAUDE_CODE_REMOTE=true only):
#     - Starts scripts/proxy-daemon.py, which runs the Python proxy auth wrapper
#     - Passes --proxy-server=http://127.0.0.1:<port> to Chrome
#     - Chrome natively bypasses the proxy for localhost/127.0.0.1, so dev
#       servers always work without extra configuration
#
# The session profile persists between starts, preserving cookies, localStorage,
# and authenticated sessions.

set -euo pipefail

DEFAULT_PORT=9222
SESSION_DIR="${PATCHRIGHT_SESSION_DIR:-$HOME/.patchright-session}"
PID_FILE="/tmp/patchright-chrome.pid"
XVFB_PID_FILE="/tmp/patchright-xvfb.pid"
PROXY_PID_FILE="/tmp/patchright-proxy.pid"
PROXY_PORT_FILE="/tmp/patchright-proxy-port"
LOG_FILE="/tmp/patchright-chrome.log"
PROXY_LOG_FILE="/tmp/patchright-proxy.log"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

# ── Display / Xvfb setup ──────────────────────────────────────────────────────
# Mirrors the logic in lib/proxy_wrapper.py ensure_virtual_display().

find_free_display() {
    for n in $(seq 99 110); do
        if [[ ! -f "/tmp/.X${n}-lock" ]]; then
            echo "$n"
            return
        fi
    done
    echo "99"
}

# Sets up a display for Chrome to use.
# Returns 0 (success/headed) or 1 (no display, caller should use --headless=new).
# Sets DISPLAY env var when starting Xvfb so Chrome inherits it.
setup_display() {
    # Already have a display - use it
    if [[ -n "${DISPLAY:-}" ]]; then
        echo "   Display: using existing $DISPLAY"
        return 0
    fi

    # Not in a remote environment - local without DISPLAY is unusual but let Chrome decide
    if [[ "${CLAUDE_CODE_REMOTE:-}" != "true" ]]; then
        return 0
    fi

    # Remote environment with no display - try Xvfb (same as proxy_wrapper.py)
    echo "   Remote environment detected, starting Xvfb virtual display..."

    if ! command -v Xvfb &>/dev/null; then
        echo "   Xvfb not found, attempting to install..."
        apt-get update -qq 2>/dev/null && apt-get install -y -qq xvfb 2>/dev/null || true
    fi

    if ! command -v Xvfb &>/dev/null; then
        echo "   Xvfb unavailable - falling back to headless mode"
        return 1
    fi

    local display_num
    display_num=$(find_free_display)
    local display=":${display_num}"

    Xvfb "$display" -screen 0 1920x1080x24 -ac -nolisten tcp &>/dev/null &
    local xvfb_pid=$!
    echo "$xvfb_pid" > "$XVFB_PID_FILE"

    # Give Xvfb a moment to initialize
    sleep 0.5

    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
        rm -f "$XVFB_PID_FILE"
        echo "   Xvfb failed to start - falling back to headless mode"
        return 1
    fi

    export DISPLAY="$display"
    echo "   Started Xvfb virtual display on $display (PID $xvfb_pid)"
    return 0
}

# ── Proxy wrapper setup ───────────────────────────────────────────────────────
# In remote environments starts scripts/proxy-daemon.py (a persistent Python
# process that runs the proxy_wrapper.py auth forwarding proxy).
# Chrome's built-in behaviour always bypasses the proxy for localhost/127.0.0.1,
# so dev server testing works without extra configuration.
#
# Sets _PROXY_SERVER to "http://127.0.0.1:<port>" on success, "" otherwise.

_PROXY_SERVER=""

setup_proxy() {
    _PROXY_SERVER=""

    if [[ "${CLAUDE_CODE_REMOTE:-}" != "true" ]]; then
        return 0
    fi

    local https_proxy="${HTTPS_PROXY:-${https_proxy:-}}"
    if [[ -z "$https_proxy" ]]; then
        return 0
    fi

    # Reuse existing proxy wrapper if still running
    if [[ -f "$PROXY_PID_FILE" ]]; then
        local existing_pid
        existing_pid=$(cat "$PROXY_PID_FILE")
        if kill -0 "$existing_pid" 2>/dev/null && [[ -f "$PROXY_PORT_FILE" ]]; then
            local port
            port=$(cat "$PROXY_PORT_FILE")
            _PROXY_SERVER="http://127.0.0.1:$port"
            echo "   Proxy:   reusing wrapper on port $port (PID $existing_pid)"
            return 0
        fi
        rm -f "$PROXY_PID_FILE" "$PROXY_PORT_FILE"
    fi

    echo "   Starting proxy auth wrapper..."
    rm -f "$PROXY_PORT_FILE"

    python3 "$SCRIPT_DIR/proxy-daemon.py" >> "$PROXY_LOG_FILE" 2>&1 &
    local proxy_pid=$!
    echo "$proxy_pid" > "$PROXY_PID_FILE"

    # Wait for proxy-daemon.py to write the port file (up to 3 seconds)
    local attempts=0
    while [[ $attempts -lt 30 ]]; do
        if [[ -f "$PROXY_PORT_FILE" ]]; then
            local port
            port=$(cat "$PROXY_PORT_FILE")
            _PROXY_SERVER="http://127.0.0.1:$port"
            echo "   Proxy:   wrapper on 127.0.0.1:$port (PID $proxy_pid)"
            return 0
        fi
        sleep 0.1
        (( attempts++ )) || true
    done

    echo "   Proxy wrapper failed to start (check $PROXY_LOG_FILE)"
    kill "$proxy_pid" 2>/dev/null || true
    rm -f "$PROXY_PID_FILE"
    return 1
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

    # Setup display - mirrors get_browser_config() headless logic
    local use_headless=false
    if ! setup_display; then
        use_headless=true
    fi

    # Setup proxy auth wrapper (remote env only)
    if ! setup_proxy; then
        echo "❌ Proxy wrapper failed to start — Chrome not launched (check $PROXY_LOG_FILE)"
        exit 1
    fi

    # Build Chrome args
    local chrome_args=(
        --remote-debugging-port="$port"
        --user-data-dir="$SESSION_DIR"
        --no-first-run
        --no-default-browser-check
        --disable-features=TranslateUI
        --no-sandbox
        --disable-setuid-sandbox
    )

    if [[ "$use_headless" == "true" ]]; then
        chrome_args+=(--headless=new)
        echo "   Mode:    headless (no display available)"
    else
        echo "   Mode:    headed${DISPLAY:+ (display: $DISPLAY)}"
    fi

    if [[ -n "$_PROXY_SERVER" ]]; then
        chrome_args+=(
            "--proxy-server=$_PROXY_SERVER"
            # The proxy wrapper uses CONNECT tunneling, not SSL interception, so
            # Chrome sees the real server cert. These flags are included to match
            # get_browser_config() and guard against edge cases where the upstream
            # proxy returns its own cert (e.g. corporate MITM proxies).
            --ignore-certificate-errors
            --ignore-certificate-errors-spki-list
        )
        echo "   Proxy:   $_PROXY_SERVER (localhost bypassed automatically by Chrome)"
    fi

    "$chrome" "${chrome_args[@]}" > "$LOG_FILE" 2>&1 &

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
            echo "   Or via helper:"
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
    # Stop Chrome
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            echo "✅ Chrome stopped (PID $pid)"
        else
            echo "ℹ️  Chrome was not running (stale PID file removed)"
        fi
        rm -f "$PID_FILE"
    else
        echo "ℹ️  No managed Chrome instance found (no PID file)"
    fi

    # Stop Xvfb if we started it
    if [[ -f "$XVFB_PID_FILE" ]]; then
        local xvfb_pid
        xvfb_pid=$(cat "$XVFB_PID_FILE")
        if kill -0 "$xvfb_pid" 2>/dev/null; then
            kill "$xvfb_pid"
            echo "✅ Xvfb stopped (PID $xvfb_pid)"
        fi
        rm -f "$XVFB_PID_FILE"
    fi

    # Stop proxy wrapper if we started it
    if [[ -f "$PROXY_PID_FILE" ]]; then
        local proxy_pid
        proxy_pid=$(cat "$PROXY_PID_FILE")
        if kill -0 "$proxy_pid" 2>/dev/null; then
            kill "$proxy_pid"
            echo "✅ Proxy wrapper stopped (PID $proxy_pid)"
        fi
        rm -f "$PROXY_PID_FILE" "$PROXY_PORT_FILE"
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
            echo "  Chrome  : running (PID $pid)"
        else
            echo "  Chrome  : stopped (stale PID file)"
        fi
    else
        echo "  Chrome  : unknown (no PID file)"
    fi

    # Xvfb
    if [[ -f "$XVFB_PID_FILE" ]]; then
        local xvfb_pid
        xvfb_pid=$(cat "$XVFB_PID_FILE")
        if kill -0 "$xvfb_pid" 2>/dev/null; then
            echo "  Xvfb    : running (PID $xvfb_pid, display ${DISPLAY:-:99})"
        else
            echo "  Xvfb    : stopped (stale PID file)"
        fi
    fi

    # Proxy wrapper
    if [[ -f "$PROXY_PID_FILE" ]]; then
        local proxy_pid
        proxy_pid=$(cat "$PROXY_PID_FILE")
        if kill -0 "$proxy_pid" 2>/dev/null && [[ -f "$PROXY_PORT_FILE" ]]; then
            local proxy_port
            proxy_port=$(cat "$PROXY_PORT_FILE")
            echo "  Proxy   : running (PID $proxy_pid, port $proxy_port)"
        else
            echo "  Proxy   : stopped (stale PID file)"
        fi
    fi

    # DevTools
    echo ""
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
    echo "  Chrome log  : $LOG_FILE"
    echo "  Proxy log   : $PROXY_LOG_FILE"
}

usage() {
    echo "Usage: $(basename "$0") {start|stop|status} [port]"
    echo ""
    echo "  start [port]   Start Chrome with remote debugging (default: $DEFAULT_PORT)"
    echo "  stop           Stop Chrome, Xvfb, and proxy wrapper"
    echo "  status [port]  Show status and connection details"
    echo ""
    echo "Environment variables:"
    echo "  PATCHRIGHT_SESSION_DIR   Chrome profile directory (default: ~/.patchright-session)"
    echo "  CLAUDE_CODE_REMOTE       Set to 'true' in Claude Code web - triggers Xvfb + proxy setup"
    echo "  HTTPS_PROXY              Upstream proxy URL (used when CLAUDE_CODE_REMOTE=true)"
    echo ""
    echo "Examples:"
    echo "  $(basename "$0") start          # Start on port $DEFAULT_PORT"
    echo "  $(basename "$0") start 9333     # Start on custom port"
    echo "  $(basename "$0") status         # Show status"
    echo "  $(basename "$0") stop           # Stop Chrome, Xvfb, and proxy"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

case "${1:-}" in
    start)  cmd_start "${2:-$DEFAULT_PORT}" ;;
    stop)   cmd_stop ;;
    status) cmd_status "${2:-$DEFAULT_PORT}" ;;
    *)      usage; exit 1 ;;
esac
