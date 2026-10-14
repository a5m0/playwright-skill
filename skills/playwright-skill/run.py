#!/usr/bin/env python3
"""Universal Patchright Executor for Claude Code

Executes Patchright automation code from:
- File path: python run.py script.py
- Inline code: python run.py 'await page.goto("...")'
- Stdin: cat script.py | python run.py

Scripts run in the caller's working directory, so relative paths resolve
against the user's project; the skill's lib/ package stays importable.
Uses Patchright (undetected Playwright fork) for anti-bot evasion.
"""

import glob
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).parent.resolve()

# Add skill directory to Python path
sys.path.insert(0, str(SKILL_DIR))

# Older releases shipped broken init-script routing; keep in sync with
# pyproject.toml and scripts/setup-session.sh
PATCHRIGHT_MIN_VERSION = "1.63.0"


def _version_tuple(version):
    return tuple(int(part) for part in version.split(".")[:3] if part.isdigit())


def check_patchright_installed():
    """Check if a recent enough Patchright is installed"""
    from importlib import metadata

    try:
        installed = metadata.version("patchright")
    except metadata.PackageNotFoundError:
        return False
    return _version_tuple(installed) >= _version_tuple(PATCHRIGHT_MIN_VERSION)


def is_uv_available():
    """Check if uv is available"""
    try:
        subprocess.run(["uv", "--version"], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def install_patchright():
    """Install or upgrade Patchright and Chrome. Prefers uv, then pip."""
    print(f"📦 Patchright >= {PATCHRIGHT_MIN_VERSION} not found. Installing...")

    use_uv = is_uv_available()
    requirements = [f"patchright>={PATCHRIGHT_MIN_VERSION}", "trafilatura>=2.0.0"]

    try:
        # Install into the interpreter running this script (which also runs the
        # user's code), not the system Python, so an active venv works
        if use_uv:
            print("  Using uv for installation...")
            subprocess.run(
                ["uv", "pip", "install", "--python", sys.executable, *requirements],
                check=True,
                cwd=SKILL_DIR,
            )
        else:
            print("  Using pip for installation...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", *requirements],
                check=True,
                cwd=SKILL_DIR,
            )

        subprocess.run(
            [sys.executable, "-m", "patchright", "install", "chrome"],
            check=True,
            cwd=SKILL_DIR,
        )
        print("✅ Patchright installed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install Patchright: {e}")
        installer = "uv pip" if use_uv else "pip"
        print(
            f"Please run manually: {installer} install '{requirements[0]}' && python3 -m patchright install chrome"
        )
        return False


def get_code_to_execute():
    """Get code to execute from various sources"""
    args = sys.argv[1:]

    # Case 1: File path provided
    if args and os.path.isfile(args[0]):
        file_path = os.path.abspath(args[0])
        print(f"📄 Executing file: {file_path}")
        with open(file_path) as f:
            return f.read()

    # Case 2: Inline code provided as argument
    if args:
        print("⚡ Executing inline code")
        return " ".join(args)

    # Case 3: Code from stdin
    if not sys.stdin.isatty():
        print("📥 Reading from stdin")
        return sys.stdin.read()

    # No input
    print("❌ No code to execute")
    print("Usage:")
    print("  python run.py script.py          # Execute file")
    print('  python run.py "code here"        # Execute inline')
    print("  cat script.py | python run.py    # Execute from stdin")
    sys.exit(1)


def cleanup_old_temp_files():
    """Clean up old temporary execution files from previous runs"""
    try:
        temp_files = glob.glob(str(SKILL_DIR / ".temp-execution-*.py"))
        for file_path in temp_files:
            try:
                os.unlink(file_path)
            except Exception:
                pass  # Ignore errors - file might be in use or already deleted
    except Exception:
        pass  # Ignore directory read errors


def _indent_code(code, spaces):
    """Indent each line of code by the given number of spaces."""
    indent = ' ' * spaces
    lines = code.split('\n')
    # Strip common leading whitespace first, then re-indent
    import textwrap
    dedented = textwrap.dedent(code)
    return '\n'.join(indent + line if line.strip() else line
                     for line in dedented.split('\n'))


def _needs_auto_browser(code):
    """Check if inline code needs auto-configured browser/page.

    Returns True when the code uses 'page' or 'browser' directly without
    creating its own via p.chromium.launch(). This lets simple inline tasks
    skip all the boilerplate.
    """
    # If code explicitly launches or connects to a browser, it manages its own lifecycle
    if 'p.chromium.launch' in code or 'p.chromium.connect' in code:
        return False
    # connect_to_persistent_session() also manages its own browser lifecycle
    if 'connect_to_persistent_session' in code:
        return False
    # If code references page/browser as pre-existing variables, auto-configure
    if 'page.' in code or 'await page' in code:
        return True
    if 'browser.' in code and 'browser = ' not in code:
        return True
    return False


