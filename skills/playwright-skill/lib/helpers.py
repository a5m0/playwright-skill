# patchright-helpers.py
# Reusable utility functions for Patchright automation

import os
import json
import asyncio
import socket
import tempfile
from typing import Optional, List, Dict, Any


def get_extra_headers_from_env() -> Optional[Dict[str, str]]:
    """
    Parse extra HTTP headers from environment variables.
    Supports two formats:
    - PW_HEADER_NAME + PW_HEADER_VALUE: Single header (simple, common case)
    - PW_EXTRA_HEADERS: JSON object for multiple headers (advanced)
    Single header format takes precedence if both are set.
    Returns headers dict or None if none configured.
    """
    header_name = os.environ.get('PW_HEADER_NAME')
    header_value = os.environ.get('PW_HEADER_VALUE')

    if header_name and header_value:
        return {header_name: header_value}

    headers_json = os.environ.get('PW_EXTRA_HEADERS')
    if headers_json:
        try:
            parsed = json.loads(headers_json)
            if isinstance(parsed, dict):
                return parsed
            print("PW_EXTRA_HEADERS must be a JSON object, ignoring...")
        except json.JSONDecodeError as e:
            print(f"Failed to parse PW_EXTRA_HEADERS as JSON: {e}")

    return None


async def launch_browser(playwright, browser_type: str = 'chromium', options: Dict[str, Any] = None):
    """
    Launch browser with standard configuration.
    Note: Patchright only supports Chromium-based browsers.

    For Claude Code web environments (proxy, certificates, Xvfb), prefer
    get_browser_config(), which handles all of that automatically.

    Args:
        playwright: The async_playwright instance
        browser_type: 'chromium' (firefox/webkit not supported in patchright)
        options: Additional launch options (override the defaults)
    """
    if options is None:
        options = {}

    if browser_type != 'chromium':
        print(f"⚠️ Warning: Patchright only supports Chromium. Using chromium instead of {browser_type}")

    # Empty values count as unset; anything but 'false' means headless
    headless_value = os.environ.get('PW_HEADLESS') or os.environ.get('HEADLESS') or 'false'
    # No sandbox flags needed: the driver adds --no-sandbox unless
    # chromium_sandbox=True is passed
    launch_options = {
        'headless': headless_value.lower() != 'false',
        'slow_mo': int(os.environ.get('SLOW_MO') or 0),
        **options,
    }

    return await playwright.chromium.launch(**launch_options)


async def create_context(browser, options: Dict[str, Any] = None):
    """
    Create browser context with common settings.
    Automatically merges environment headers.

    For mobile emulation, pass a device descriptor:
        create_context(browser, p.devices['iPhone 15'])

    Args:
        browser: Browser instance
        options: Context options (override the defaults)
    """
    if options is None:
        options = {}

    headers = {**(get_extra_headers_from_env() or {}), **options.get('extra_http_headers', {})}
    context_options = {
        'viewport': {'width': 1280, 'height': 720},
        'locale': 'en-US',
        'timezone_id': 'America/New_York',
        **options,
    }
    if headers:
        context_options['extra_http_headers'] = headers

    return await browser.new_context(**context_options)


async def take_screenshot(page, name: str, options: Dict[str, Any] = None) -> str:
    """
    Take a full-page screenshot with a timestamped filename.

    Saved to options['path'] if given, otherwise to options['directory'],
    $PW_ARTIFACT_DIR, or the system temp directory (in that order).

    Args:
        page: Patchright page
        name: Screenshot name prefix
        options: path, directory, plus any page.screenshot() options
    """
    options = dict(options or {})
    filename = options.pop('path', None)
    directory = options.pop('directory', None)

    if not filename:
        from datetime import datetime
        output_dir = directory or os.environ.get('PW_ARTIFACT_DIR') or tempfile.gettempdir()
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().isoformat().replace(':', '-').replace('.', '-')
        filename = os.path.join(output_dir, f"{name}-{timestamp}.png")

    await page.screenshot(path=filename, **{'full_page': True, **options})

    print(f"Screenshot saved: {filename}")
    return filename


async def handle_cookie_banner(page, timeout: int = 3000) -> bool:
    """
    Dismiss a cookie banner if one of the common accept buttons is visible.

    Args:
        page: Patchright page
        timeout: Total time budget in milliseconds
    """
    common_selectors = [
        'button:has-text("Accept")',
        'button:has-text("Accept all")',
        'button:has-text("OK")',
        'button:has-text("Got it")',
        'button:has-text("I agree")',
        '.cookie-accept',
        '#cookie-accept',
        '[data-testid="cookie-accept"]'
    ]

    selector_timeout = timeout // len(common_selectors)

    for selector in common_selectors:
        try:
            await page.locator(selector).filter(visible=True).first.click(timeout=selector_timeout)
            print("Cookie banner dismissed")
            return True
        except Exception:
            continue

    return False


def detect_dev_servers_sync(custom_ports: List[int] = None) -> List[str]:
    """
    Detect running dev servers on common ports.

    Args:
        custom_ports: Additional ports to check

    Returns:
        List of detected server URLs
    """
    if custom_ports is None:
        custom_ports = []

    common_ports = [3000, 3001, 3002, 5173, 8080, 8000, 4200, 5000, 9000, 1234]
    all_ports = sorted(set(common_ports + custom_ports))

    detected_servers = []

    print("🔍 Checking for running dev servers...")

    for port in all_ports:
        try:
            with socket.create_connection(('localhost', port), timeout=0.5):
                detected_servers.append(f"http://localhost:{port}")
                print(f"  ✅ Found server on port {port}")
        except OSError:
            continue

    if not detected_servers:
        print("  ❌ No dev servers detected")

    return detected_servers


