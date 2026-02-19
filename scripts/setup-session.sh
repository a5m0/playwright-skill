#!/bin/bash
# SessionStart hook for Claude Code on the web
# Automatically installs patchright and Chrome browser at session startup
set -e  # Exit on error

# Helper functions for logging
log_info() {
    echo "[playwright-skill] $1"
}

log_error() {
    echo "[playwright-skill ERROR] $1" >&2
}

log_success() {
    echo "[playwright-skill ✓] $1"
}

# Detect environment (local vs remote/web)
if [ "$CLAUDE_CODE_REMOTE" = "true" ]; then
    log_info "Running in Claude Code on the web environment"
    IS_WEB_ENV=true
else
    log_info "Running in local terminal environment"
    IS_WEB_ENV=false
fi

# Determine skill directory location
# Priority: 1) Relative to this script (always works), 2) CLAUDE_PROJECT_DIR,
#           3) Plugin root, 4) Global, 5) Project .claude/skills
SKILL_DIR=""

# Resolve repo root from this script's own location (scripts/ lives at repo root)
# This works regardless of whether CLAUDE_PROJECT_DIR is set
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -d "${SCRIPT_REPO_ROOT}/skills/playwright-skill" ]; then
    SKILL_DIR="${SCRIPT_REPO_ROOT}/skills/playwright-skill"
    log_info "Found skill relative to script: $SKILL_DIR"
elif [ -n "$CLAUDE_PROJECT_DIR" ] && [ -d "$CLAUDE_PROJECT_DIR/skills/playwright-skill" ]; then
    SKILL_DIR="$CLAUDE_PROJECT_DIR/skills/playwright-skill"
    log_info "Found skill in repository: $SKILL_DIR"
elif [ -n "$CLAUDE_PLUGIN_ROOT" ] && [ -d "$CLAUDE_PLUGIN_ROOT/skills/playwright-skill" ]; then
    SKILL_DIR="$CLAUDE_PLUGIN_ROOT/skills/playwright-skill"
    log_info "Found skill as plugin: $SKILL_DIR"
elif [ -d "$HOME/.claude/skills/playwright-skill" ]; then
    SKILL_DIR="$HOME/.claude/skills/playwright-skill"
    log_info "Found skill in global location: $SKILL_DIR"
elif [ -n "$CLAUDE_PROJECT_DIR" ] && [ -d "$CLAUDE_PROJECT_DIR/.claude/skills/playwright-skill" ]; then
    SKILL_DIR="$CLAUDE_PROJECT_DIR/.claude/skills/playwright-skill"
    log_info "Found skill in project .claude/skills: $SKILL_DIR"
fi

if [ -z "$SKILL_DIR" ]; then
    log_error "Skill directory not found. Checked:"
    log_error "  - ${SCRIPT_REPO_ROOT}/skills/playwright-skill (relative to script)"
    log_error "  - \$CLAUDE_PROJECT_DIR/skills/playwright-skill"
    log_error "  - \$CLAUDE_PLUGIN_ROOT/skills/playwright-skill"
    log_error "  - $HOME/.claude/skills/playwright-skill"
    log_error "  - \$CLAUDE_PROJECT_DIR/.claude/skills/playwright-skill"
    exit 2
fi

# Step 1: Install patchright
# Prefer vendored patched wheel which fixes upstream PRs #96/#99:
#   - CDN URL: playwright.azureedge.net -> cdn.playwright.dev (silent patch failure)
#   - Init script DNS: .internal domain -> route.continue_() (navigation failure)
# When upstream merges these PRs, remove vendor/ and revert to PyPI install.

# Check if uv is available
if ! command -v uv &> /dev/null; then
    log_info "uv not found, installing..."
    pip install uv --quiet || {
        log_error "Failed to install uv"
        exit 2
    }
fi

# Resolve vendor directory relative to skill dir
# Plugin layout: vendor/ is at repo root, skill is at skills/playwright-skill/
REPO_ROOT="$(cd "${SKILL_DIR}/../.." && pwd)"
VENDOR_WHEEL=$(ls "${REPO_ROOT}"/vendor/patchright-*.whl 2>/dev/null | head -1)

