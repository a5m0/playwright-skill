#!/usr/bin/env python3
"""
Proxy authentication wrapper daemon for persistent Chrome instances.

Starts the proxy_wrapper.py forwarding proxy and keeps it alive until
terminated. Used by chrome-instance.sh when CLAUDE_CODE_REMOTE=true.

Writes the local proxy port to /tmp/patchright-proxy-port once ready,
then blocks until SIGTERM or SIGINT.
"""

import os
import signal
import sys
import time
from pathlib import Path

# Resolve skill dir relative to this script (scripts/ -> skills/playwright-skill/)
SCRIPT_DIR = Path(__file__).parent.resolve()
SKILL_DIR = SCRIPT_DIR.parent / 'skills' / 'playwright-skill'
sys.path.insert(0, str(SKILL_DIR))

PORT_FILE = '/tmp/patchright-proxy-port'

try:
    from lib.proxy_wrapper import get_proxy_config, start_proxy_wrapper, stop_proxy_wrapper
except ImportError as e:
    print(f"Failed to import proxy_wrapper: {e}", file=sys.stderr)
    sys.exit(1)


def main():
    proxy_config = get_proxy_config()
    if not proxy_config:
        print("No proxy configuration found (HTTPS_PROXY not set)", file=sys.stderr)
        sys.exit(1)

    wrapper_info = start_proxy_wrapper(proxy_config, verbose=False)
    port = wrapper_info['server'].rsplit(':', 1)[-1]

    # Write port so chrome-instance.sh can read it and pass --proxy-server
    with open(PORT_FILE, 'w') as f:
        f.write(port)

    print(f"Proxy wrapper listening on 127.0.0.1:{port}", flush=True)

    def shutdown(signum, frame):
        stop_proxy_wrapper()
        try:
            os.unlink(PORT_FILE)
        except FileNotFoundError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Block until terminated
    while True:
        time.sleep(1)


if __name__ == '__main__':
    main()