async def detect_dev_servers(custom_ports: List[int] = None) -> List[str]:
    """Async wrapper around detect_dev_servers_sync()."""
    return await asyncio.to_thread(detect_dev_servers_sync, custom_ports)


async def extract_content(
    page,
    output_format: str = 'markdown',
    include_comments: bool = False,
    include_tables: bool = True,
    include_links: bool = True,
    include_images: bool = False,
    with_metadata: bool = False,
    url: Optional[str] = None
) -> Optional[str]:
    """
    Extract main content from a page using Trafilatura.

    Args:
        page: Patchright page
        output_format: Output format ('markdown', 'txt', 'json', 'xml', 'csv')
        include_comments: Include comments in output
        include_tables: Include tables in output
        include_links: Include links in output
        include_images: Include images in output
        with_metadata: Include metadata in output
        url: Optional URL for metadata (uses page.url if not provided)

    Returns:
        Extracted content as string, or None if extraction failed
    """
    try:
        import trafilatura
    except ImportError:
        print("⚠️ Trafilatura not installed. Run: pip install trafilatura")
        return None

    try:
        # Get HTML content from the page
        html_content = await page.content()

        # Use page URL if not provided
        if url is None:
            url = page.url

        # Extract content using Trafilatura
        extracted = trafilatura.extract(
            html_content,
            output_format=output_format,
            url=url,
            include_comments=include_comments,
            include_tables=include_tables,
            include_links=include_links,
            include_images=include_images,
            with_metadata=with_metadata
        )

        return extracted

    except Exception as e:
        print(f"❌ Content extraction failed: {e}")
        return None


async def extract_markdown(
    page,
    include_tables: bool = True,
    include_links: bool = True,
    include_images: bool = False
) -> Optional[str]:
    """
    Extract page content as Markdown.

    Args:
        page: Patchright page
        include_tables: Include tables in markdown
        include_links: Include links in markdown
        include_images: Include images in markdown

    Returns:
        Markdown content as string, or None if extraction failed
    """
    return await extract_content(
        page,
        output_format='markdown',
        include_tables=include_tables,
        include_links=include_links,
        include_images=include_images
    )


async def extract_text(page, include_tables: bool = True) -> Optional[str]:
    """
    Extract page content as plain text.

    Args:
        page: Patchright page
        include_tables: Include tables in text

    Returns:
        Plain text content as string, or None if extraction failed
    """
    return await extract_content(
        page,
        output_format='txt',
        include_tables=include_tables,
        include_links=False
    )


async def extract_with_metadata(
    page,
    output_format: str = 'json'
) -> Optional[Dict[str, Any]]:
    """
    Extract page content with metadata (title, author, date, etc.).

    Args:
        page: Patchright page
        output_format: Output format ('json', 'xml')

    Returns:
        Dictionary with content and metadata, or None if extraction failed
    """
    try:
        import trafilatura
        import json as json_lib
    except ImportError:
        print("⚠️ Trafilatura not installed. Run: pip install trafilatura")
        return None

    try:
        html_content = await page.content()
        url = page.url

        extracted = trafilatura.extract(
            html_content,
            output_format=output_format,
            url=url,
            with_metadata=True,
            include_tables=True,
            include_links=True
        )

        if output_format == 'json' and extracted:
            return json_lib.loads(extracted)
        else:
            return extracted

    except Exception as e:
        print(f"❌ Metadata extraction failed: {e}")
        return None


# Import proxy wrapper functions
try:
    from .proxy_wrapper import (
        is_claude_code_remote_environment,
        is_claude_code_web_environment,
        get_proxy_config,
        get_browser_config,
        start_proxy_wrapper,
        stop_proxy_wrapper,
        ensure_virtual_display,
        stop_virtual_display,
    )
    _PROXY_WRAPPER_AVAILABLE = True
except ImportError:
    _PROXY_WRAPPER_AVAILABLE = False
    # Provide stub functions if import fails
    def is_claude_code_remote_environment():
        return False

    def is_claude_code_web_environment():
        return False

    def get_proxy_config():
        return None

    def get_browser_config(headless=None, verbose=True, use_chrome=True):
        config = {
            'launch_options': {'args': []},
            'context_options': {},
            'proxy_wrapper_used': False,
            'xvfb_used': False
        }
        if use_chrome:
            config['launch_options']['channel'] = 'chrome'
        if headless is not None:
            config['launch_options']['headless'] = headless
        return config

    def start_proxy_wrapper(proxy_config, verbose=True):
        return None

    def stop_proxy_wrapper():
        pass

    def ensure_virtual_display(verbose=True):
        return False

    def stop_virtual_display():
        pass


# Import persistent session helpers
from .persistent_session import (
    is_persistent_session_running,
    get_persistent_session_info,
    connect_to_persistent_session,
)


__all__ = [
    'get_extra_headers_from_env',
    'launch_browser',
    'create_context',
    'take_screenshot',
    'handle_cookie_banner',
    'detect_dev_servers',
    'detect_dev_servers_sync',
    'extract_content',
    'extract_markdown',
    'extract_text',
    'extract_with_metadata',
    'is_claude_code_remote_environment',
    'is_claude_code_web_environment',
    'get_proxy_config',
    'get_browser_config',
    'start_proxy_wrapper',
    'stop_proxy_wrapper',
    'ensure_virtual_display',
    'stop_virtual_display',
    'is_persistent_session_running',
    'get_persistent_session_info',
    'connect_to_persistent_session',
]
