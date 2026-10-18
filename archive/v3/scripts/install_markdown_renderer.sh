#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
RENDERER_DIR="$REPO_ROOT/tools/markdown-renderer"

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js was not found in PATH. Install Node.js 20+ first." >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm was not found in PATH. Install Node.js with npm first." >&2
  exit 1
fi

cd "$RENDERER_DIR"
npm install
npx playwright install chromium

if command -v npx >/dev/null 2>&1; then
  echo "If Chromium fails to launch on Linux, run:"
  echo "  npx playwright install-deps chromium"
fi

npm run check

cat <<'EOF'

Markdown renderer installed.
Enable it in config.yaml:
render:
  markdown_image:
    enabled: true
EOF
