"""End-to-end smoke tests against a real Chrome.

Skipped when Chrome can't be launched, unless REQUIRE_BROWSER=1 (as in CI).
These guard the patchright regressions that once forced a vendored build:
init scripts that silently never ran, and clicks that hung.
"""

import asyncio
import functools
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

patchright = pytest.importorskip("patchright")
from patchright.async_api import async_playwright  # noqa: E402

from lib.helpers import get_browser_config, handle_cookie_banner, take_screenshot  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def base_url():
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run_with_page(fn):
    """Launch the configured browser, run fn(page), and close everything."""
    async def runner():
        config = get_browser_config(headless=True, verbose=False)
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(**config["launch_options"])
            except Exception as error:
                if os.environ.get("REQUIRE_BROWSER") == "1":
                    raise
                pytest.skip(f"Chrome not available: {error}")
            try:
                context = await browser.new_context(**config["context_options"])
                page = await context.new_page()
                return await fn(page)
            finally:
                await browser.close()
    return asyncio.run(runner())


def test_goto_and_click_navigation(base_url):
    async def check(page):
        await page.goto(f"{base_url}/index.html")
        assert await page.title() == "Fixture Home"
        await page.get_by_role("link", name="Next page").click(timeout=10000)
        await page.wait_for_url("**/next.html")
        assert await page.title() == "Fixture Next"
    run_with_page(check)


def test_init_scripts_run_across_navigations(base_url):
    async def check(page):
        await page.context.add_init_script("window.__ctxInit = 'ctx'")
        await page.add_init_script("window.__pageInit = 'page'")
        # Init scripts run in the main world; patchright's evaluate() defaults
        # to an isolated one
        read = "() => [window.__ctxInit, window.__pageInit]"
        for path in ("/index.html", "/next.html"):
            await page.goto(f"{base_url}{path}")
            assert await page.evaluate(read, isolated_context=False) == ["ctx", "page"]
    run_with_page(check)


def test_handle_cookie_banner(base_url):
    async def check(page):
        await page.goto(f"{base_url}/index.html")
        assert await handle_cookie_banner(page) is True
        assert await page.locator("#cookie-banner").count() == 0
    run_with_page(check)


def test_take_screenshot(base_url, tmp_path):
    async def check(page):
        await page.goto(f"{base_url}/index.html")
        path = await take_screenshot(page, "smoke", {"directory": str(tmp_path)})
        assert Path(path).stat().st_size > 0
    run_with_page(check)
