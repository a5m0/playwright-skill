"""Tests for the run.py executor (no browser required)."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import run
from run import wrap_code_if_needed

RUN_PY = Path(run.__file__).resolve()

# A complete script runs as-is, so these tests never start the driver
COMPLETE_SCRIPT = textwrap.dedent("""\
    import asyncio
    import sys
    from patchright.async_api import async_playwright

    async def main():
        {body}

    asyncio.run(main())
""")


class TestWrapCodeIfNeeded:
    def test_complete_script_unchanged(self):
        code = COMPLETE_SCRIPT.format(body="pass")
        assert wrap_code_if_needed(code) == code

    def test_page_snippet_gets_auto_browser(self):
        wrapped = wrap_code_if_needed("await page.goto('https://example.com')")
        assert "page = await context.new_page()" in wrapped
        assert "get_browser_config()" in wrapped
        compile(wrapped, "<wrapped>", "exec")

    def test_launch_snippet_gets_manual_mode(self):
        wrapped = wrap_code_if_needed("browser = await p.chromium.launch()\nawait browser.close()")
        assert "page = await context.new_page()" not in wrapped
        assert "async with async_playwright() as p:" in wrapped
        compile(wrapped, "<wrapped>", "exec")

    def test_auto_mode_imports_exist(self):
        # Every helper the auto-configured preamble imports must still exist
        wrapped = wrap_code_if_needed("await page.goto('https://example.com')")
        start = wrapped.index("from lib.helpers import (")
        names = wrapped[start:wrapped.index(")", start)].split("(", 1)[1]
        from lib import helpers
        for name in (n.strip() for n in names.split(",")):
            if name:
                assert hasattr(helpers, name), name

    def test_partial_script_wrapped(self):
        wrapped = wrap_code_if_needed("from patchright.async_api import async_playwright\nprint('hi')")
        assert "async def main():" in wrapped
        compile(wrapped, "<wrapped>", "exec")


@pytest.fixture
def require_patchright():
    if not run.check_patchright_installed():
        pytest.skip(f"patchright>={run.PATCHRIGHT_MIN_VERSION} not installed")


class TestExecution:
    def _run(self, tmp_path, body):
        script = tmp_path / "script.py"
        script.write_text(COMPLETE_SCRIPT.format(body=body))
        workdir = tmp_path / "project"
        workdir.mkdir()
        result = subprocess.run(
            [sys.executable, str(RUN_PY), str(script)],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result, workdir

    def test_runs_in_callers_cwd(self, tmp_path, require_patchright):
        result, workdir = self._run(tmp_path, "open('out.txt', 'w').write('ok')")
        assert result.returncode == 0, result.stderr
        assert (workdir / "out.txt").read_text() == "ok"
        assert not (RUN_PY.parent / "out.txt").exists()

    def test_lib_importable_from_any_cwd(self, tmp_path, require_patchright):
        result, _ = self._run(tmp_path, "from lib.helpers import get_browser_config")
        assert result.returncode == 0, result.stderr

    def test_exit_code_preserved(self, tmp_path, require_patchright):
        result, _ = self._run(tmp_path, "sys.exit(3)")
        assert result.returncode == 3
