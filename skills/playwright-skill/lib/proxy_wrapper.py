#!/usr/bin/env python3
"""
Proxy authentication wrapper for Claude Code web environments.

This wrapper handles proxy authentication that Chromium/Playwright cannot handle natively.
It intercepts both CONNECT tunnels and plain HTTP requests, adding the Proxy-Authorization
header before forwarding to the upstream proxy server.

Inspired by the local forwarding proxy pattern used in simonw/research go-rod-cli.
"""

import socket
import threading
import base64
import logging
import os
import shutil
import subprocess
import time
from ipaddress import ip_address
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_wrapper_thread = None
_wrapper_server = None
_wrapper_port = None
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

    Reads HTTPS_PROXY, HTTP_PROXY, and NO_PROXY environment variables.

    Returns:
        Dict with proxy details or None
    """
    proxy_url = (
        os.environ.get('HTTPS_PROXY')
        or os.environ.get('https_proxy')
        or os.environ.get('HTTP_PROXY')
        or os.environ.get('http_proxy')
    )

    if not proxy_url:
        return None

    parsed = urlparse(proxy_url)

    if not parsed.hostname or not parsed.port:
        return None

    # Parse NO_PROXY into a list of patterns
    no_proxy_raw = os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or ''
    no_proxy = [p.strip() for p in no_proxy_raw.split(',') if p.strip()]

    return {
        'host': parsed.hostname,
        'port': parsed.port,
        'username': parsed.username,
        'password': parsed.password,
        'url': proxy_url,
        'no_proxy': no_proxy,
    }


def _should_bypass_proxy(hostname, proxy_config):
    """
    Check if a hostname should bypass the proxy based on NO_PROXY rules.

    Supports:
    - Exact matches: "example.com"
    - Domain suffixes: ".example.com" matches "sub.example.com"
    - Wildcard: "*" bypasses everything
    - IP addresses: "127.0.0.1", "::1"
    - localhost is always bypassed

    Returns:
        True if the request should bypass the proxy
    """
    # Always bypass localhost regardless of NO_PROXY
    if hostname in ('localhost', '127.0.0.1', '::1'):
        return True

    no_proxy = proxy_config.get('no_proxy', [])
    if not no_proxy:
        return False

    hostname_lower = hostname.lower()

    for pattern in no_proxy:
        pattern = pattern.lower().strip()

        if pattern == '*':
            return True

        # Check if it's an IP match
        try:
            if ip_address(hostname) == ip_address(pattern):
                return True
        except ValueError:
            pass

        # Domain suffix match: ".example.com" matches "sub.example.com"
        if pattern.startswith('.'):
            if hostname_lower.endswith(pattern) or hostname_lower == pattern[1:]:
                return True
        elif hostname_lower == pattern:
            return True

    return False


def _build_auth_header(proxy_config):
    """Build the Proxy-Authorization header value, or None if no credentials."""
    if proxy_config.get('username') and proxy_config.get('password'):
        credentials = f"{proxy_config['username']}:{proxy_config['password']}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"
    return None


def _inject_auth_header(request_lines, auth_header):
    """
    Strip any existing Proxy-Authorization and inject a new one.

    Returns the modified request as a string ending with \\r\\n\\r\\n.
    """
    result = request_lines[0] + '\r\n'
    for line in request_lines[1:]:
        if line and not line.lower().startswith('proxy-authorization:'):
            result += line + '\r\n'
    if auth_header:
        result = result.rstrip('\r\n') + '\r\n'
        result += f'Proxy-Authorization: {auth_header}\r\n'
    result += '\r\n'
    return result


def _send_error(client_socket, status_code, reason):
    """Send an HTTP error response back to the client (Chrome)."""
    body = f"{status_code} {reason}\r\n"
    response = (
        f"HTTP/1.1 {status_code} {reason}\r\n"
        f"Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
        f"{body}"
    )
    try:
        client_socket.sendall(response.encode())
    except OSError:
        pass


def _forward_data(source, destination, label=""):
    """Bidirectional forwarding helper with logging on errors."""
    try:
        while True:
            data = source.recv(8192)
            if not data:
                break
            destination.sendall(data)
    except OSError as e:
        logger.debug("Forward %s ended: %s", label, e)
    finally:
        for sock in (source, destination):
            try:
                sock.close()
            except OSError:
                pass


def _handle_connect(client_socket, request_lines, proxy_config, auth_header):
    """Handle a CONNECT tunnel request."""
    proxy_socket = None
    try:
        proxy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy_socket.settimeout(30)
        proxy_socket.connect((proxy_config['host'], proxy_config['port']))

        modified = _inject_auth_header(request_lines, auth_header)
        proxy_socket.sendall(modified.encode())

        # Read upstream proxy response
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = proxy_socket.recv(4096)
            if not chunk:
                break
            response += chunk

        client_socket.sendall(response)

        if b"200" in response[:50]:
            # Tunnel established - bidirectional forwarding
            t1 = threading.Thread(
                target=_forward_data,
                args=(client_socket, proxy_socket, "client->proxy"),
                daemon=True,
            )
            t2 = threading.Thread(
                target=_forward_data,
                args=(proxy_socket, client_socket, "proxy->client"),
                daemon=True,
            )
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        else:
            status_line = response.split(b'\r\n', 1)[0].decode('utf-8', errors='ignore')
            logger.warning("Upstream proxy rejected CONNECT: %s", status_line)
            client_socket.close()
            proxy_socket.close()

    except socket.timeout:
        logger.warning("Timeout connecting to upstream proxy %s:%s", proxy_config['host'], proxy_config['port'])
        _send_error(client_socket, 504, "Gateway Timeout")
        if proxy_socket:
            try:
                proxy_socket.close()
            except OSError:
                pass
    except OSError as e:
        logger.warning("Error in CONNECT handler: %s", e)
        _send_error(client_socket, 502, "Bad Gateway")
        if proxy_socket:
            try:
                proxy_socket.close()
            except OSError:
                pass


def _handle_http(client_socket, request_lines, full_request, proxy_config, auth_header):
    """Handle a plain HTTP request (GET, POST, etc.) through the proxy."""
    proxy_socket = None
    try:
        proxy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy_socket.settimeout(30)
        proxy_socket.connect((proxy_config['host'], proxy_config['port']))

        modified = _inject_auth_header(request_lines, auth_header)
        proxy_socket.sendall(modified.encode())

        # If the original request had a body beyond headers, forward it
        header_end = full_request.find(b'\r\n\r\n')
        if header_end != -1 and header_end + 4 < len(full_request):
            proxy_socket.sendall(full_request[header_end + 4:])

        # Stream the response back to Chrome
        while True:
            chunk = proxy_socket.recv(8192)
            if not chunk:
                break
            client_socket.sendall(chunk)

    except socket.timeout:
        logger.warning("Timeout on HTTP request to upstream proxy")
        _send_error(client_socket, 504, "Gateway Timeout")
    except OSError as e:
        logger.warning("Error in HTTP handler: %s", e)
        _send_error(client_socket, 502, "Bad Gateway")
    finally:
        for sock in (client_socket, proxy_socket):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass


def handle_client(client_socket, proxy_config):
    """Handle a single client connection - dispatches to CONNECT or HTTP handler."""
    try:
        # Read the client request headers
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = client_socket.recv(4096)
            if not chunk:
                break
            request += chunk

        if not request:
            client_socket.close()
            return

        request_str = request.decode('utf-8', errors='ignore')
        lines = request_str.split('\r\n')

        if not lines or not lines[0]:
            _send_error(client_socket, 400, "Bad Request")
            client_socket.close()
            return

        method = lines[0].split(' ', 1)[0].upper()

        # Check NO_PROXY bypass
        target = lines[0].split(' ')[1] if len(lines[0].split(' ')) > 1 else ''
        if method == 'CONNECT':
            hostname = target.split(':')[0]
        else:
            try:
                hostname = urlparse(target).hostname or ''
            except Exception:
                hostname = ''

        if _should_bypass_proxy(hostname, proxy_config):
            logger.debug("Bypassing proxy for %s (NO_PROXY match)", hostname)
            _send_error(client_socket, 502, "Direct connection not supported through proxy wrapper")
            client_socket.close()
            return

        auth_header = _build_auth_header(proxy_config)

        if method == 'CONNECT':
            _handle_connect(client_socket, lines, proxy_config, auth_header)
        else:
            _handle_http(client_socket, lines, request, proxy_config, auth_header)

    except Exception as e:
        logger.warning("Unhandled error in client handler: %s", e)
        try:
            _send_error(client_socket, 500, "Internal Proxy Error")
        except OSError:
            pass
        try:
            client_socket.close()
        except OSError:
            pass


def _find_free_port():
    """Find a free port by binding to port 0 and letting the OS assign one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start_proxy_wrapper(proxy_config, verbose=True):
    """
    Start the proxy wrapper server.

    Binds to a dynamically allocated port on 127.0.0.1.

    Args:
        proxy_config: Proxy configuration dict
        verbose: Print status messages

    Returns:
        Dict with local proxy info including 'server' URL
    """
    global _wrapper_server, _wrapper_port

    if _wrapper_server:
        if verbose:
            print(f"   Proxy wrapper already running on 127.0.0.1:{_wrapper_port}")
        return {'server': f'http://127.0.0.1:{_wrapper_port}'}

    _wrapper_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _wrapper_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    _wrapper_server.bind(('127.0.0.1', 0))
    _wrapper_port = _wrapper_server.getsockname()[1]
    _wrapper_server.listen(10)

    if verbose:
        print(f"   Starting proxy auth wrapper on 127.0.0.1:{_wrapper_port}")
        print(f"   Forwarding to: {proxy_config['host']}:{proxy_config['port']}")
        if proxy_config.get('no_proxy'):
            print(f"   NO_PROXY: {', '.join(proxy_config['no_proxy'])}")

    def accept_connections():
        while True:
            try:
                client, addr = _wrapper_server.accept()
                thread = threading.Thread(target=handle_client, args=(client, proxy_config), daemon=True)
                thread.start()
            except OSError:
                break

    global _wrapper_thread
    _wrapper_thread = threading.Thread(target=accept_connections, daemon=True)
    _wrapper_thread.start()

    # Give it a moment to start
    time.sleep(0.1)

    return {'server': f'http://127.0.0.1:{_wrapper_port}'}


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
            print(f"   Using existing display: {os.environ['DISPLAY']}")
        return True

    # Already started Xvfb
    if _xvfb_process and _xvfb_process.poll() is None:
        if verbose:
            print(f"   Xvfb already running on {os.environ.get('DISPLAY', ':99')}")
        return True

    # Check if Xvfb is available, try to install if not
    if not shutil.which('Xvfb'):
        if verbose:
            print("   Xvfb not found, attempting to install...")
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
                print("   Xvfb not available, falling back to headless mode")
            return False
        if verbose:
            print("   Xvfb installed")

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
                print("   Xvfb failed to start, falling back to headless mode")
            _xvfb_process = None
            return False

        os.environ['DISPLAY'] = display
        if verbose:
            print(f"   Started Xvfb virtual display on {display}")
        return True

    except Exception as e:
        if verbose:
            print(f"   Failed to start Xvfb: {e}, falling back to headless mode")
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
            print("   Using Chrome for improved stealth (falls back to Chromium if unavailable)")

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
                print("   Proxy authentication configured")

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
                    print("   Headed mode via Xvfb (better anti-bot evasion)")
        elif headless is None:
            config['launch_options']['headless'] = True
            config['xvfb_used'] = False
            if verbose:
                print("   Headless mode enabled (web environment default)")
        else:
            config['launch_options']['headless'] = True
            config['xvfb_used'] = False
            if verbose:
                print("   Headless mode enabled")
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
    global _wrapper_server, _wrapper_thread, _wrapper_port

    if _wrapper_server:
        try:
            _wrapper_server.close()
        except OSError:
            pass
        _wrapper_server = None
        _wrapper_thread = None
        _wrapper_port = None


__all__ = [
    'is_claude_code_web_environment',
    'get_proxy_config',
    'get_browser_config',
    'start_proxy_wrapper',
    'stop_proxy_wrapper',
    'ensure_virtual_display',
    'stop_virtual_display',
]
