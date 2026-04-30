#!/bin/bash
# Build a patched patchright wheel from upstream source.
#
# Fix applied (upstream PR #99, closed/rejected):
#   - Init script DNS: route.fallback(url=...internal) -> route.continue_()
#
# The maintainer rejected PR #99 ("This is needed for Patchright's internal
# InitScript functionality"), but the .internal domain breaks navigation in
# environments where it doesn't resolve.
#
# Note: PR #96 (CDN URL: playwright.azureedge.net -> cdn.playwright.dev) was
# merged upstream in v1.58.0, so it is no longer patched here.
#
# Usage:
#   cd vendor && bash build-patchright.sh [VERSION]
#   # Default version: 1.59.0
#
# Prerequisites:
#   python3 with venv module (script creates an isolated venv for the build,
#   which avoids Debian/Ubuntu-patched setuptools breaking bdist_wheel).
#
# Exit strategy: If upstream ever accepts the DNS fix (or removes the
# .internal redirect), delete vendor/ and revert pyproject.toml to use
# patchright from PyPI directly.

set -euo pipefail

VERSION="${1:-1.59.0}"
BUILD_DIR="/tmp/patchright-build-$$"
VENV_DIR="/tmp/patchright-venv-$$"
# Capture output directory NOW before any cd commands
OUTPUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Building patchright ${VERSION} ==="
echo "Build dir: ${BUILD_DIR}"

# Create isolated venv with clean setuptools (Debian/Ubuntu's patched
# setuptools breaks bdist_wheel with "AttributeError: install_layout").
echo "Creating build venv..."
python3 -m venv "${VENV_DIR}"
PY="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"
"${PIP}" install --quiet --upgrade pip setuptools wheel setuptools_scm toml

# Clone repos
echo "Cloning patchright-python..."
git clone --quiet https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python.git "${BUILD_DIR}"
echo "Cloning playwright-python v${VERSION}..."
git clone --quiet https://github.com/microsoft/playwright-python --branch "v${VERSION}" --depth=1 "${BUILD_DIR}/playwright-python"

cd "${BUILD_DIR}"

# === Fix: Init script DNS (PR #99, rejected) ===
# The .internal domain doesn't resolve, causing navigation failures.
echo "Applying fix: init script DNS (PR #99)..."
"${PY}" -c "
with open('patch_python_package.py') as f:
    content = f.read()
# Replace all occurrences of the broken route handler
old = '''                    protocol = route.request.url.split(\":\")[0]
                    await route.fallback(url=f\"{protocol}://patchright-init-script-inject.internal/\")'''
new = '''                    await route.continue_()'''
count = content.count(old)
if count == 0:
    raise RuntimeError(
        'Failed to find .internal route handler in patch_python_package.py. '
        'Upstream patchright-python may have changed; review the script.'
    )
content = content.replace(old, new)
with open('patch_python_package.py', 'w') as f:
    f.write(content)
print(f'  Replaced route handler in {count} location(s)')
"

# === Run the patching script ===
echo "Running patch_python_package.py..."
patchright_release="${VERSION}" "${PY}" patch_python_package.py

# Fix license field format for newer setuptools
"${PY}" -c "
import toml
with open('playwright-python/pyproject.toml') as f:
    data = toml.load(f)
if isinstance(data['project'].get('license'), str):
    data['project']['license'] = {'text': data['project']['license']}
    with open('playwright-python/pyproject.toml', 'w') as f:
        toml.dump(data, f)
    print('  Fixed license field format')
"

# === Build the wheel ===
echo "Building wheel..."
cd playwright-python
PLAYWRIGHT_TARGET_WHEEL=manylinux1_x86_64.whl "${PY}" setup.py bdist_wheel 2>&1 | tail -3

# === Copy result ===
WHEEL=$(ls dist/patchright-*.whl)
cp "${WHEEL}" "${OUTPUT_DIR}/"
WHEEL_NAME=$(basename "${WHEEL}")
echo ""
echo "=== Built successfully ==="
echo "Wheel: ${OUTPUT_DIR}/${WHEEL_NAME}"
echo ""
echo "Next steps:"
echo "  uv pip install vendor/${WHEEL_NAME}"
echo "  uv run python -m patchright install chrome"

# Cleanup
rm -rf "${BUILD_DIR}" "${VENV_DIR}"
