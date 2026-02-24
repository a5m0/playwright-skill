"""
Persistent Chrome session helpers for Patchright.

Lets automation scripts connect to an already-running Chrome instance that was
started with --remote-debugging-port, rather than launching a fresh browser
for every script. Useful for:

  - Interactive debugging (open DevTools in the live browser while the script runs)
  - Reusing authenticated sessions without logging in each time
  - Keeping browser state (cookies, localStorage) across multiple scripts
  - Connecting to a browser the user is actively viewing

Quick start:

    # 1. Start the persistent instance once
    scripts/chrome-instance.sh start

    # 2. Connect in your automation code
    from lib.persistent_session import connect_to_persistent_session

    async with async_playwright() as p:
        browser = await connect_to_persistent_session(p)
        context = browser.contexts[0]  # reuse existing context/session
        page = await context.new_page()
        await page.goto('https://example.com')
        # Calling browser.close() does NOT kill Chrome - it stays running

    # 3. Stop when done
    scripts/chrome-instance.sh stop
"""

import json
import socket
import urllib.request
from typing import Optional, Dict, Any

DEFAULT_PORT = 9222
DEFAULT_HOST = "localhost"


def is_persistent_session_running(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> bool:
    """
    Check if a persistent Chrome session is running and its DevTools port is open.

    Args:
        port: Remote debugging port to check (default: 9222)
        host: Host where Chrome is running (default: localhost)

    Returns:
        True if Chrome is reachable on the given port.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def get_persistent_session_info(
    port: int = DEFAULT_PORT, host: str = DEFAULT_HOST
) -> Optional[Dict[str, Any]]:
    """
    Fetch metadata from the running Chrome DevTools endpoint.

    Args:
        port: Remote debugging port (default: 9222)
        host: Host where Chrome is running (default: localhost)

    Returns:
        Dict with keys like 'Browser', 'webSocketDebuggerUrl', 'V8-Version', etc.
        Returns None if Chrome is not running or not responding.

    Example:
        info = get_persistent_session_info()
        if info:
            print('Browser:', info['Browser'])
            print('WS URL:', info.get('webSocketDebuggerUrl'))
    """
    try:
        url = f"http://{host}:{port}/json/version"
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


async def connect_to_persistent_session(
    playwright,
    port: int = DEFAULT_PORT,
    host: str = DEFAULT_HOST,
):
    """
    Connect to a running Chrome instance via CDP (Chrome DevTools Protocol).

    The Chrome instance must already be running with --remote-debugging-port.
    Use scripts/chrome-instance.sh to manage the lifecycle.

    Key difference from a normal launch:
        - Does NOT start a new Chrome process
        - browser.close() disconnects Patchright but leaves Chrome running
        - Shares the same session (cookies, storage, open tabs) as the live browser

    Args:
        playwright: The async_playwright instance (the `p` in `async with async_playwright() as p`)
        port: Remote debugging port Chrome was started with (default: 9222)
        host: Host where Chrome is running (default: localhost)

    Returns:
        Browser object connected to the running Chrome instance.

    Raises:
        ConnectionError: If no Chrome instance is responding on the given port.

    Example:
        async with async_playwright() as p:
            browser = await connect_to_persistent_session(p)

            # Option A: reuse an existing page/context (preserves session)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()

            # Option B: open a fresh page in a new context
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto('https://example.com')
            print(await page.title())

            await browser.close()  # disconnects only - Chrome keeps running
    """
    if not is_persistent_session_running(port, host):
        raise ConnectionError(
            f"No Chrome instance found on {host}:{port}.\n"
            f"Start one with:  scripts/chrome-instance.sh start {port}\n"
            f"Or manually:     chrome --remote-debugging-port={port} --user-data-dir=/tmp/chrome-session"
        )

    cdp_url = f"http://{host}:{port}"
    browser = await playwright.chromium.connect_over_cdp(cdp_url)
    return browser
