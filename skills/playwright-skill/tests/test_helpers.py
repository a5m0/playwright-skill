"""Tests for lib.helpers (no browser required)."""

import asyncio
import os
import socket
from unittest import mock

from lib.helpers import (
    create_context,
    detect_dev_servers,
    detect_dev_servers_sync,
    get_extra_headers_from_env,
    launch_browser,
    take_screenshot,
)


class _Recorder:
    """Records the kwargs of the last awaited call."""

    def __init__(self):
        self.kwargs = None

    async def __call__(self, **kwargs):
        self.kwargs = kwargs
        return kwargs


def _fake_playwright():
    launch = _Recorder()
    return mock.Mock(chromium=mock.Mock(launch=launch)), launch


def _fake_browser():
    new_context = _Recorder()
    return mock.Mock(new_context=new_context), new_context


# ===================================================================
# get_extra_headers_from_env
# ===================================================================

class TestGetExtraHeadersFromEnv:
    def test_none_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            assert get_extra_headers_from_env() is None

    def test_single_header(self):
        env = {"PW_HEADER_NAME": "X-Test", "PW_HEADER_VALUE": "1"}
        with mock.patch.dict(os.environ, env, clear=True):
            assert get_extra_headers_from_env() == {"X-Test": "1"}

    def test_json_headers(self):
        env = {"PW_EXTRA_HEADERS": '{"A": "1", "B": "2"}'}
        with mock.patch.dict(os.environ, env, clear=True):
            assert get_extra_headers_from_env() == {"A": "1", "B": "2"}

    def test_single_header_takes_precedence(self):
        env = {"PW_HEADER_NAME": "X", "PW_HEADER_VALUE": "1", "PW_EXTRA_HEADERS": '{"A": "1"}'}
        with mock.patch.dict(os.environ, env, clear=True):
            assert get_extra_headers_from_env() == {"X": "1"}

    def test_invalid_json_ignored(self):
        with mock.patch.dict(os.environ, {"PW_EXTRA_HEADERS": "{nope"}, clear=True):
            assert get_extra_headers_from_env() is None

    def test_non_object_json_ignored(self):
        with mock.patch.dict(os.environ, {"PW_EXTRA_HEADERS": "[1, 2]"}, clear=True):
            assert get_extra_headers_from_env() is None


# ===================================================================
# launch_browser
# ===================================================================

class TestLaunchBrowser:
    def test_visible_by_default(self):
        p, launch = _fake_playwright()
        with mock.patch.dict(os.environ, {}, clear=True):
            asyncio.run(launch_browser(p))
        assert launch.kwargs["headless"] is False
        assert launch.kwargs["slow_mo"] == 0
        assert "args" not in launch.kwargs

    def test_pw_headless_env(self):
        p, launch = _fake_playwright()
        with mock.patch.dict(os.environ, {"PW_HEADLESS": "true"}, clear=True):
            asyncio.run(launch_browser(p))
        assert launch.kwargs["headless"] is True

    def test_empty_pw_headless_treated_as_unset(self):
        p, launch = _fake_playwright()
        with mock.patch.dict(os.environ, {"PW_HEADLESS": "", "HEADLESS": "true"}, clear=True):
            asyncio.run(launch_browser(p))
        assert launch.kwargs["headless"] is True

    def test_options_override_defaults(self):
        p, launch = _fake_playwright()
        with mock.patch.dict(os.environ, {"SLOW_MO": "50"}, clear=True):
            asyncio.run(launch_browser(p, options={"headless": True, "args": ["--foo"]}))
        assert launch.kwargs["headless"] is True
        assert launch.kwargs["slow_mo"] == 50
        assert launch.kwargs["args"] == ["--foo"]


# ===================================================================
# create_context
# ===================================================================

class TestCreateContext:
    def test_defaults(self):
        browser, new_context = _fake_browser()
        with mock.patch.dict(os.environ, {}, clear=True):
            asyncio.run(create_context(browser))
        assert new_context.kwargs == {
            "viewport": {"width": 1280, "height": 720},
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }

    def test_env_headers_merged_with_option_headers(self):
        browser, new_context = _fake_browser()
        env = {"PW_EXTRA_HEADERS": '{"A": "env", "B": "env"}'}
        with mock.patch.dict(os.environ, env, clear=True):
            asyncio.run(create_context(browser, {"extra_http_headers": {"B": "opt"}}))
        assert new_context.kwargs["extra_http_headers"] == {"A": "env", "B": "opt"}

    def test_device_descriptor_passes_through(self):
        browser, new_context = _fake_browser()
        device = {
            "user_agent": "iPhone UA",
            "viewport": {"width": 393, "height": 659},
            "is_mobile": True,
            "has_touch": True,
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            asyncio.run(create_context(browser, device))
        assert new_context.kwargs["viewport"] == {"width": 393, "height": 659}
        assert new_context.kwargs["is_mobile"] is True
        assert new_context.kwargs["user_agent"] == "iPhone UA"
        assert "extra_http_headers" not in new_context.kwargs


# ===================================================================
# take_screenshot
# ===================================================================

class TestTakeScreenshot:
    def _page(self):
        screenshot = _Recorder()
        return mock.Mock(screenshot=screenshot), screenshot

    def test_explicit_path(self, tmp_path):
        page, screenshot = self._page()
        target = str(tmp_path / "shot.png")
        result = asyncio.run(take_screenshot(page, "x", {"path": target}))
        assert result == target
        assert screenshot.kwargs == {"path": target, "full_page": True}

    def test_artifact_dir_env(self, tmp_path):
        page, screenshot = self._page()
        out = tmp_path / "artifacts"
        with mock.patch.dict(os.environ, {"PW_ARTIFACT_DIR": str(out)}):
            result = asyncio.run(take_screenshot(page, "home"))
        assert os.path.dirname(result) == str(out)
        assert os.path.basename(result).startswith("home-")
        assert out.is_dir()

    def test_defaults_to_temp_dir_not_cwd(self, tmp_path):
        page, _ = self._page()
        env = {k: v for k, v in os.environ.items() if k != "PW_ARTIFACT_DIR"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("tempfile.gettempdir", return_value=str(tmp_path)):
            result = asyncio.run(take_screenshot(page, "home"))
        assert os.path.dirname(result) == str(tmp_path)

    def test_directory_option_and_passthrough(self, tmp_path):
        page, screenshot = self._page()
        result = asyncio.run(
            take_screenshot(page, "x", {"directory": str(tmp_path), "full_page": False, "type": "jpeg"})
        )
        assert os.path.dirname(result) == str(tmp_path)
        assert screenshot.kwargs["full_page"] is False
        assert screenshot.kwargs["type"] == "jpeg"
        assert "directory" not in screenshot.kwargs


# ===================================================================
# detect_dev_servers
# ===================================================================

class TestDetectDevServers:
    def test_detects_listening_custom_port(self):
        with socket.socket() as server:
            server.bind(("localhost", 0))
            server.listen()
            port = server.getsockname()[1]
            assert f"http://localhost:{port}" in detect_dev_servers_sync([port])
            assert f"http://localhost:{port}" in asyncio.run(detect_dev_servers([port]))

    def test_closed_port_not_detected(self):
        with socket.socket() as probe:
            probe.bind(("localhost", 0))
            port = probe.getsockname()[1]
        assert f"http://localhost:{port}" not in detect_dev_servers_sync([port])