# Check if patchright is already installed from the vendor wheel.
# We always prefer the vendor wheel since it contains critical patches.
# If vendor wheel exists, verify install source matches; if not, reinstall.
PATCHRIGHT_INSTALLED=false
if python3 -c "import patchright" 2>/dev/null; then
    if [ -n "$VENDOR_WHEEL" ]; then
        # Check if the installed version came from our vendor wheel by looking
        # for our patched init script behavior (route.continue_ instead of route.fallback)
        INSTALLED_FROM_VENDOR=$(python3 -c "
import importlib.util, pathlib
spec = importlib.util.find_spec('patchright')
if spec and spec.origin:
    pkg_dir = pathlib.Path(spec.origin).parent
    init_helper = pkg_dir / '_impl' / '_helper.py'
    if init_helper.exists():
        content = init_helper.read_text()
        # Vendor wheel uses route.continue_(), PyPI uses route.fallback()
        if 'route.continue_()' in content or 'patchright-init-script-inject.internal' not in content:
            print('yes')
        else:
            print('no')
    else:
        print('unknown')
else:
    print('no')
" 2>/dev/null || echo "no")

        if [ "$INSTALLED_FROM_VENDOR" = "yes" ]; then
            PATCHRIGHT_INSTALLED=true
            log_info "Patched patchright (vendor wheel) already installed"
        else
            log_info "patchright installed but not from vendor wheel, reinstalling with patches..."
        fi
    else
        PATCHRIGHT_INSTALLED=true
        log_info "patchright already installed (no vendor wheel available)"
    fi
fi

# Install patchright and all other dependencies in one pass.
if [ "$PATCHRIGHT_INSTALLED" = false ]; then
    if [ -n "$VENDOR_WHEEL" ]; then
        # Install vendor wheel (patched) + trafilatura together
        log_info "Installing patched patchright from vendor wheel + dependencies..."
        uv pip install --system "${VENDOR_WHEEL}" "trafilatura>=2.0.0" --quiet --reinstall-package patchright || {
            log_error "Failed to install dependencies"
            exit 2
        }
    else
        log_info "No vendor wheel found, installing from PyPI (may be unpatched)..."
        uv pip install --system patchright "trafilatura>=2.0.0" --quiet || {
            log_error "Failed to install dependencies"
            exit 2
        }
    fi
    log_success "All dependencies installed"
else
    # Patchright already correct, just ensure trafilatura is present
    if ! python3 -c "import trafilatura" 2>/dev/null; then
        log_info "Installing trafilatura..."
        uv pip install --system "trafilatura>=2.0.0" --quiet || {
            log_error "Failed to install trafilatura (non-fatal)"
        }
    fi
fi

# Step 2: Install Chrome browser via patchright
log_info "Checking Chrome browser installation..."

# Check if Chrome is already installed
CHROME_INSTALLED=false
if python3 -c "from patchright.sync_api import sync_playwright; p = sync_playwright().start(); browser = p.chromium.launch(channel='chrome'); browser.close(); p.stop()" 2>/dev/null; then
    CHROME_INSTALLED=true
    log_info "Chrome browser already installed"
else
    log_info "Chrome browser not found or not working, installing..."
fi

if [ "$CHROME_INSTALLED" = false ]; then
    uv run patchright install chrome || {
        log_error "Failed to install Chrome via patchright"
        exit 2
    }
    log_success "Chrome browser installed successfully"
fi

# Step 3: Set up environment variables for the session
if [ -n "$CLAUDE_ENV_FILE" ]; then
    log_info "Persisting environment variables to session..."

    # Add skill directory to Python path
    echo "export PYTHONPATH=\"$SKILL_DIR:\${PYTHONPATH:-}\"" >> "$CLAUDE_ENV_FILE"
    log_success "Added skill directory to PYTHONPATH"

    # Find and persist Chrome executable path
    CHROME_PATH=$(python3 -c "
try:
    from patchright.sync_api import sync_playwright
    p = sync_playwright().start()
    print(p.chromium.executable_path)
    p.stop()
except Exception:
    pass
" 2>/dev/null || echo "")

    if [ -n "$CHROME_PATH" ] && [ -x "$CHROME_PATH" ]; then
        echo "export CHROME_EXECUTABLE=\"$CHROME_PATH\"" >> "$CLAUDE_ENV_FILE"
        log_success "Chrome path persisted: $CHROME_PATH"
    fi

    # Mark that setup has completed (for debugging)
    echo "export PLAYWRIGHT_SKILL_SETUP_COMPLETE=true" >> "$CLAUDE_ENV_FILE"
else
    log_info "CLAUDE_ENV_FILE not available (local environment)"
fi

# Step 4: Verify installation
log_info "Verifying installation..."

# Verify patchright can be imported
if ! python3 -c "import patchright; v = getattr(patchright, '__version__', 'unknown'); print(f'patchright {v}')" 2>/dev/null; then
    log_error "Failed to import patchright after installation"
    exit 2
fi

# Verify Chrome is accessible
if ! python3 -c "from patchright.sync_api import sync_playwright; p = sync_playwright().start(); p.chromium.executable_path; p.stop()" 2>/dev/null; then
    log_error "Chrome browser not accessible after installation"
    exit 2
fi

log_success "Session setup completed successfully"
log_info "Skill ready at: $SKILL_DIR"
log_info "Claude can now use the playwright-skill for browser automation"

exit 0
