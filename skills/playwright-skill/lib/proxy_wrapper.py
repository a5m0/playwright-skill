#!/usr/bin/env python3
"""
Proxy authentication wrapper for Claude Code web environments.

This wrapper handles proxy authentication that Chromium/Playwright cannot handle natively.
It intercepts CONNECT requests and adds the Proxy-Authorization header before forwarding
to the real proxy server.
"""

import socket
import threading
import base64
import os
import shutil
import subprocess
import time
from urllib.parse import urlparse


LOCAL_PROXY_PORT = 18080
_wrapper_thread = None
_wrapper_server = None
_xvfb_process = None


def is_claude_code_web_environment() -> bool:
    """
    Detect if running in Claude Code for Web environment.

    Uses the official CLAUDE_CODE_REMOTE environment variable which is set to "true"
    in browser-based Claude Code sessions.

    Returns:
        True if in Claude Code web environment with proxy
    """
    # Use official Claude Code web detection
    is_remote = os.environ.get('CLAUDE_CODE_REMOTE') == 'true'

    if not is_remote:
        return False

    # Verify proxy is configured (required for external sites)
    proxy_env = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    return proxy_env is not None


def get_proxy_config():
    """
    Get proxy configuration from environment.

    Returns:
        Dict with proxy details or None
    """
    proxy_url = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')

    if not proxy_url:
        return None

    parsed = urlparse(proxy_url)

    if not parsed.hostname or not parsed.port:
        return None

    return {
        'host': parsed.hostname,
        'port': parsed.port,
        'username': parsed.username,
        'password': parsed.password,
        'url': proxy_url
    }


def handle_client(client_socket, proxy_config):
    """Handle a single client connection."""
    try:
        # Read the client's CONNECT request
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = client_socket.recv(4096)
            if not chunk:
                break
            request += chunk

        # Parse the request
        request_str = request.decode('utf-8', errors='ignore')
        lines = request_str.split('\r\n')

        if not lines or not lines[0].startswith('CONNECT'):
            client_socket.close()
            return

        # Connect to the real proxy
        proxy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy_socket.settimeout(30)
        proxy_socket.connect((proxy_config['host'], proxy_config['port']))

        # Forward the CONNECT request with authentication
        modified_request = lines[0] + '\r\n'
        for line in lines[1:]:
            if line and not line.lower().startswith('proxy-authorization:'):
                modified_request += line + '\r\n'

        # Add authentication header
        if proxy_config['username'] and proxy_config['password']:
            credentials = f"{proxy_config['username']}:{proxy_config['password']}"
            auth_header = base64.b64encode(credentials.encode()).decode()
            modified_request = modified_request.rstrip('\r\n') + '\r\n'
            modified_request += f"Proxy-Authorization: Basic {auth_header}\r\n"
            modified_request += '\r\n'

        proxy_socket.sendall(modified_request.encode())

        # Read proxy's response
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = proxy_socket.recv(4096)
            if not chunk:
                break
            response += chunk

        # Forward response to client
        client_socket.sendall(response)

        # Check if tunnel was established
        if b"200" in response[:50]:
            # Tunnel established, start bidirectional forwarding
            def forward(source, destination):
                try:
                    while True:
                        data = source.recv(8192)
                        if not data:
                            break
                        destination.sendall(data)
                except:
                    pass
                finally:
                    try:
                        source.close()
                    except:
                        pass
                    try:
                        destination.close()
                    except:
                        pass

            # Start forwarding in both directions
            t1 = threading.Thread(target=forward, args=(client_socket, proxy_socket), daemon=True)
            t2 = threading.Thread(target=forward, args=(proxy_socket, client_socket), daemon=True)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        else:
            client_socket.close()
            proxy_socket.close()

    except Exception as e:
        try:
            client_socket.close()
        except:
            pass