def wrap_code_if_needed(code):
    """Wrap code in async function if not already wrapped.

    Three modes:
    1. Complete script (has imports + async def main): run as-is
    2. Partial script (has imports, no wrapper): wrap in async main()
    3. Inline snippet (no imports):
       a. Uses page/browser directly: auto-configure browser, context, page
       b. Uses p.chromium.launch: provide just playwright instance
    """
    # Check if code already has imports and async structure
    has_import = "from patchright" in code or "import patchright" in code
    has_async_main = "async def main" in code or "asyncio.run" in code

    # If it's already a complete script, return as-is
    if has_import and has_async_main:
        return code

    # If it's just Patchright commands, wrap in full template
    if not has_import:
        # Detect whether to provide auto-configured browser+page
        auto_browser = _needs_auto_browser(code)

        if auto_browser:
            # Auto-configured mode: provides browser, context, page, config
            # User code just does: await page.goto(...), print(await page.title()), etc.
            indented = _indent_code(code, 12)
            return f'''
import asyncio
import os
import sys
from pathlib import Path

# Add skill directory to path for helpers import
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from patchright.async_api import async_playwright
from lib import helpers
from lib.helpers import (
    get_browser_config, extract_markdown, extract_text,
    extract_with_metadata, extract_content, take_screenshot,
    handle_cookie_banner, stop_virtual_display,
)

async def main():
    config = get_browser_config()
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(**config['launch_options'])
            context = await browser.new_context(**config['context_options'])
            page = await context.new_page()

{indented}

    except Exception as error:
        print(f"❌ Automation error: {{error}}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        stop_virtual_display()


if __name__ == "__main__":
    asyncio.run(main())
'''
        else:
            # Manual mode: provides p (playwright), helpers, config helper
            indented = _indent_code(code, 12)
            return f'''
import asyncio
import os
import sys
from pathlib import Path

# Add skill directory to path for helpers import
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from patchright.async_api import async_playwright
from lib import helpers
from lib.helpers import get_browser_config, stop_virtual_display
from lib.persistent_session import (
    connect_to_persistent_session,
    is_persistent_session_running,
    get_persistent_session_info,
)

# Extra headers from environment variables (if configured)
__extra_headers = helpers.get_extra_headers_from_env()


def get_context_options_with_headers(options=None):
    """
    Utility to merge environment headers into context options.
    Use when creating contexts with raw Patchright API instead of helpers.create_context().
    """
    if options is None:
        options = {{}}
    if not __extra_headers:
        return options

    merged_headers = {{**__extra_headers, **options.get('extra_http_headers', {{}})}}
    return {{**options, 'extra_http_headers': merged_headers}}


async def main():
    try:
        async with async_playwright() as p:
{indented}
    except Exception as error:
        print(f"❌ Automation error: {{error}}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        stop_virtual_display()


if __name__ == "__main__":
    asyncio.run(main())
'''

    # If has import but no async wrapper
    if not has_async_main:
        indented = _indent_code(code, 8)
        return f"""
import asyncio
import sys

async def main():
    try:
{indented}
    except Exception as error:
        print(f"❌ Automation error: {{error}}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
"""

    return code


def main():
    """Main execution"""
    print("🎭 Patchright Skill - Universal Executor\n")

    # Clean up old temp files from previous runs
    cleanup_old_temp_files()

    # Check Patchright installation
    if not check_patchright_installed():
        installed = install_patchright()
        if not installed:
            sys.exit(1)

    # Get code to execute
    raw_code = get_code_to_execute()
    code = wrap_code_if_needed(raw_code)

    # Create temporary file for execution
    temp_file = SKILL_DIR / f".temp-execution-{os.getpid()}.py"

    try:
        # Write code to temp file
        with open(temp_file, "w") as f:
            f.write(code)

        # Execute the code in the caller's working directory so relative paths
        # resolve against the user's project. The temp file lives in SKILL_DIR,
        # which puts the skill's lib/ package on the script's import path.
        print("🚀 Starting automation...\n")
        result = subprocess.run([sys.executable, str(temp_file)])
        sys.exit(result.returncode)

    except Exception as error:
        print(f"❌ Execution failed: {error}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        # Clean up temp file
        try:
            if temp_file.exists():
                temp_file.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
