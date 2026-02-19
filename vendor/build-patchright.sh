#!/bin/bash
# Build a patched patchright wheel from upstream source.
#
# Fixes applied (upstream PRs #96 and #99):
#   - CDN URL: playwright.azureedge.net -> cdn.playwright.dev
#   - Init script DNS: route.fallback(url=...internal) -> route.continue_()
#   - Validation flag to catch future URL changes
#
# Usage:
#   cd vendor && bash build-patchright.sh [VERSION]
#   # Default version: 1.58.0
#
# Prerequisites:
#   pip install toml setuptools wheel setuptools_scm
#
# Exit strategy: When upstream merges PRs #96/#99, delete vendor/ and
# revert pyproject.toml to use PyPI directly (patchright>=1.58.1 or later).

set -euo pipefail

VERSION="${1:-1.58.0}"
BUILD_DIR="/tmp/patchright-build-$$"
# Capture output directory NOW before any cd commands
OUTPUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Building patchright ${VERSION} ==="
echo "Build dir: ${BUILD_DIR}"

# Clone repos
echo "Cloning patchright-python..."
git clone --quiet https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python.git "${BUILD_DIR}"
echo "Cloning playwright-python v${VERSION}..."
git clone --quiet https://github.com/microsoft/playwright-python --branch "v${VERSION}" --depth=1 "${BUILD_DIR}/playwright-python"

cd "${BUILD_DIR}"

# === Fix 1: CDN URL (PR #96) ===
# Playwright changed CDN from playwright.azureedge.net to cdn.playwright.dev.
# The AST pattern match in patch_python_package.py silently fails without this.
echo "Applying fix: CDN URL (PR #96)..."
sed -i 's|https://playwright.azureedge.net/builds/driver/|https://cdn.playwright.dev/builds/driver/|g' patch_python_package.py

# Add validation flag so future URL changes fail loudly
sed -i '/^patchright_version/a\\n# Validation flags to catch silent failures when upstream changes\nupstream_driver_url_found = False' patch_python_package.py
# Set flag when URL match is found (insert after the url match line)
sed -i '/node.targets\[0\].id == "url"/,/node.value = ast.JoinedStr/{
    /node.value = ast.JoinedStr/i\                upstream_driver_url_found = True
}' patch_python_package.py
# Assert after setup.py patching
sed -i '/patch_file("playwright-python\/setup.py", setup_tree)/a\\nif not upstream_driver_url_found:\n    raise RuntimeError(\n        "Failed to find upstream driver URL in setup.py. "\n        "Playwright may have changed their CDN URL again. "\n        "The patchright driver will NOT be downloaded."\n    )' patch_python_package.py

# === Fix 2: Init script DNS (PR #99) ===
# The .internal domain doesn't resolve, causing navigation failures.
echo "Applying fix: init script DNS (PR #99)..."
python3 -c "
with open('patch_python_package.py') as f:
    content = f.read()
# Replace all occurrences of the broken route handler
old = '''                    protocol = route.request.url.split(\":\")[0]
                    await route.fallback(url=f\"{protocol}://patchright-init-script-inject.internal/\")'''
new = '''                    await route.continue_()'''
count = content.count(old)
content = content.replace(old, new)
with open('patch_python_package.py', 'w') as f:
    f.write(content)
print(f'  Replaced route handler in {count} location(s)')
"

# === Run the patching script ===
echo "Running patch_python_package.py..."
pip install --quiet toml 2>/dev/null || true
patchright_release="${VERSION}" python3 patch_python_package.py

# Fix license field format for newer setuptools
python3 -c "
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
pip install --quiet setuptools wheel setuptools_scm 2>/dev/null || true
# Uninstall auditwheel if present (incompatible version causes errors, and it's optional)
pip uninstall -y auditwheel 2>/dev/null || true
PLAYWRIGHT_TARGET_WHEEL=manylinux1_x86_64.whl python3 setup.py bdist_wheel 2>&1 | tail -3

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
rm -rf "${BUILD_DIR}"