def start_proxy_wrapper(proxy_config, verbose=True):
    """
    Start the proxy wrapper server.

    Args:
        proxy_config: Proxy configuration dict
        verbose: Print status messages

    Returns:
        Dict with local proxy info
    """
    global _wrapper_server

    if _wrapper_server:
        if verbose:
            print(f"🔄 Proxy wrapper already running on 127.0.0.1:{LOCAL_PROXY_PORT}")
        return {'server': f'http://127.0.0.1:{LOCAL_PROXY_PORT}'}

    # Check if port is already in use (by another process)
    test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    test_sock.settimeout(0.5)
    try:
        result = test_sock.connect_ex(('127.0.0.1', LOCAL_PROXY_PORT))
        test_sock.close()
        if result == 0:
            # Port is already in use, assume wrapper is running
            if verbose:
                print(f"🔄 Proxy wrapper already running on 127.0.0.1:{LOCAL_PROXY_PORT} (external process)")
            return {'server': f'http://127.0.0.1:{LOCAL_PROXY_PORT}'}
    except:
        pass

    _wrapper_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _wrapper_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _wrapper_server.bind(('127.0.0.1', LOCAL_PROXY_PORT))
    _wrapper_server.listen(10)

    if verbose:
        print(f"🔄 Starting proxy auth wrapper on 127.0.0.1:{LOCAL_PROXY_PORT}")
        print(f"   Forwarding to: {proxy_config['host']}:{proxy_config['port']}")

    def accept_connections():
        while True:
            try:
                client, addr = _wrapper_server.accept()
                thread = threading.Thread(target=handle_client, args=(client, proxy_config), daemon=True)
                thread.start()
            except:
                break

    global _wrapper_thread
    _wrapper_thread = threading.Thread(target=accept_connections, daemon=True)
    _wrapper_thread.start()

    # Give it a moment to start
    time.sleep(0.5)

    return {'server': f'http://127.0.0.1:{LOCAL_PROXY_PORT}'}


def _has_display() -> bool:
    """Check if a working display is available."""
    display = os.environ.get('DISPLAY')
    return bool(display)


def _find_free_display() -> int:
    """Find an unused X display number."""
    for display_num in range(99, 200):
        lock_file = f'/tmp/.X{display_num}-lock'
        sock_file = f'/tmp/.X11-unix/X{display_num}'
        if not os.path.exists(lock_file) and not os.path.exists(sock_file):
            return display_num
    return 99


def ensure_virtual_display(verbose=True) -> bool:
    """
    Start Xvfb virtual display if no display is available.

    Used to enable headed browser mode (headless=False) in environments
    without a physical display (e.g., Claude Code web, CI/CD).
    Headed mode is preferred for anti-bot evasion since some detection
    systems fingerprint headless browsers.

    Returns:
        True if a display is available (existing or newly started)
    """
    global _xvfb_process

    # Already have a display
    if _has_display():
        if verbose:
            print(f"   ✅ Using existing display: {os.environ['DISPLAY']}")
        return True

    # Already started Xvfb
    if _xvfb_process and _xvfb_process.poll() is None:
        if verbose:
            print(f"   ✅ Xvfb already running on {os.environ.get('DISPLAY', ':99')}")
        return True

    # Check if Xvfb is available, try to install if not
    if not shutil.which('Xvfb'):
        if verbose:
            print("   📦 Xvfb not found, attempting to install...")
        try:
            subprocess.run(
                ['apt-get', 'update', '-qq'],
                capture_output=True, timeout=30
            )
            subprocess.run(
                ['apt-get', 'install', '-y', '-qq', 'xvfb'],
                capture_output=True, timeout=60
            )
        except Exception:
            pass

        if not shutil.which('Xvfb'):
            if verbose:
                print("   ⚠️  Xvfb not available, falling back to headless mode")
            return False
        if verbose:
            print("   ✅ Xvfb installed")

    display_num = _find_free_display()
    display = f':{display_num}'

    try:
        _xvfb_process = subprocess.Popen(
            ['Xvfb', display, '-screen', '0', '1920x1080x24', '-ac', '-nolisten', 'tcp'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        # Give Xvfb a moment to start
        time.sleep(0.5)

        if _xvfb_process.poll() is not None:
            if verbose:
                print("   ⚠️  Xvfb failed to start, falling back to headless mode")
            _xvfb_process = None
            return False

        os.environ['DISPLAY'] = display
        if verbose:
            print(f"   ✅ Started Xvfb virtual display on {display}")
        return True

    except Exception as e:
        if verbose:
            print(f"   ⚠️  Failed to start Xvfb: {e}, falling back to headless mode")
        _xvfb_process = None
        return False


def stop_virtual_display():
    """Stop the Xvfb virtual display if we started one."""
    global _xvfb_process
    if _xvfb_process:
        try:
            _xvfb_process.terminate()
            _xvfb_process.wait(timeout=5)
        except Exception:
            try:
                _xvfb_process.kill()
            except Exception:
                pass
        _xvfb_process = None


def get_browser_config(headless=None, verbose=True, use_chrome=True):
    """
    Get browser configuration for current environment.

    Automatically detects Claude Code web environment and configures:
    - Proxy wrapper for authentication
    - Headless mode
    - Certificate error handling
    - Chrome preference for better stealth

    Args:
        headless: Override headless setting (None = auto-detect)
        verbose: Print configuration messages
        use_chrome: Prefer Chrome over Chromium for better stealth (default: True)

    Returns:
        Dict with launch options and proxy config
    """
    config = {
        'launch_options': {
            'args': ['--no-sandbox', '--disable-setuid-sandbox']
        },
        'context_options': {},
        'proxy_wrapper_used': False
    }

    # Prefer Chrome over Chromium for better stealth and bot detection avoidance
    if use_chrome:
        config['launch_options']['channel'] = 'chrome'
        if verbose:
            print("   🎯 Using Chrome for improved stealth (falls back to Chromium if unavailable)")

    # Check if in Claude Code web environment
    if is_claude_code_web_environment():
        proxy_config = get_proxy_config()

        if proxy_config:
            # Start proxy wrapper
            wrapper_info = start_proxy_wrapper(proxy_config, verbose=verbose)

            # Configure browser to use wrapper
            config['launch_options']['proxy'] = {'server': wrapper_info['server']}
            config['launch_options']['args'].extend([
                '--ignore-certificate-errors',
                '--ignore-certificate-errors-spki-list',
            ])
            config['context_options']['ignore_https_errors'] = True
            config['proxy_wrapper_used'] = True

            if verbose:
                print("   ✅ Proxy authentication configured")

        # Determine headless mode for web environment
        if headless is False:
            # User explicitly wants headed mode - use Xvfb for virtual display
            # Headed mode is better for anti-bot evasion (some detectors fingerprint headless)
            has_display = ensure_virtual_display(verbose=verbose)
            config['launch_options']['headless'] = not has_display
            config['xvfb_used'] = has_display
            if has_display:
                # Explicitly pass DISPLAY to Chrome via env - Playwright's process
                # spawning may not inherit os.environ changes made after startup
                config['launch_options']['env'] = {**os.environ}
                if verbose:
                    print("   🎯 Headed mode via Xvfb (better anti-bot evasion)")
        elif headless is None:
            config['launch_options']['headless'] = True
            config['xvfb_used'] = False
            if verbose:
                print("   ✅ Headless mode enabled (web environment default)")
        else:
            config['launch_options']['headless'] = True
            config['xvfb_used'] = False
            if verbose:
                print("   ✅ Headless mode enabled")
    else:
        config['xvfb_used'] = False
        # Not in Claude Code web - use default settings
        if headless is not None:
            config['launch_options']['headless'] = headless
        else:
            # Default to visible browser in local environments
            config['launch_options']['headless'] = False

    return config


def stop_proxy_wrapper():
    """Stop the proxy wrapper server."""
    global _wrapper_server, _wrapper_thread

    if _wrapper_server:
        try:
            _wrapper_server.close()
        except:
            pass
        _wrapper_server = None
        _wrapper_thread = None


__all__ = [
    'is_claude_code_web_environment',
    'get_proxy_config',
    'get_browser_config',
    'start_proxy_wrapper',
    'stop_proxy_wrapper',
    'ensure_virtual_display',
    'stop_virtual_display',
]
